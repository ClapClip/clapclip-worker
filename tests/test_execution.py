import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from mediaworker.config import Config
from mediaworker.engine.base import Engine, JobContext, RunResult
from mediaworker.execution import ExecutionResources
from mediaworker.memory import MemoryEstimate, MemoryLedger
from mediaworker import lifecycle, scheduling
from mediaworker.models import ClaimedJob
from mediaworker.single_instance import FileLock

GIB = 1024**3


class Group:
    def __init__(self, gated=False):
        self.gate = threading.Event()
        if not gated:
            self.gate.set()
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.started = threading.Event()
        self.two = threading.Event()
        self.per_session_peak = 0


class Session:
    def __init__(self, group):
        self.group, self.lock, self.active = group, threading.Lock(), 0

    def get_providers(self):
        return ["DmlExecutionProvider", "CPUExecutionProvider"]

    def run(self):
        with self.lock:
            with self.group.lock:
                self.active += 1
                self.group.active += 1
                self.group.peak = max(self.group.peak, self.group.active)
                self.group.per_session_peak = max(self.group.per_session_peak, self.active)
                self.group.started.set()
                if self.group.active >= 2:
                    self.group.two.set()
            try:
                if not self.group.gate.wait(5):
                    raise TimeoutError("generated thread latch not released")
            finally:
                with self.group.lock:
                    self.active -= 1
                    self.group.active -= 1


class FixtureEngine(Engine):
    def __init__(self, group, spec=None):
        self.group, self.spec, self.task_type = group, spec, "cutout"
        self.sessions = {name: Session(group) for name in ("m1", "m2") if spec is None or name == spec[0]}
        self.available_face_models = ["gpen", "gfpgan"] if spec is None else [spec[1]] if spec[1] else []
        for face in self.available_face_models:
            self.sessions["face:"+face] = Session(group)
        self.sessions["recognition"] = Session(group)
        self.sessions["detector"] = Session(group)

    @property
    def available_models(self):
        return [name for name in self.sessions if name in {"m1", "m2"}]

    @property
    def execution_sessions(self):
        return self.sessions

    def context_spec(self, params):
        return params.get("model", "m1"), params.get("face", "gpen")

    def context_requirements(self, spec=None):
        return MemoryEstimate(1024, 1024, 1024, 1024, (("DmlExecutionProvider", 0),))

    def spawn_context(self, spec=None):
        return type(self)(self.group, spec)

    def run(self, ctx):
        self.sessions[ctx.params.get("model", "m1")].run()
        output = ctx.workdir / "result.bin"
        output.write_bytes(b"generated")
        return RunResult(output, 1, 1)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config.from_env(load_env=False)
        self.cfg.runtime_dir = Path(self.temp.name) / "runtime"
        self.cfg.concurrency, self.cfg.ai_contexts, self.cfg.download_concurrency = 30, 2, 10
        self.owned = []

    def resource(self, engines):
        value = ExecutionResources(self.cfg, engines, headroom=lambda _: (64*GIB, 64*GIB))
        self.addCleanup(value.close)
        return value

    def admission(self, resources, caps):
        value = resources.try_acquire(caps)
        if value is not None:
            self.owned.append(value)
        return value

    def wait_admission(self, resources, caps):
        end = time.monotonic()+3
        while time.monotonic()<end:
            value = self.admission(resources, caps)
            if value is not None:
                return value
            time.sleep(.01)
        self.fail("ready independent context did not become available")

    def test_thirty_global_slots_are_reachable_and_thirty_first_is_rejected(self):
        slots = [scheduling.acquire_worker_slot(self.cfg.runtime_dir) for _ in range(30)]
        try:
            self.assertTrue(all(slots))
            self.assertIsNone(scheduling.acquire_worker_slot(self.cfg.runtime_dir))
        finally:
            for slot in slots:
                slot.release()
        again = scheduling.acquire_worker_slot(self.cfg.runtime_dir)
        self.assertIsNotNone(again)
        again.release()

    def test_ten_downloads_do_not_block_ready_AI_and_partial_IO_failure_releases_quota(self):
        resources = self.resource({"download": FixtureEngine(Group()), "cutout": FixtureEngine(Group())})
        tickets = []
        try:
            for _ in range(10):
                tickets.append(self.admission(resources, ["download"]))
            self.assertTrue(all(tickets))
            self.assertIsNone(self.admission(resources, ["download"]))
            ai = self.admission(resources, ["download", "cutout"])
            self.assertIsNotNone(ai)
            self.assertEqual(ai.capability, "cutout")
            ai.release()
        finally:
            for ticket in tickets:
                ticket.release()
        with patch.object(scheduling, "acquire_worker_slot", side_effect=OSError("generated")):
            with self.assertRaises(OSError):
                resources.try_acquire(["download"])
        self.assertEqual(resources.active, 0)
        self.assertEqual(resources.memory.tickets, 0)
        lock = FileLock(self.cfg.runtime_dir/"download-slots/slot-0.lock").acquire()
        lock.release()

    def test_each_AI_type_has_two_independent_overlapping_contexts_and_nondefault_profiles(self):
        for cap in ("faceswap", "cutout", "enhance"):
            for spec in (("m2", "gfpgan"), ("m1", "gpen")):
                with self.subTest(capability=cap, spec=spec):
                    group = Group(gated=True)
                    resources = ExecutionResources(self.cfg, {cap: FixtureEngine(group)}, headroom=lambda _: (64*GIB,64*GIB))
                    tickets, futures = [], []
                    try:
                        for index in range(2):
                            ticket = self.wait_admission(resources, [cap]);tickets.append(ticket)
                            params = {"model":spec[0], "face":spec[1], "ignoredURL":"synthetic-canary"}
                            ticket.claimed(SimpleNamespace(params=params))
                            directory=Path(self.temp.name)/cap/spec[0]/str(index);directory.mkdir(parents=True,exist_ok=True)
                            futures.append(resources.executor.submit(ticket.engine.run, JobContext(str(index),cap,params,{},directory)))
                            self.assertTrue(group.started.wait(1))
                        self.assertTrue(group.two.wait(1))
                        self.assertEqual(group.peak, 2)
                        self.assertEqual(group.per_session_peak, 1)
                        self.assertEqual(resources.pools[cap][1].spec, spec)
                        self.assertFalse(set(map(id,resources.pools[cap][0].engine.execution_sessions.values())) &
                                         set(map(id,resources.pools[cap][1].engine.execution_sessions.values())))
                    finally:
                        group.gate.set()
                        for future in futures:future.result(timeout=2)
                        for ticket in tickets:ticket.release()
                        resources.close()

    def test_memory_epoch_never_refunds_estimate_as_actual_free_and_blocks_build_rebase(self):
        ledger=MemoryLedger();ledger.rebase(9*GIB,9*GIB)
        cost=MemoryEstimate(2*GIB,2*GIB,GIB,GIB)
        self.assertTrue(ledger.reserve_resident(cost));epoch=ledger.epoch
        self.assertFalse(ledger.rebase(100*GIB,100*GIB))
        self.assertEqual(ledger.ram,8*GIB)
        ledger.resident_ready(epoch)
        self.assertTrue(ledger.rebase(9*GIB,9*GIB));ledger.release_resident(cost,epoch)
        self.assertEqual((ledger.ram,ledger.resident_ram),(8*GIB,0))
        self.assertFalse(ledger.reserve_workspace(MemoryEstimate(workspace_ram=8*GIB+1),True))
        self.assertTrue(ledger.reserve_workspace(MemoryEstimate(workspace_ram=5*GIB),True))
        self.assertFalse(ledger.reserve_workspace(MemoryEstimate(workspace_ram=4*GIB),True))
        ledger.release_workspace(MemoryEstimate(workspace_ram=5*GIB))
        self.assertTrue(ledger.rebase(10*GIB,10*GIB));self.assertEqual(ledger.ram,9*GIB)

    def test_local_dispatch_keeps_AI_runnable_with_ten_downloads_and_backlog(self):
        from mediaworker import local_web
        down_group=Group(gated=True);ai=FixtureEngine(Group())
        class Download:
            def run(self,ctx):
                with down_group.lock:down_group.active+=1;down_group.peak=max(down_group.peak,down_group.active)
                try:
                    if not down_group.gate.wait(5):raise TimeoutError("generated gate")
                    path=ctx.workdir/'result.bin';path.write_bytes(b'generated');return RunResult(path,1,1)
                finally:
                    with down_group.lock:down_group.active-=1
        factory=lambda cfg,engines:ExecutionResources(cfg,engines,headroom=lambda _:(64*GIB,64*GIB))
        with patch.object(local_web,'ExecutionResources',side_effect=factory):
            app=local_web.Workbench(self.cfg,Path(self.temp.name)/'local',{'download':Download(),'cutout':ai})
        try:
            with app.lock:
                for index in range(21):
                    directory=app.root/str(index);directory.mkdir()
                    app.jobs[str(index)]=local_web.Job(str(index),'download' if index<20 else 'cutout',{},directory,{})
            app.wakeup.set();end=time.monotonic()+3
            while time.monotonic()<end and app.jobs['20'].status!='ready':time.sleep(.01)
            self.assertEqual(app.jobs['20'].status,'ready')
            while time.monotonic()<end and down_group.peak<10:time.sleep(.01)
            self.assertEqual(down_group.peak,10)
            self.assertEqual(sum(j.status=='queued' for j in app.jobs.values()),10)
        finally:down_group.gate.set();app.close()

    def test_actual_provider_drives_budget_and_new_primary_requires_fresh_epoch(self):
        from mediaworker.engine.base import context_requirements
        cpu=SimpleNamespace(get_providers=lambda:['CPUExecutionProvider'])
        dml=SimpleNamespace(get_providers=lambda:['DmlExecutionProvider','CPUExecutionProvider'],get_provider_options=lambda:{'DmlExecutionProvider':{'device_id':'1'}})
        self.assertEqual(context_requirements(self.cfg,[],{'session':cpu}).devices,())
        self.assertEqual(context_requirements(self.cfg,[],{'session':dml}).devices,(('DmlExecutionProvider',1),))
        resources=self.resource({'cutout':FixtureEngine(Group())})
        ticket=self.admission(resources,['cutout'])
        with self.assertRaises(RuntimeError):resources.register('enhance',FixtureEngine(Group()))
        ticket.release();resources.register('enhance',FixtureEngine(Group()))
        self.assertFalse(resources.budget_ready)
        with patch.object(resources,'_refresh_if_idle'):
            self.assertIsNone(resources.try_acquire(['enhance']))
        resources._refresh_memory();ticket=self.admission(resources,['enhance']);self.assertIsNotNone(ticket);ticket.release()

    def test_local_submit_accepts_thirty_pending_tasks_and_rejects_the_next(self):
        from mediaworker import local_web
        factory=lambda cfg,engines:ExecutionResources(cfg,engines,headroom=lambda _:(64*GIB,64*GIB))
        with patch.object(local_web,'ExecutionResources',side_effect=factory):
            app=local_web.Workbench(self.cfg,Path(self.temp.name)/'pending',{'download':FixtureEngine(Group())})
        try:
            with app.lock:
                app.loading=True
                for _ in range(30):app.submit({'tool':'download','params':{'operation':'info','url':'https://offline.invalid/fixture'}})
                with self.assertRaises(ValueError):app.submit({'tool':'download','params':{'operation':'info','url':'https://offline.invalid/fixture'}})
                self.assertEqual(len(app.jobs),30)
        finally:app.close()

    def test_dead_primary_rebuilds_capacity_for_future_claims_without_rerunning_a_job(self):
        class Alive(FixtureEngine):
            def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.is_alive=True
            def close(self):self.is_alive=False
        primary=Alive(Group());resources=self.resource({'cutout':primary})
        primary.is_alive=False
        ticket=self.wait_admission(resources,['cutout'])
        self.assertIsNot(ticket.engine.context.engine,primary)
        self.assertTrue(ticket.engine.context.engine.is_alive)
        self.assertTrue(ticket.engine.context.primary)
        self.assertEqual(primary.group.peak,0)
        ticket.release()




class EngineProfileTests(unittest.TestCase):
    def test_real_enhance_profile_factory_follows_two_nondefault_restorer_combinations(self):
        try:from mediaworker.engine.enhance_engine import EnhanceEngine
        except ImportError:self.skipTest('optional AI dependencies absent')
        class Generated(EnhanceEngine):
            def load(self):
                self._models={name:[(None,SimpleNamespace(session=object()))] for name in self._cfg.enhance_models}
                self._faces={name:SimpleNamespace(session=object()) for name in self._cfg.enhance_face_models}
                self._detector=SimpleNamespace(session=object()) if self._faces else None
        cfg=Config.from_env(load_env=False);cfg.enhance_models=['fast','animation'];cfg.enhance_face_models=['gfpgan','gpen']
        primary=Generated(cfg);primary.load()
        for spec in (('animation','gfpgan'),('fast','gpen')):
            self.assertEqual(primary.context_spec({'model':spec[0],'face':spec[1]}),spec)
            other=primary.spawn_context(spec)
            self.assertEqual(other.available_models,[spec[0],'portrait']);self.assertEqual(other.available_face_models,[spec[1]])
            self.assertFalse(set(map(id,primary.execution_sessions.values()))&set(map(id,other.execution_sessions.values())))
            self.assertIsNot(primary._run_lock,other._run_lock)

    def test_real_face_and_local_factories_keep_recognition_GIF_and_detector_independent(self):
        try:
            from mediaworker.engine.faceswap_engine import FaceSwapEngine
            from mediaworker.local_faceswap import LocalFaceSwap
        except ImportError:self.skipTest('optional AI dependencies absent')
        class Generated(FaceSwapEngine):
            def load(self):
                self._models={name:SimpleNamespace(session=object()) for name in self._model_paths}
                self._recognition=SimpleNamespace(session=object())
                self._gif=SimpleNamespace(detector=object()) if self._enable_gif else None
        cfg=Config.from_env(load_env=False);primary=Generated(cfg);primary.load()
        for spec in (('hyperswap_256_c',True),('hyperswap_256_a',False)):
            other=primary.spawn_context(spec)
            self.assertEqual(other.available_models,[spec[0]]);self.assertEqual(other._gif is not None,spec[1])
            self.assertFalse(set(map(id,primary.execution_sessions.values()))&set(map(id,other.execution_sessions.values())))
            self.assertIsNot(primary._inference_lock,other._inference_lock)
        class LocalGenerated(LocalFaceSwap):
            def load(self):
                self.detector=object();self.engine._models={name:SimpleNamespace(session=object()) for name in self.engine._model_paths}
                self.engine._recognition=SimpleNamespace(session=object())
        local=LocalGenerated(cfg);local.load()
        for model in ('hyperswap_256_a','hyperswap_256_c'):
            other=local.spawn_context((model,False))
            self.assertEqual(other.available_models,[model]);self.assertIsNone(other.engine._gif)
            self.assertFalse(set(map(id,local.execution_sessions.values()))&set(map(id,other.execution_sessions.values())))
            self.assertIsNot(local.lock,other.lock)


class ThreadOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_thirty_lifecycles_reach_upload_with_only_two_contexts_and_keep_slots_to_ACK(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=Config.from_env(load_env=False);cfg.runtime_dir=Path(directory)/"runtime";cfg.work_dir=Path(directory)/"work"
            cfg.concurrency,cfg.ai_contexts=30,2
            resources=ExecutionResources(cfg,{"cutout":FixtureEngine(Group())},headroom=lambda _:(64*GIB,64*GIB))
            client=SimpleNamespace(heartbeat=AsyncMock(),complete=AsyncMock(),fail=AsyncMock())
            uploads=0;all_uploading=asyncio.Event();release=asyncio.Event();tasks=[];tickets=[]
            async def upload(*args,**kwargs):
                nonlocal uploads
                uploads+=1
                if uploads==30:all_uploading.set()
                await release.wait()
            async def heartbeat(_client,_job,stop,*args):await stop.wait()
            async def process(ticket,job):
                try:await lifecycle._process_job(cfg,client,{"cutout":ticket.engine},job,executor=resources.executor)
                finally:ticket.release()
            try:
                with patch.object(lifecycle,"_heartbeat_loop",side_effect=heartbeat),patch.object(lifecycle.storage,"upload",side_effect=upload):
                    for index in range(30):
                        ticket=None
                        for _ in range(300):
                            ticket=resources.try_acquire(["cutout"])
                            if ticket:break
                            await asyncio.sleep(.001)
                        self.assertIsNotNone(ticket);tickets.append(ticket)
                        job=ClaimedJob(id=str(UUID(int=index+1)),lease_token=str(UUID(int=100+index)),attempt=1,task_type="cutout",params={"model":"m1"},inputs={},result={"put_url":"https://offline.test/put","key":str(index)})
                        ticket.claimed(job);tasks.append(asyncio.create_task(process(ticket,job)))
                    await asyncio.wait_for(all_uploading.wait(),3)
                    self.assertEqual(resources.active,30)
                    self.assertLessEqual(len(resources.pools["cutout"]),2)
                    self.assertIsNone(resources.try_acquire(["cutout"]))
                    self.assertIsNone(scheduling.acquire_worker_slot(cfg.runtime_dir))
                    self.assertEqual(client.complete.await_count,0)
                    release.set();await asyncio.gather(*tasks)
                    self.assertEqual(client.complete.await_count,30)
                    self.assertEqual(len({call.args[0] for call in client.complete.await_args_list}),30)
                    client.fail.assert_not_awaited();self.assertEqual(resources.active,0)
                    self.assertEqual(resources.memory.tickets,0)
            finally:
                release.set();await asyncio.gather(*tasks,return_exceptions=True)
                for ticket in tickets:ticket.release()
                resources.close()

    async def test_double_cancel_and_executor_shutdown_wait_for_real_thread_before_cleanup(self):
        for lose in (False, True):
            with self.subTest(lease_loss=lose), tempfile.TemporaryDirectory() as directory:
                cfg=Config.from_env(load_env=False);cfg.runtime_dir=Path(directory)/"runtime";cfg.work_dir=Path(directory)/"work"
                cfg.concurrency,cfg.ai_contexts=30,1
                group=Group(gated=True);resources=ExecutionResources(cfg,{"cutout":FixtureEngine(group)},headroom=lambda _:(64*GIB,64*GIB))
                admission=resources.try_acquire(["cutout"]);admission.claimed(SimpleNamespace(params={}))
                job=ClaimedJob(id=str(UUID(int=1)),lease_token=str(UUID(int=2)),attempt=1,task_type="cutout",params={"model":"m1"},inputs={},result={"put_url":"https://offline.test/put","key":"fixture"})
                client=SimpleNamespace(heartbeat=AsyncMock(),complete=AsyncMock(),fail=AsyncMock())
                lost_ref=[]
                async def heartbeat(_client,_job,stop,lost,*args):
                    lost_ref.append(lost);await stop.wait()
                with patch.object(lifecycle,"_heartbeat_loop",side_effect=heartbeat),patch.object(lifecycle.storage,"upload",new_callable=AsyncMock) as upload:
                    task=asyncio.create_task(lifecycle._process_job(cfg,client,{"cutout":admission.engine},job,executor=resources.executor))
                    try:
                        for _ in range(100):
                            if group.started.is_set():break
                            await asyncio.sleep(.01)
                        self.assertTrue(group.started.is_set());task.cancel();await asyncio.sleep(.02)
                        if lose:lost_ref[0].set()
                        task.cancel();resources.executor.shutdown(wait=False,cancel_futures=True);await asyncio.sleep(.03)
                        self.assertFalse(task.done());self.assertEqual(resources.memory.tickets,1)
                        self.assertTrue((cfg.work_dir/job.id/job.lease_token).exists())
                    finally:
                        group.gate.set()
                        with self.assertRaises(asyncio.CancelledError):await task
                        admission.release();resources.close()
                    upload.assert_not_awaited();client.complete.assert_not_awaited();client.fail.assert_not_awaited()
                    self.assertFalse((cfg.work_dir/job.id).exists())
