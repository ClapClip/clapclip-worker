"""Generated models and private child pipes only; no business claims or models."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
from types import SimpleNamespace
from uuid import UUID

from mediaworker.config import Config
from mediaworker.engine.base import JobContext
from mediaworker.model_process import ModelProcess, OwnedJob
from mediaworker.memory import MemoryEstimate, MemoryLedger
from mediaworker.execution import ExecutionResources
from mediaworker.models import ClaimedJob
from mediaworker import lifecycle

NATIVE_PROOF = None


class ExternalBudgetTests(unittest.TestCase):
    def test_child_GPU_commitments_survive_parent_epochs_until_exact_child_exit(self):
        gib=1024**3;ledger=MemoryLedger();cost=MemoryEstimate(gib,gib,0,0,(),True)
        ledger.account_materialized(cost);ledger.rebase(10*gib,4*gib)
        self.assertEqual(ledger.external_gpu,gib)
        self.assertTrue(ledger.reserve_resident(cost));epoch=ledger.epoch
        self.assertFalse(ledger.rebase(100*gib,100*gib))
        ledger.resident_ready(epoch);self.assertTrue(ledger.rebase(10*gib,4*gib))
        self.assertEqual(ledger.external_gpu,2*gib)
        self.assertFalse(ledger.reserve_workspace(MemoryEstimate(workspace_gpu=2*gib),True))
        ledger.release_resident(cost,epoch)
        self.assertEqual(ledger.external_gpu,gib)
        ledger.release_resident(cost,0);self.assertEqual(ledger.external_gpu,0)


@unittest.skipUnless(all(importlib.util.find_spec(n) for n in ('onnx','onnxruntime','numpy','PIL')),'optional native generated-model dependencies absent')
class ModelProcessTests(unittest.TestCase):
    def setUp(self):
        import onnxruntime as ort
        self.ort_version=ort.__version__
        if 'DmlExecutionProvider' not in ort.get_available_providers():self.skipTest('native DirectML provider unavailable')
        import onnx
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup);self.root=Path(self.temporary.name)
        self.cfg=Config.from_env(load_env=False);self.cfg.work_dir=self.root/'work';self.cfg.runtime_dir=self.root/'runtime'
        self.cfg.models_dir=self.cfg.cutout_models_dir=self.root/'models';self.cfg.cutout_models_dir.mkdir()
        self.cfg.onnx_providers=['DmlExecutionProvider','CPUExecutionProvider'];self.cfg.max_output_pixels=4096
        graph=onnx.helper.make_graph([onnx.helper.make_node('ReduceMean',['input'],['output'],axes=[1],keepdims=1)],'generated',
            [onnx.helper.make_tensor_value_info('input',onnx.TensorProto.FLOAT,[1,3,1024,1024])],
            [onnx.helper.make_tensor_value_info('output',onnx.TensorProto.FLOAT,[1,1,1024,1024])])
        model=onnx.helper.make_model(graph,opset_imports=[onnx.helper.make_opsetid('',13)]);model.ir_version=10
        (self.cfg.cutout_models_dir/'isnet-general-use.onnx').write_bytes(model.SerializeToString())

    def context(self,index):
        from PIL import Image
        directory=self.root/str(index);directory.mkdir();path=directory/'input.png';Image.new('RGB',(16,16),'white').save(path)
        return JobContext(str(index),'cutout',{}, {'image':path}, directory)

    def test_real_product_model_children_overlap_and_profile_DML_nodes(self):
        # Test-only exact command replacement adds profiling before importing
        # the genuine child entry. Production has no profiling/remote-code flag.
        import mediaworker.model_process as module
        source=Path(module.__file__).resolve();wrapper=self.root/'generated-profile-entry.py'
        wrapper.write_text('''import sys,os,time,json
from pathlib import Path
sys.path.insert(0,SOURCE_ROOT)
import onnxruntime as ort
import mediaworker.engine.base as base
original_options=base.session_options
def options(*args,**kwargs):
    value=original_options(*args,**kwargs);value.enable_profiling=True
    value.profile_file_prefix=str(Path(os.environ['TEMP'])/'generated-profile')
    value.intra_op_num_threads=value.inter_op_num_threads=1
    value.add_session_config_entry('session.intra_op.allow_spinning','0')
    return value
base.session_options=options
original_run=ort.InferenceSession.run
def run(self,*args,**kwargs):
    start=time.monotonic_ns()
    for _ in range(32):result=original_run(self,*args,**kwargs)
    end=time.monotonic_ns()
    Path(os.environ['TEMP'],'native-interval.json').write_text(json.dumps({'start_ns':start,'end_ns':end}))
    profile=Path(self.end_profiling());counts={}
    for event in json.loads(profile.read_text()):
        provider=event.get('args',{}).get('provider')
        if provider:counts[provider]=counts.get(provider,0)+1
    Path(os.environ['TEMP'],'provider-counts.json').write_text(json.dumps(counts))
    return result
ort.InferenceSession.run=run
from mediaworker.model_process import child_main
child_main()
'''.replace('SOURCE_ROOT',repr(str(source.parent.parent))),encoding='utf8')
        original=subprocess.Popen
        def profiled(args,*positional,**kwargs):
            if isinstance(args,list) and args[-2:]==[str(source),'--model-child']:
                args=[*args[:-2],str(wrapper)]
            return original(args,*positional,**kwargs)
        engines=[ModelProcess(self.cfg,'cutout') for _ in range(2)];contexts=[self.context(i) for i in range(2)]
        try:
            with patch.object(module.subprocess,'Popen',side_effect=profiled):
                for engine in engines:engine.load()
            self.assertNotEqual(engines[0].process.pid,engines[1].process.pid)
            barrier=threading.Barrier(2)
            def run(index):barrier.wait(timeout=3);return engines[index].run(contexts[index])
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures=[executor.submit(run,i) for i in range(2)]
                results=[f.result(timeout=45) for f in futures]
            from PIL import Image
            for result in results:
                with Image.open(result.output_path) as image:self.assertEqual(image.size,(16,16));self.assertEqual(image.mode,'RGBA')
            intervals=[];providers=[]
            for engine in engines:
                counts=json.loads(Path(engine.private.name,'provider-counts.json').read_text())
                self.assertGreaterEqual(counts.get('DmlExecutionProvider',0),32)
                providers.append(counts)
                intervals.append(json.loads(Path(engine.private.name,'native-interval.json').read_text()))
            self.assertLess(max(v['start_ns'] for v in intervals),min(v['end_ns'] for v in intervals))
            global NATIVE_PROOF
            NATIVE_PROOF={'provider_node_counts':providers,'native_monotonic_intervals':intervals,'outputs_verified':True,'actual_child_pids':[e.process.pid for e in engines],'ORT_version':self.ort_version,'business_models_or_throughput_not_verified':True}
        finally:
            directories=[Path(e.private.name) for e in engines if e.private is not None]
            for engine in engines:engine.close()
            self.assertTrue(all(e.process is None or e.process.poll() is not None for e in engines))
            self.assertTrue(all(not p.exists() for p in directories))

    def test_cancel_reaps_exact_model_child_before_return(self):
        engine=ModelProcess(self.cfg,'cutout');engine.load();context=self.context('cancel');context.cancel.set()
        directory=Path(engine.private.name)
        try:
            with self.assertRaisesRegex(RuntimeError,'CANCELLED'):engine.run(context)
            self.assertIsNotNone(engine.process.poll());self.assertFalse(directory.exists());self.assertFalse(engine.is_alive)
        finally:engine.close()

    def test_child_crash_is_failure_and_cannot_be_used_again(self):
        engine=ModelProcess(self.cfg,'cutout');engine.load();context=self.context('crash')
        try:
            engine.process.kill();engine.process.wait(timeout=5)
            with self.assertRaisesRegex(RuntimeError,'unavailable'):engine.run(context)
            self.assertFalse(engine.is_alive)
        finally:engine.close()

    def test_only_public_configuration_and_known_params_cross_the_child_boundary(self):
        import mediaworker.model_process as module
        original_popen=module.subprocess.Popen;original_send=ModelProcess._send;environments=[];messages=[]
        def popen(*args,**kwargs):environments.append(kwargs['env']);return original_popen(*args,**kwargs)
        def send(engine,value):messages.append(value);return original_send(engine,value)
        self.cfg.node_token=self.cfg.cognito_password=self.cfg.github_token='GENERATED_SECRET_CANARY'
        engine=ModelProcess(self.cfg,'cutout');context=self.context('boundary')
        context.params={'model':self.cfg.cutout_model,'ignoredURL':'https://secret.invalid/canary'};context.cancel.set()
        try:
            with patch.dict(os.environ,{'NODE_TOKEN':'GENERATED_SECRET_CANARY','COOKIE':'GENERATED_SECRET_CANARY','CUDA_VISIBLE_DEVICES':'0'}),patch.object(module.subprocess,'Popen',side_effect=popen),patch.object(ModelProcess,'_send',send):
                engine.load()
                with self.assertRaisesRegex(RuntimeError,'CANCELLED'):engine.run(context)
            self.assertNotIn('GENERATED_SECRET_CANARY',json.dumps(environments)+json.dumps(messages))
            self.assertEqual(environments[0]['CUDA_VISIBLE_DEVICES'],'0')
            self.assertEqual(messages[1]['params'],{'model':self.cfg.cutout_model})
            self.assertNotIn('secret.invalid',json.dumps(messages))
        finally:engine.close()
        invalid=ModelProcess(self.cfg,'cutout')
        with patch.dict(os.environ,{'CUDA_VISIBLE_DEVICES':'0\ninvalid'}),self.assertRaises(ValueError):invalid.load()
        self.assertIsNone(invalid.process);self.assertIsNone(invalid.private)

    @unittest.skipUnless(os.name=='nt','Windows Job parent-death proof')
    def test_parent_death_kills_the_exact_model_process(self):
        import ctypes
        from ctypes import wintypes
        import mediaworker.model_process as module
        settings={n:str(v) if isinstance(v,Path) else v for n in module.CONFIG_FIELDS for v in [getattr(self.cfg,n)]}
        settings['work_dir']=str(self.root/'owner-work');file=self.root/'public-engine.json';file.write_text(json.dumps(settings))
        owner_script=self.root/'generated-model-owner.py'
        owner_script.write_text('''import sys,json
from pathlib import Path
sys.path.insert(0,SOURCE)
from mediaworker.config import Config
from mediaworker.model_process import ModelProcess
cfg=Config.from_env(load_env=False)
for name,value in json.loads(Path(SETTINGS).read_text()).items():setattr(cfg,name,Path(value) if name.endswith('_dir') else value)
engine=ModelProcess(cfg,'cutout');engine.load()
print(json.dumps({'pid':engine.process.pid}),flush=True)
sys.stdin.buffer.read(1)
'''.replace('SOURCE',repr(str(Path(module.__file__).resolve().parent.parent))).replace('SETTINGS',repr(str(file))),encoding='utf8')
        environment={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','COMSPEC','PATHEXT','LANG','LC_ALL'}}
        environment.update(MEDIAWORKER_LOAD_DOTENV='0',PYTHONDONTWRITEBYTECODE='1',PYTHONNOUSERSITE='1',HOME=str(self.root),USERPROFILE=str(self.root),TEMP=str(self.root),TMP=str(self.root),__PYVENV_LAUNCHER__=sys.executable)
        owner=subprocess.Popen([sys._base_executable,'-B',str(owner_script)],env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        import queue
        received=queue.Queue();reader=threading.Thread(target=lambda:received.put(owner.stdout.readline(65537)));reader.start();handle=None
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        try:
            value=json.loads(received.get(timeout=20));handle=kernel.OpenProcess(0x101000,False,value['pid']);self.assertTrue(handle)
            self.assertEqual(kernel.WaitForSingleObject(handle,0),258)
            owner.kill();owner.wait(timeout=10)
            self.assertEqual(kernel.WaitForSingleObject(handle,10000),0)
        finally:
            if owner.poll() is None:owner.kill()
            owner.wait(timeout=10);reader.join(timeout=3)
            self.assertFalse(reader.is_alive())
            owner.stdin.close();owner.stdout.close()
            if handle:kernel.CloseHandle(handle)


class PosixOwnershipTests(unittest.TestCase):
    @unittest.skipIf(os.name=='nt','POSIX process-group test is not a Windows native proof')
    def test_exited_group_leader_does_not_hide_live_descendant(self):
        # Only generated sleeping test children, in a dedicated new session.
        process=subprocess.Popen([sys.executable,'-B','-c',
            "import subprocess,sys;subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(30)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"],start_new_session=True)
        job=OwnedJob(process);process.wait(timeout=5)
        try:
            self.assertFalse(job.empty());job.terminate(process)
            end=time.monotonic()+5
            while not job.empty() and time.monotonic()<end:time.sleep(.01)
            self.assertTrue(job.empty())
        finally:job.terminate(process)


@unittest.skipUnless(all(importlib.util.find_spec(n) for n in ('onnx','onnxruntime','numpy','PIL')),'optional native generated-model dependencies absent')
class ModelCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_double_cancel_and_lease_loss_keep_attempt_and_ticket_until_child_exit_confirmed(self):
        fixture=ModelProcessTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        import mediaworker.model_process as module
        source=Path(module.__file__).resolve();wrapper=fixture.root/'generated-blocked-model.py'
        wrapper.write_text('''import sys,os,threading
from pathlib import Path
sys.path.insert(0,SOURCE)
from mediaworker.engine.cutout_engine import CutoutEngine
def blocked(self,ctx):
    Path(os.environ['TEMP'],'entered').write_bytes(b'generated')
    threading.Event().wait(60)
    raise RuntimeError('generated native wait exceeded')
CutoutEngine.run=blocked
from mediaworker.model_process import child_main
child_main()
'''.replace('SOURCE',repr(str(source.parent.parent))),encoding='utf8')
        original=subprocess.Popen
        def blocked(args,*positional,**kwargs):
            if isinstance(args,list) and args[-2:]==[str(source),'--model-child']:args=[*args[:-2],str(wrapper)]
            return original(args,*positional,**kwargs)
        engine=ModelProcess(fixture.cfg,'cutout')
        with patch.object(module.subprocess,'Popen',side_effect=blocked):engine.load()
        entered=Path(engine.private.name)/'entered'
        resources=ExecutionResources(fixture.cfg,{'cutout':engine},headroom=lambda _:(64*1024**3,64*1024**3))
        admission=resources.try_acquire(['cutout'])
        job=ClaimedJob(id=str(UUID(int=601)),lease_token=str(UUID(int=602)),attempt=1,task_type='cutout',params={},inputs={},result={'put_url':'https://offline.test/put','key':'generated'})
        admission.claimed(job);client=SimpleNamespace(heartbeat=AsyncMock(),complete=AsyncMock(),fail=AsyncMock())
        lost_ref=[];release=threading.Event();original_empty=OwnedJob.empty
        def confirmed(job_object):return original_empty(job_object) if release.is_set() else False
        async def heartbeat(_client,_job,stop,lost,*args):lost_ref.append(lost);await stop.wait()
        with patch.object(OwnedJob,'empty',confirmed),patch.object(lifecycle,'_heartbeat_loop',side_effect=heartbeat),patch.object(lifecycle.storage,'upload',new_callable=AsyncMock) as upload:
            task=asyncio.create_task(lifecycle._process_job(fixture.cfg,client,{'cutout':admission.engine},job,executor=resources.executor))
            try:
                for _ in range(300):
                    if entered.exists():break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.exists());task.cancel()
                for _ in range(100):
                    if engine.process.poll() is not None:break
                    await asyncio.sleep(.01)
                self.assertIsNotNone(engine.process.poll())
                lost_ref[0].set();task.cancel();await asyncio.sleep(.03)
                self.assertFalse(task.done());self.assertEqual(resources.active,1);self.assertEqual(resources.memory.tickets,1)
                self.assertTrue((fixture.cfg.work_dir/job.id/job.lease_token).exists())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,5)
                admission.release();resources.close()
            upload.assert_not_awaited();client.complete.assert_not_awaited();client.fail.assert_not_awaited()
            self.assertFalse((fixture.cfg.work_dir/job.id).exists());self.assertEqual(resources.memory.external_gpu,0)
