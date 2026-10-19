import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mediaworker.downloader import runtime, worker


class RuntimeTests(unittest.TestCase):
    def config(self):
        return worker.Config()

    def ready(self, node="v24.6.0"):
        def output(args, **kwargs):
            return SimpleNamespace(stdout=(node if args[-1] == "--version" else
                "\n".join(" V..... "+c for c in ("libx264","libvpx-vp9","aac","libopus","libmp3lame"))
                if args[-1] == "-encoders" else "ffmpeg version test"))
        return (
            patch.object(runtime.sys, "version_info", (3, 14)),
            patch.object(runtime.metadata, "version", return_value="2026.8.19"),
            patch.object(runtime.shutil, "which", side_effect=lambda value: value),
            patch.object(runtime.subprocess, "run", side_effect=output),
        )

    def test_runtime_is_checked_without_api_or_gpu(self):
        a, b, c, d = self.ready()
        with a, b, c, d, patch.dict(os.environ, {"API_BASE_URL": "", "NODE_TOKEN": ""}):
            cfg = self.config()
            cfg.node = "node"
            result = runtime.check_runtime(cfg)
        self.assertEqual(result["status"], "ok")
        self.assertIn("yt_dlp", result)
        self.assertNotIn("api_key", str(cfg))

    def test_old_python_and_node_rejected(self):
        with patch.object(runtime.sys, "version_info", (3, 12)):
            with self.assertRaisesRegex(RuntimeError, "Python 3.13"):
                runtime.check_runtime(self.config())
        a, b, c, d = self.ready("v20.0.0")
        with a, b, c, d:
            cfg = self.config()
            cfg.node = "node"
            with self.assertRaisesRegex(RuntimeError, "Node.js 22"):
                runtime.check_runtime(cfg)

    def test_missing_ejs_or_tool_rejected(self):
        with patch.object(runtime.sys, "version_info", (3, 14)), patch.object(runtime.metadata, "version", side_effect=runtime.metadata.PackageNotFoundError):
            with self.assertRaisesRegex(RuntimeError, r"mediaworker\[downloader\]"):
                runtime.check_runtime(self.config())
        a, b, _, d = self.ready()
        with a, b, d, patch.object(runtime.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Install node"):
                runtime.check_runtime(self.config())


    def test_download_configuration_ignores_retired_server_settings(self):
        with patch.dict(os.environ, {"DOWNLOAD_API_KEY": "short-retired-key", "DOWNLOAD_FFMPEG": "custom-ffmpeg", "DOWNLOAD_PORT": "not-a-port", "DOWNLOAD_MAX_BYTES": "10000"}):
            cfg = worker.Config.from_env()
        self.assertEqual(cfg.ffmpeg, "custom-ffmpeg")
        self.assertFalse(hasattr(cfg, "port"))
        self.assertFalse(hasattr(cfg, "api_key"))
        self.assertFalse(hasattr(cfg, "max_bytes"))
        self.assertEqual((cfg.max_width,cfg.max_height,cfg.max_duration),(1280,720,300))

    def test_windows_taskkill_failure_falls_back_to_owned_process_handle(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        with patch.object(worker.os, "name", "nt"), patch.object(worker.subprocess, "run", side_effect=OSError):
            worker.stop_process(process)
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=10)


if __name__ == "__main__":
    unittest.main()
