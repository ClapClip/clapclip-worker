"""Synthetic manager/child interactions. No OS service, node or media task."""
import asyncio
from dataclasses import asdict
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mediaworker.managed import agent, processes
from mediaworker.managed.common import ManagedError, read_json, write_json
from mediaworker.managed.host import Process
from mediaworker.managed.runtime import RuntimeEvents
from mediaworker import scheduling


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_gate_precedes_polling_and_only_matching_session_opens_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = RuntimeEvents(root, "fixture-session", "a" * 40)
            write_json(root / "manager.json", {"identity": asdict(processes.identity(os.getpid()))})
            shutdown = asyncio.Event()
            task = asyncio.create_task(events.before_polling(None, {"cutout": SimpleNamespace(available_models=["synthetic"])}, shutdown))
            await asyncio.sleep(0)
            self.assertEqual(read_json(root / "worker.json")["stage"], "registered_gate_closed")
            self.assertFalse(task.done())
            write_json(root / "allow-claims.json", {"session": "different-session"})
            await asyncio.sleep(.11)
            self.assertFalse(task.done())
            write_json(root / "allow-claims.json", {"session": "fixture-session"})
            await asyncio.wait_for(task, 1)
            self.assertEqual(read_json(root / "worker.json")["stage"], "polling_enabled")

    async def test_drain_cancels_gate_before_any_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = RuntimeEvents(root, "fixture", "a" * 40)
            write_json(root / "manager.json", {"identity": asdict(processes.identity(os.getpid()))})
            shutdown = asyncio.Event()
            gate = asyncio.create_task(events.before_polling(None, {"cutout": SimpleNamespace(available_models=[])}, shutdown))
            write_json(root / "drain.json", {"session": "fixture"})
            await events.watch(shutdown)
            await asyncio.wait_for(gate, 1)
            self.assertTrue(shutdown.is_set())
            self.assertNotIn("accepted_poll_groups", read_json(root / "worker.json"))

    async def test_real_temporary_owner_exit_closes_gate_without_any_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-B", "-c", "import time; time.sleep(30)"],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                record = processes.identity(parent.pid)
                write_json(root / "manager.json", {"identity": asdict(record)})
                events = RuntimeEvents(root, "fixture", "a" * 40)
                self.assertTrue(events.manager_alive())
                shutdown = asyncio.Event()
                watch = asyncio.create_task(events.watch(shutdown))
                parent.terminate(); parent.wait(timeout=5)
                await asyncio.wait_for(watch, 1)
                self.assertTrue(shutdown.is_set())
                self.assertFalse(events.manager_alive())
            finally:
                if parent.poll() is None:
                    parent.terminate(); parent.wait(timeout=5)

    async def test_manager_pid_reuse_requests_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = processes.identity(os.getpid())
            write_json(root / "manager.json", {"identity": {**asdict(current), "created": current.created - 100}})
            events = RuntimeEvents(root, "fixture", "a" * 40)
            shutdown = asyncio.Event()
            await asyncio.wait_for(events.watch(shutdown), 1)
            self.assertTrue(shutdown.is_set())

    async def test_unreadable_state_or_status_still_stops_polling(self):
        with tempfile.TemporaryDirectory() as directory:
            events = RuntimeEvents(Path(directory), "fixture", "a" * 40)
            shutdown = asyncio.Event()
            with patch.object(events, "manager_alive", return_value=True), patch.object(events, "requested", side_effect=ManagedError("STATE_UNREADABLE")), patch.object(events, "record", side_effect=OSError):
                await asyncio.wait_for(events.watch(shutdown), 1)
            self.assertTrue(shutdown.is_set())

    async def test_owner_death_preserves_accepted_work_and_prevents_next_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-B", "-c", "import time; time.sleep(30)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                write_json(root / "manager.json", {"identity": asdict(processes.identity(owner.pid))})
                events = RuntimeEvents(root, "fixture", "a" * 40)
                shutdown, accepted, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
                client = SimpleNamespace(claim=AsyncMock(return_value=object()))
                slot = Mock()
                async def complete_in_flight(*_):
                    accepted.set()
                    await finish.wait()
                cfg = SimpleNamespace(runtime_dir=root, api_base_url="http://localhost", poll_interval_s=.01, concurrency=30)
                engine = SimpleNamespace(available_models=["synthetic"])
                with patch.object(scheduling, "acquire_worker_slot", return_value=slot), patch.object(scheduling, "_process_job", side_effect=complete_in_flight):
                    poller = asyncio.create_task(scheduling._worker_loop(0, cfg, client, {"cutout": engine}, shutdown, ["cutout"]))
                    watch = asyncio.create_task(events.watch(shutdown))
                    await asyncio.wait_for(accepted.wait(), 1)
                    owner.terminate(); owner.wait(timeout=5)
                    await asyncio.wait_for(watch, 1)
                    self.assertFalse(poller.done())
                    slot.release.assert_not_called()
                    finish.set()
                    await asyncio.wait_for(poller, 1)
                self.assertEqual(client.claim.await_count, 1)
                slot.release.assert_called_once()
            finally:
                if owner.poll() is None:
                    owner.terminate(); owner.wait(timeout=5)


class ProcessFamilyTests(unittest.TestCase):
    def family(self):
        child = processes.Child.__new__(processes.Child)
        child.root, child.release = Path("host"), Path("host/release")
        child.arguments = ["venv/python", "-B", "-m", "mediaworker.managed", "worker", "--session", "fixture"]
        child.launcher = Process(10, 1, 100., "venv/python", "owned")
        child.known = {10: child.launcher}
        child.images = lambda: {Path("venv/python").resolve(), Path("tools/python").resolve()}
        return child

    def test_windows_launcher_and_exact_actual_python_are_one_family(self):
        child = self.family()
        actual = Process(11, 10, 101., "tools/python", "owned")
        handle = Mock(pid=11)
        handle.cmdline.return_value = [actual.image, *child.arguments[1:]]
        parent = Mock()
        parent.children.return_value = [handle]
        with patch.object(processes, "same_process", return_value=True), patch.object(processes.psutil, "Process", return_value=parent), patch.object(processes, "identity", return_value=actual):
            self.assertEqual(set(child.refresh()), {10, 11})
            self.assertTrue(child.ready_identity(11))

    def test_unrelated_python_descendant_is_not_permitted(self):
        child = self.family()
        handle = Mock(pid=11)
        handle.cmdline.return_value = ["tools/python", "unrelated-script.py"]
        parent = Mock()
        parent.children.return_value = [handle]
        with patch.object(processes, "same_process", return_value=True), patch.object(processes.psutil, "Process", return_value=parent), patch.object(processes, "identity", return_value=Process(11, 10, 101., "tools/python", "owned")):
            self.assertEqual(set(child.refresh()), {10})

    def test_known_actual_child_is_tracked_after_launcher_exit(self):
        child = self.family()
        child.known[11] = Process(11, 10, 101., "tools/python", "owned")
        p = Mock(); p.children.return_value = []
        with patch.object(processes, "same_process", side_effect=lambda record: record.pid == 11), patch.object(processes.psutil, "Process", return_value=p):
            self.assertEqual(set(child.refresh()), {11})

    def test_recycled_pid_does_not_match_old_creation_time(self):
        old = Process(11, 10, 101., "tools/python", "owned")
        with patch.object(processes, "identity", return_value=Process(11, 10, 202., "tools/python", "owned")):
            self.assertFalse(processes.same_process(old))

    def test_unverifiable_identity_is_not_reported_as_exit(self):
        record = Process(11, 10, 101., "tools/python", "owned")
        with patch.object(processes, "identity", side_effect=ManagedError("OWNED_PROCESS_IDENTITY_UNVERIFIABLE")):
            with self.assertRaisesRegex(ManagedError, "OWNED_PROCESS_IDENTITY_UNVERIFIABLE"):
                processes.same_process(record)


class AgentTransitionTests(unittest.TestCase):
    def setup_agent(self, directory, failures=()):
        root = Path(directory)
        data = root / "data"
        release = Mock()
        release.release.side_effect = lambda revision: root / revision
        log = []
        instances = []

        class FakeChild:
            def __init__(self, host, source, session, revision, env):
                if instances:
                    assert not instances[-1].living, "overlapping worker"
                self.revision, self.session = revision, session
                self.living = revision not in failures
                self.directory = data / "sessions" / session
                self.record = Process(99, 1, 1., "synthetic-python", "owned")
                instances.append(self)
                log.append(("start", revision))
                assert not (self.directory / "allow-claims.json").exists()
                write_json(self.directory / "worker.json", {"pid": 99, "session": session, "revision": revision,
                           "stage": "registered_gate_closed", "capabilities": ["cutout"]})
            def refresh(self): return {99: self.record} if self.living else {}
            def ready_identity(self, pid): return pid == 99 and self.living
            def alive(self):
                if (self.directory / "drain.json").exists():
                    log.append(("exit", self.revision))
                    self.living = False
                return self.living
            def terminate_gated(self):
                assert not (self.directory / "allow-claims.json").exists()
                self.living = False

        with patch.object(agent, "manager_family", return_value={os.getpid()}):
            manager = agent.Agent(root, releases=release, child_factory=FakeChild, sleep=lambda _: None)
        manager.inventory = Mock()
        return manager, log, instances

    def select(self, revision):
        return {"revision": revision * 40, "configuration": {"capabilities": ["cutout"]}}

    def test_admission_is_committed_after_old_fully_exited(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, "public_environment", return_value={}):
            manager, log, children = self.setup_agent(directory)
            old, new = self.select("a"), self.select("b")
            manager.activate(old, None)
            manager.activate(new, old)
            self.assertLess(log.index(("exit", old["revision"])), log.index(("start", new["revision"])))
            self.assertEqual(read_json(manager.data / "selected.json"), new)
            self.assertTrue((children[-1].directory / "allow-claims.json").is_file())

    def test_start_failure_rolls_back_after_candidate_exit(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, "public_environment", return_value={}):
            manager, log, children = self.setup_agent(directory, failures={"b" * 40})
            old, new = self.select("a"), self.select("b")
            manager.activate(old, None)
            with self.assertRaises(ManagedError):
                manager.activate(new, old)
            self.assertEqual(read_json(manager.data / "selected.json"), old)
            self.assertTrue(children[-1].living)
            self.assertFalse((children[-2].directory / "allow-claims.json").exists())

    def test_preparation_failure_leaves_old_worker_and_pointer_intact(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, "public_environment", return_value={}):
            manager, log, children = self.setup_agent(directory)
            selected = self.select("a")
            manager.activate(selected, None)
            manager.releases.latest.return_value = "b" * 40
            manager.releases.prepare.side_effect = ManagedError("RELEASE_VALIDATION_FAILED")
            self.assertEqual(manager.update(selected), selected)
            self.assertEqual(len(children), 1)
            self.assertTrue(children[0].living)

    def test_drain_timeout_never_kills_or_starts_replacement(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, "public_environment", return_value={}):
            manager, log, children = self.setup_agent(directory)
            manager.activate(self.select("a"), None)
            children[0].alive = Mock(return_value=True)
            manager.clock = Mock(side_effect=[0., 2.])
            with self.assertRaisesRegex(ManagedError, "IN_FLIGHT_DRAIN_TIMEOUT"):
                manager.drain(timeout=1)
            self.assertEqual(len(children), 1)
            self.assertTrue(children[0].living)

    def test_every_exception_drains_before_releasing_host_lock(self):
        for error in (ManagedError("HOST_PROCESS_INVENTORY_FAILED"), OSError("synthetic"), ValueError("synthetic")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                manager, _, _ = self.setup_agent(directory)
                ownership = []
                class Lock:
                    def __enter__(self): ownership.append("held")
                    def __exit__(self, *_): ownership.append("released")
                manager.child = Mock()
                # A transient inspection failure must retain the lock too.
                manager.child.alive.side_effect = [ManagedError("OWNED_PROCESS_IDENTITY_UNVERIFIABLE"), True, False]
                manager.request_drain = Mock(side_effect=lambda: self.assertEqual(ownership, ["held"]))
                manager._run_locked = Mock(side_effect=error)
                manager.sleep = Mock(side_effect=lambda _: self.assertEqual(ownership, ["held"]))
                with patch.object(agent, "HostLock", return_value=Lock()), patch.object(agent.signal, "signal"):
                    with self.assertRaises(type(error)):
                        manager.run()
                self.assertEqual(ownership, ["held", "released"])
                self.assertIsNone(manager.child)

    def test_startup_budget_counts_only_consecutive_failures(self):
        for outcomes, expected_backoff in (
            ([False] * 4, [1, 2, 3]),
            ([False] * 3 + [True] + [False] * 4, [1, 2, 3, 1, 2, 3]),
        ):
            with self.subTest(outcomes=outcomes), tempfile.TemporaryDirectory() as directory:
                manager, _, _ = self.setup_agent(directory)
                write_json(manager.data / "selected.json", self.select("a"))
                manager.recover = Mock()
                now = [0.]
                manager.clock = lambda: now[0]
                manager.sleep = lambda seconds: now.__setitem__(0, now[0] + max(60., seconds))
                sequence = iter(outcomes)
                def activate(*_):
                    if not next(sequence):
                        raise ManagedError("CANDIDATE_STARTUP_FAILED")
                    # A fully admitted runtime later exits; its next start has
                    # a fresh failure budget even after three prior failures.
                    manager.child = Mock()
                    manager.child.alive.return_value = False
                manager.activate = Mock(side_effect=activate)
                manager.status = Mock()
                self.assertEqual(manager._run_locked(), 78)
                backoff = [call.kwargs["failures"] for call in manager.status.call_args_list
                           if call.args[0] == "startup_backoff"]
                self.assertEqual(backoff, expected_backoff)
                self.assertEqual(manager.activate.call_count, len(outcomes))
                self.assertTrue(read_json(manager.data / "control.json")["halted"])


class ManagerHandoffTests(unittest.TestCase):
    def test_stable_launcher_restores_previous_unacknowledged_manager_and_preserves_pause(self):
        from mediaworker.managed.setup import LAUNCHER
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "host.json", {"service_user": "501"})
            previous = {"revision": "a" * 40, "configuration": {"capabilities": ["cutout"]}}
            write_json(root / "data/selected.json", {"revision": "b" * 40})
            write_json(root / "data/manager-handoff.json", {"candidate": "b" * 40, "previous": previous, "acknowledged": False})
            write_json(root / "data/control.json", {"paused": True, "halted": True})
            path = root / "clapclip-agent.py"
            path.write_text(LAUNCHER)
            child = Mock(); child.wait.return_value = 78
            with patch("subprocess.Popen", return_value=child), patch("signal.signal"), self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(path), run_name="__main__")
            self.assertEqual(stopped.exception.code, 75)
            self.assertEqual(read_json(root / "data/selected.json"), previous)
            self.assertTrue(read_json(root / "data/control.json")["paused"])
            self.assertFalse(read_json(root / "data/control.json")["halted"])

    def test_acknowledged_manager_failure_does_not_roll_back_accepted_release(self):
        from mediaworker.managed.setup import LAUNCHER
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "host.json", {"service_user": "501"})
            selected = {"revision": "b" * 40}
            write_json(root / "data/selected.json", selected)
            write_json(root / "data/manager-handoff.json", {"candidate": "b" * 40, "previous": {"revision": "a" * 40}, "acknowledged": True})
            path = root / "clapclip-agent.py"; path.write_text(LAUNCHER)
            child = Mock(); child.wait.return_value = 78
            with patch("subprocess.Popen", return_value=child), patch("signal.signal"), self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(path), run_name="__main__")
            self.assertEqual(stopped.exception.code, 78)
            self.assertEqual(read_json(root / "data/selected.json"), selected)


if __name__ == "__main__":
    unittest.main()
