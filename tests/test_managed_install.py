"""OS adapters and complete setup ordering, entirely under temporary roots."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from mediaworker.managed import credentials, host, setup, releases
from mediaworker.managed.common import ManagedError, read_json, write_json
from mediaworker.single_instance import FileLock


class HostPlansTests(unittest.TestCase):
    def test_windows_boot_trigger_is_limited_service_and_has_no_password(self):
        with patch.object(host.sys, "platform", "win32"):
            plan = host.service_plan(Path("C:/ProgramData/ClapClipWorker"), Path("C:/tools/python.exe"), "S-1-5-19")
        tree = ElementTree.fromstring(plan.content)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertIsNotNone(tree.find("t:Triggers/t:BootTrigger", ns))
        self.assertIsNone(tree.find("t:Triggers/t:LogonTrigger", ns))
        self.assertEqual(tree.findtext("t:Principals/t:Principal/t:UserId", namespaces=ns), "S-1-5-19")
        self.assertEqual(tree.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=ns), "PT0S")
        self.assertNotIn(b"password", plan.content.lower())

    def test_macos_is_system_daemon_with_nonroot_user(self):
        pwd = SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_name="fixture-user"))
        with patch.object(host.sys, "platform", "darwin"), patch.dict(sys.modules, {"pwd": pwd}):
            plan = host.service_plan(Path("/Library/Application Support/ClapClipWorker"), Path("/tools/bin/python"), "501")
        value = plistlib.loads(plan.content)
        self.assertEqual(value["UserName"], "fixture-user")
        self.assertTrue(value["RunAtLoad"])
        self.assertIn("LaunchDaemons", str(plan.path))
        self.assertNotIn("LaunchAgents", str(plan.path))

    def test_linux_dynamic_state_is_separate_from_protected_host_root(self):
        with patch.object(host.sys, "platform", "linux"):
            plan = host.service_plan(Path("/var/lib/clapclip-worker"), Path("/tools/bin/python"), "dynamic")
        text = plan.content.decode()
        self.assertIn("StateDirectory=clapclip-worker-runtime\n", text)
        self.assertNotIn("StateDirectory=clapclip-worker\n", text)
        self.assertIn("WantedBy=multi-user.target", text)
        self.assertIn("TimeoutStopSec=infinity", text)

    def test_failed_service_enable_retries_even_with_definition_left_on_disk(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(host, "administrator", return_value=True), patch.object(host.sys, "platform", "linux"):
            plan = host.ServicePlan(Path(directory) / "worker.service", b"fixture", (("enable",),), ("start",), ())
            with patch.object(host, "run_checked", side_effect=ManagedError("OS_SERVICE_COMMAND_FAILED")):
                with self.assertRaisesRegex(ManagedError, "BOOT_REGISTRATION_FAILED"):
                    host.install_service(plan)
            self.assertTrue(plan.path.is_file())
            with patch.object(host, "run_checked") as run:
                host.install_service(plan)
                run.assert_called_once_with(["enable"])

    def test_foreign_definition_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(host, "administrator", return_value=True):
            path = Path(directory) / "worker.service"
            path.write_bytes(b"foreign")
            plan = host.ServicePlan(path, b"ours", (), (), ())
            with self.assertRaises(ManagedError), patch.object(host, "run_checked") as run:
                host.install_service(plan)
            run.assert_not_called()
            self.assertEqual(path.read_bytes(), b"foreign")

    def test_windows_conda_and_venv_interpreters_are_different_paths(self):
        with patch.object(releases.sys, "platform", "win32"):
            self.assertEqual(releases.tools_python(Path("tools")), Path("tools/python.exe"))
            self.assertEqual(releases.venv_python(Path("venv")), Path("venv/Scripts/python.exe"))


class CredentialTests(unittest.TestCase):
    def test_secret_never_appears_in_repr(self):
        self.assertNotIn("synthetic-password", repr(credentials.Login("fixture", "synthetic-password")))

    def test_systemd_encrypt_uses_stdin_and_keeps_prior_file_when_refused(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(credentials.sys, "platform", "linux"), patch.object(credentials.shutil, "which", return_value="systemd-creds"):
            root = Path(directory)
            path = root / "credential.enc"
            path.write_bytes(b"existing-ciphertext")
            with patch.object(credentials.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run:
                with self.assertRaises(ManagedError):
                    credentials.store(root, credentials.Login("fixture", "synthetic-password"))
            self.assertNotIn("synthetic-password", repr(run.call_args.args))
            self.assertIn(b"synthetic-password", run.call_args.kwargs["input"])
            self.assertEqual(path.read_bytes(), b"existing-ciphertext")

    @unittest.skipUnless(sys.platform == "win32", "Native Windows DPAPI roundtrip")
    def test_native_machine_dpapi_roundtrip_without_service_or_credentials(self):
        secret = b"synthetic-only-not-a-real-credential"
        encrypted = credentials._dpapi(secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(credentials._dpapi(encrypted, decrypt=True), secret)

    def test_unknown_credential_kind_is_not_read_as_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ManagedError):
                credentials.load(Path(directory), {"kind": "unknown", "path": "credential"})


class InstallerTests(unittest.TestCase):
    def installer(self, root, *, discovery_error=False):
        (root / "bootstrap-owner.txt").write_text("clapclip-bootstrap-v1")
        release = Mock()
        release.latest.return_value = "a" * 40
        release.prepare.return_value = root / "release"
        api = Mock()
        api.account_binding.return_value = "synthetic-binding"
        factory = Mock(return_value=api)
        if discovery_error:
            factory.discover.side_effect = ManagedError("BOOTSTRAP_NOT_DEPLOYED_OR_UNAVAILABLE")
        return release, factory

    def scope(self):
        stack = ExitStack()
        stack.enter_context(patch.object(setup, "administrator", return_value=True))
        stack.enter_context(patch.object(setup, "service_user", return_value="501"))
        stack.enter_context(patch.object(setup, "protect_new_root"))
        stack.enter_context(patch.object(setup, "handoff_data"))
        stack.enter_context(patch.object(setup, "require_no_other_worker"))
        stack.enter_context(patch.object(setup, "configured_origin", return_value="https://offline.test"))
        stack.enter_context(patch.object(setup, "service_plan", return_value=SimpleNamespace(start=("synthetic-start",))))
        stack.enter_context(patch.object(setup, "run_checked"))
        stack.enter_context(patch("mediaworker.managed.control.status", return_value={"background_alive": True, "queue_poll_accepted": True}))
        return stack

    def test_missing_real_api_stops_before_password_store_or_boot_registration(self):
        with tempfile.TemporaryDirectory() as directory, self.scope(), patch.object(setup, "store") as store, patch.object(setup, "install_service") as service:
            root = Path(directory)
            release, factory = self.installer(root, discovery_error=True)
            prompt = Mock()
            with self.assertRaisesRegex(ManagedError, "BOOTSTRAP_NOT_DEPLOYED_OR_UNAVAILABLE"):
                setup.install(root, releases=release, api_factory=factory, prompt=prompt)
            prompt.assert_not_called(); store.assert_not_called(); service.assert_not_called()

    def test_complete_install_uses_one_node_and_readiness_before_service(self):
        with tempfile.TemporaryDirectory() as directory, self.scope(), patch.object(setup, "store", return_value={"kind": "synthetic"}), patch.object(setup, "install_service") as service:
            root = Path(directory)
            release, factory = self.installer(root)
            value = {"capabilities": ["cutout"], "model_set": "synthetic"}
            def prepared(*args, **kwargs):
                service.assert_not_called()
                self.assertFalse((root / "clapclip-agent.py").exists())
                return value
            with patch.object(setup, "prepare_configuration", side_effect=prepared):
                report = setup.install(root, releases=release, api_factory=factory, prompt=lambda: credentials.Login("fixture", "synthetic-password"))
            self.assertTrue(report["background_alive"])
            service.assert_called_once()
            node = read_json(root / "host.json")["node_id"]
            self.assertTrue(node.startswith("worker-"))
            self.assertNotIn("synthetic-password", (root / "host.json").read_text())
            self.assertEqual(read_json(root / "data/selected.json")["configuration"], value)

    def test_competing_installer_cannot_write_node_or_prompt(self):
        with tempfile.TemporaryDirectory() as directory, self.scope():
            root = Path(directory)
            release, factory = self.installer(root)
            prompt = Mock()
            with FileLock(root / "install.lock"):
                with self.assertRaisesRegex(ManagedError, "INSTALL_ALREADY_RUNNING"):
                    setup.install(root, releases=release, api_factory=factory, prompt=prompt)
            prompt.assert_not_called()
            self.assertFalse((root / "host.json").exists())

    def test_native_temporary_venv_manager_parent_family(self):
        # This starts only a tiny local identity probe, never Agent/Worker/API.
        import venv
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv.EnvBuilder(with_pip=False).create(root / "venv")
            output = root / "family.json"
            from mediaworker.managed import processes
            dependencies = str(Path(processes.psutil.__file__).parents[1])
            source = str(Path(__file__).parents[1])
            probe = root / "probe.py"
            probe.write_text("import sys,json,os\nfrom pathlib import Path\n" +
                f"sys.path[:0]={[source, dependencies]!r}\n" +
                "from mediaworker.managed.processes import manager_family\n" +
                f"Path({str(output)!r}).write_text(json.dumps({{'family':list(manager_family(Path({str(root)!r}))), 'pid':os.getpid(), 'parent':os.getppid()}}))\n")
            launcher = root / "clapclip-agent.py"
            python = releases.venv_python(root / "venv")
            launcher.write_text("import subprocess,sys\n" + f"raise SystemExit(subprocess.call({[str(python), '-B', str(probe)]!r}))\n")
            # Match the real Windows service's pythonw stable launcher, then
            # the venv python.exe shim and its actual base Python child.
            launcher_python = Path(getattr(sys, "_base_executable", sys.executable))
            if sys.platform == "win32":
                launcher_python = launcher_python.with_name("pythonw.exe")
            result = subprocess.run([str(launcher_python), "-B", str(launcher)], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            value = read_json(output)
            self.assertIn(value["pid"], value["family"])
            self.assertIn(value["parent"], value["family"])
            self.assertGreaterEqual(len(value["family"]), 2)


if __name__ == "__main__":
    unittest.main()
