import asyncio
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mediaworker import deployment, runner


class DeploymentTests(unittest.TestCase):
    @contextmanager
    def config(self, directory, extra="", *, private=False):
        root = Path(directory).resolve()
        node = root / "state" / "new-node"
        text = (f"NODE_ID=new-node\nNODE_RUNTIME_DIR={node.as_posix()}\n"
                f"NODE_TOKEN_FILE={(node / 'node-token.json').as_posix()}\n"
                f"MODELS_DIR={(root / 'models').as_posix()}\n"
                f"ENHANCE_MODELS_DIR={(root / 'models/enhance').as_posix()}\n"
                "CAPABILITIES=faceswap,cutout,enhance,download\n")
        if private:
            text += ("API_BASE_URL=https://offline.test\nWORKER_COGNITO_USERNAME=synthetic-user\n"
                     "WORKER_COGNITO_PASSWORD=synthetic-secret\nWORKER_COGNITO_USER_POOL_ID=test-pool\n"
                     "WORKER_COGNITO_CLIENT_ID=test-client\n")
        env = root / "selected.env"
        env.write_text(text + extra, encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=root), deployment.configuration(env) as cfg:
            yield cfg

    def test_platform_rejects_new_python_free_threading_and_missing_wheel_architecture(self):
        with patch.object(deployment.sys, "version_info", (3, 14, 0)):
            self.assertIn("CPython 3.13", deployment.platform_problem())
        with patch.object(deployment.sysconfig, "get_config_var", return_value=1):
            self.assertIn("Free-threaded", deployment.platform_problem())
        with patch.object(deployment.sys, "platform", "win32"), patch.object(deployment.platform, "machine", return_value="ARM64"):
            self.assertIn("ARM64", deployment.platform_problem())
        with patch.object(deployment.sys, "platform", "linux"), patch.object(deployment.platform, "libc_ver", return_value=("musl", "1.2")):
            self.assertIn("musl", deployment.platform_problem())

    def test_macos_accepts_native_m_series_python(self):
        with patch.object(deployment.sys, "platform", "darwin"), \
             patch.object(deployment.platform, "machine", return_value="arm64"), \
             patch.object(deployment.platform, "mac_ver", return_value=("13.7", (), "")), \
             patch.object(deployment.subprocess, "run", return_value=SimpleNamespace(stdout="0")):
            self.assertIsNone(deployment.platform_problem())

    def test_macos_rejects_intel_without_suggesting_a_python_install(self):
        with patch.object(deployment.sys, "platform", "darwin"), \
             patch.object(deployment.platform, "machine", return_value="x86_64"), \
             patch.object(deployment.platform, "mac_ver", return_value=("13.7", (), "")), \
             patch.object(deployment.subprocess, "run", return_value=SimpleNamespace(stdout="0")):
            self.assertIn("Intel Macs are not supported", deployment.platform_problem())
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = root / "must-not-create"
                with self.assertRaisesRegex(SystemExit, "Intel Macs are not supported"):
                    deployment.install(root, directory)
                self.assertFalse(directory.exists())

    def test_macos_rejects_rosetta_with_native_python_instructions(self):
        with patch.object(deployment.sys, "platform", "darwin"), \
             patch.object(deployment.platform, "machine", return_value="x86_64"), \
             patch.object(deployment.platform, "mac_ver", return_value=("15.0", (), "")), \
             patch.object(deployment.subprocess, "run", return_value=SimpleNamespace(stdout="1")):
            message = deployment.platform_problem()
            self.assertIn("Rosetta", message)
            self.assertIn("natively as arm64", message)

    def test_configuration_does_not_read_default_dotenv_or_inherit_another_identity(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
            "NODE_ID": "old-node", "NODE_TOKEN": "synthetic-secret", "WORKER_AUTO_UPDATE": "true",
            "FACESWAP_MODEL_AUTO_DOWNLOAD": "true", "PATH": os.environ.get("PATH", "")}, clear=True), \
             patch.object(Path, "home", return_value=Path(temporary)), patch("dotenv.load_dotenv") as load:
            before = os.environ.copy()
            with deployment.configuration(None) as cfg:
                self.assertEqual(cfg.node_token, "")
                self.assertNotEqual(cfg.node_id, "old-node")
                self.assertFalse(cfg.auto_update or cfg.faceswap_model_auto_download or cfg.cutout_model_auto_download)
            self.assertEqual(dict(os.environ), before)
            load.assert_not_called()

    def test_configuration_does_not_expand_passwords_from_ambient_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.config(temporary, "WORKER_COGNITO_PASSWORD='${DO_NOT_EXPAND}'\n") as cfg:
                self.assertEqual(cfg.cognito_password, "${DO_NOT_EXPAND}")

    def test_fresh_examples_are_private_unique_and_never_overwrite_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env.portable"
            deployment.write_example(path, "new-identity", local=False)
            before = path.read_bytes()
            deployment.write_example(path, "other-identity", local=False)
            self.assertEqual(before, path.read_bytes())
            self.assertIn(b"NODE_RUNTIME_DIR=~/.mediaworker/nodes/new-identity", before)
            self.assertIn(b"NODE_TOKEN=\n", before)
            self.assertNotIn(b"enhance,", before)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_installer_refuses_unowned_environment_and_corrupt_marker_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(deployment, "platform_problem", return_value=None):
            root = Path(temporary).resolve()
            existing = root / "old venv"
            existing.mkdir()
            sentinel = existing / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "not managed"):
                deployment.install(root, existing)
            (existing / deployment.MARKER).write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "unreadable"):
                deployment.install(root, existing)
            self.assertEqual(sentinel.read_text(), "preserve")

    def test_installer_failure_is_rerunnable_and_space_paths_remain_single_arguments(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(deployment, "platform_problem", return_value=None):
            root = Path(temporary).resolve() / "source with spaces"
            root.mkdir()
            directory = root / "environment with spaces"
            def create(path):
                executable = deployment.python_in(Path(path))
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.touch()
            calls = []
            def execute(arguments, **kwargs):
                calls.append(arguments)
                return SimpleNamespace(returncode=0)
            with patch.object(deployment.venv.EnvBuilder, "create", side_effect=create), \
                 patch.object(deployment.subprocess, "run", side_effect=execute), \
                 patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(deployment.install(root, directory), 0)
                saved = (root / ".env.portable").read_bytes()
                self.assertEqual(deployment.install(root, directory), 0)
            self.assertEqual(saved, (root / ".env.portable").read_bytes())
            self.assertFalse((directory / ".setup-lock").exists())
            self.assertIn(str(root) + "[downloader]", [arg for call in calls for arg in call])
            self.assertTrue(all(call[0] == str(deployment.python_in(directory)) for call in calls))

    def test_partial_install_failure_keeps_config_and_releases_installer_lock(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(deployment, "platform_problem", return_value=None):
            root = Path(temporary).resolve()
            directory = root / "venv"
            directory.mkdir()
            (directory / deployment.MARKER).write_text(json.dumps({"profile": "portable-cp313", "source": str(root)}))
            interpreter = deployment.python_in(directory)
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.touch()
            config = root / ".env.portable"
            config.write_text("preserve synthetic config")
            with patch.object(deployment.subprocess, "run", side_effect=[SimpleNamespace(returncode=0), RuntimeError("synthetic failure")]):
                with self.assertRaises(RuntimeError):
                    deployment.install(root, directory)
            self.assertFalse((directory / ".setup-lock").exists())
            self.assertEqual(config.read_text(), "preserve synthetic config")

    def test_installer_rejects_symlink_parent_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(deployment, "platform_problem", return_value=None):
            root = Path(temporary).resolve()
            real = root / "existing"
            real.mkdir()
            link = root / "link"
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is unavailable for this test user")
            with self.assertRaisesRegex(SystemExit, "symlink"):
                deployment.install(root, link / "new environment")
            self.assertFalse((real / "new environment").exists())

    def test_authentication_requires_explicit_identity_and_separate_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.config(temporary, private=True) as cfg:
                self.assertEqual(deployment.authentication(cfg, explicit=False)["status"], "not_checked")
                self.assertEqual(deployment.authentication(cfg, explicit=True)["status"], "configured_not_authenticated")
                cfg.node_token_file.parent.mkdir(parents=True)
                cfg.node_token_file.write_text(json.dumps({"node_id": "another-node", "token": "synthetic-secret"}))
                report = deployment.authentication(cfg, explicit=True)
                self.assertEqual(report["status"], "node_cache_conflict")
                self.assertNotIn("synthetic-secret", json.dumps(report))

    def test_auth_error_hints_distinguish_password_permission_and_wrong_origin_offline(self):
        self.assertEqual(deployment.auth_failure("FORCE_CHANGE_PASSWORD")["status"], "account_setup_required")
        self.assertEqual(deployment.auth_failure("403")["status"], "access_not_authorized")
        self.assertEqual(deployment.auth_failure("404")["status"], "worker_api_not_found")
        self.assertNotIn("synthetic-secret", json.dumps(deployment.auth_failure("synthetic-secret")))

    def test_doctor_checks_download_config_and_does_not_confuse_coreml_with_enhancement(self):
        fake = SimpleNamespace(get_available_providers=lambda: ["CoreMLExecutionProvider", "CPUExecutionProvider"])
        with tempfile.TemporaryDirectory() as temporary:
            with self.config(temporary) as cfg, \
                 patch.object(deployment, "platform_problem", return_value=None), \
                 patch.object(deployment, "import_module", return_value=fake), \
                 patch.object(deployment, "model_inventory", return_value={"status": "missing", "missing": ["synthetic.onnx"]}), \
                 patch("mediaworker.downloader.runtime.check_runtime") as runtime, \
                 patch("mediaworker.client.bootstrap_node_token", new_callable=AsyncMock) as auth:
                report = deployment.inspect(cfg, mode="local", explicit=True)
                checked = runtime.call_args.args[0]
                self.assertTrue(hasattr(checked, "ffmpeg") and hasattr(checked, "node"))
                self.assertTrue(report["installation_ok"])
                self.assertFalse(report["all_configured_capabilities_ready"])
                self.assertEqual(report["ready_capabilities"], ["download"])
                self.assertEqual(report["capabilities"]["enhance"]["status"], "unsupported_provider")
                self.assertEqual(report["capabilities"]["faceswap"]["status"], "missing_models")
                auth.assert_not_awaited()

    def test_start_with_no_ready_capability_never_authenticates_or_enters_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.config(temporary, private=True) as cfg, \
                 patch.object(deployment, "inspect", return_value={"installation_ok": True, "capabilities": {"faceswap": {"status": "missing_models"}}}), \
                 patch.object(runner, "run_forever", new_callable=AsyncMock) as run, patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(deployment.start(cfg, mode="distributed", explicit=True, port=8765, env_file=None), 1)
                run.assert_not_awaited()

    def test_duplicate_portable_start_stops_before_model_loading_or_authentication(self):
        from mediaworker.single_instance import FileLock
        with tempfile.TemporaryDirectory() as temporary:
            with self.config(temporary, private=True) as cfg, \
                 FileLock(cfg.runtime_dir / "portable-worker.lock"), \
                 patch.object(deployment, "inspect") as inspect, patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(deployment.start(cfg, mode="distributed", explicit=True, port=8765, env_file=None), 1)
                inspect.assert_not_called()

    def test_portable_runtime_registers_only_loaded_engines_then_intersects_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.config(temporary, private=True) as cfg:
                def authenticate(config):
                    self.assertEqual(config.capabilities, ["faceswap", "download"])
                    config.capabilities = ["faceswap"]
                client = Mock()
                client.__aenter__ = AsyncMock(return_value=Mock())
                client.__aexit__ = AsyncMock(return_value=False)
                groups = Mock(return_value=[])
                with patch("mediaworker.model_registry.sync_models", return_value=cfg.capabilities), \
                     patch.object(runner, "build_registry", return_value={"faceswap": Mock(), "download": Mock()}), \
                     patch.object(runner, "bootstrap_node_token", new=AsyncMock(side_effect=authenticate)), \
                     patch.object(runner, "WorkerClient", return_value=client), \
                     patch.object(runner, "KeepAwake"), patch.object(runner, "_install_signal_handlers"), \
                     patch.object(runner, "runtime_capability_groups", groups):
                    asyncio.run(runner.run_forever(cfg, available_only=True))
                self.assertEqual(cfg.capabilities, ["faceswap"])
                self.assertEqual(groups.call_args.args[1], ["faceswap"])

    def test_local_workbench_skips_disabled_gpu_engine(self):
        from mediaworker.local_web import Workbench
        with tempfile.TemporaryDirectory() as temporary:
            with self.config(temporary, "CAPABILITIES=download\n") as cfg:
                engine = Mock()
                with patch.dict("mediaworker.engine.registry.ENGINE_FACTORIES", {"download": Mock(return_value=engine), "enhance": Mock(side_effect=AssertionError("must not load"))}):
                    app = Workbench(cfg, Path(temporary) / "work")
                    try:
                        app.load()
                        self.assertEqual(app.states["download"], "ready")
                        self.assertEqual(app.states["enhance"], "unavailable")
                    finally:
                        app.close()


if __name__ == "__main__":
    unittest.main()
