"""Distinguish OS registration, verified live identity and historical receipts."""
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mediaworker.managed import control, host
from mediaworker.managed.common import read_json, write_json
from mediaworker.managed.host import Process
from mediaworker.managed.releases import PROFILE


class ControlEvidenceTests(unittest.TestCase):
    def state(self, root):
        write_json(root / "host.json", {"owner": "clapclip-managed-v1", "profile": PROFILE,
                   "node_id": "fixture-node", "service_user": "501", "credential": {"kind": "synthetic"}})
        manager = Process(100, 1, 100., "synthetic-manager", "owned")
        child = Process(200, 100, 101., "synthetic-child", "owned")
        write_json(root / "data/selected.json", {"revision": "a" * 40})
        write_json(root / "data/agent.json", {"stage": "running", "session": "fixture", "manager": asdict(manager)})
        directory = root / "data/sessions/fixture"
        write_json(directory / "processes.json", {"identities": [asdict(child)]})
        write_json(directory / "worker.json", {"session": "fixture", "revision": "a" * 40, "pid": 200,
                   "capabilities": ["download"], "accepted_poll_groups": [["download"]], "last_poll_accepted": time.time() - 500})
        return directory

    def scope(self, root):
        stack = ExitStack()
        plan = SimpleNamespace(path=root / "definition", content=b"fixture")
        plan.path.write_bytes(plan.content)
        stack.enter_context(patch.object(control, "service_plan", return_value=plan))
        stack.enter_context(patch.object(control, "service_running", return_value=True))
        stack.enter_context(patch.object(host, "service_registered", return_value=False))
        return stack

    def test_dead_or_recycled_manager_does_not_promote_stale_worker_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.state(root)
            with self.scope(root), patch.object(control, "same_process", side_effect=lambda p: p.pid == 200):
                report = control.status(root)
            self.assertTrue(report["boot_definition_present"])
            self.assertFalse(report["boot_registered_enabled"])
            self.assertFalse(report["background_alive"])
            self.assertFalse(report["registered"])
            self.assertFalse(report["queue_poll_accepted"])

    def test_live_session_has_historical_receipt_but_old_poll_is_not_recent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.state(root)
            with self.scope(root), patch.object(control, "same_process", return_value=True):
                report = control.status(root)
            self.assertTrue(report["background_alive"])
            self.assertTrue(report["registered"])
            self.assertTrue(report["queue_poll_accepted"])
            self.assertFalse(report["poll_recent"])
            self.assertEqual(report["media_task_success"], "not_verified")

    def test_other_session_or_revision_cannot_prove_live_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = self.state(root) / "worker.json"
            value = read_json(path)
            write_json(path, {**value, "session": "previous-session"})
            with self.scope(root), patch.object(control, "same_process", return_value=True):
                self.assertFalse(control.status(root)["background_alive"])

    def test_local_definition_alone_is_not_a_real_os_boot_registration(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(host.sys, "platform", "linux"):
            path = Path(directory) / "fixture.service"
            plan = host.ServicePlan(path, b"fixture", (), (), ())
            path.write_bytes(plan.content)
            with patch.object(host, "run_checked", return_value=SimpleNamespace(stdout=b"LoadState=loaded\nUnitFileState=disabled\nFragmentPath=/other/path\n")):
                self.assertFalse(host.service_registered(plan))


if __name__ == "__main__":
    unittest.main()
