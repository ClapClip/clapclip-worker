import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

import httpx
from mediaworker.config import Config
from mediaworker.engine.base import JobContext
from mediaworker.engine.download_engine import (
    DOWNLOAD_LIMITS,
    DownloadEngine,
    resolve_job_config,
)
from mediaworker.downloader.safety import ServiceError
from mediaworker import storage


class QueueEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = DownloadEngine(Config.from_env(load_env=False))
        self.proxy = patch("mediaworker.engine.download_engine.EgressProxy")
        self.runner = patch("mediaworker.engine.download_engine.ProcessRunner")
        self.proxy_mock = self.proxy.start()
        self.runner_factory = self.runner.start()
        self.runner_mock = self.runner_factory.return_value

    def tearDown(self):
        self.runner.stop()
        self.proxy.stop()
        self.temp.cleanup()

    def ctx(self, operation="info"):
        return JobContext("test-job", "download", {
            "operation": operation,
            "url": "https://www.youtube.com/watch?v=test",
            "limits": dict(DOWNLOAD_LIMITS),
        }, {}, self.root)

    def test_info_returns_opaque_formats_without_stream_urls(self):
        self.runner_mock.inspect.return_value = {"title": "Demo", "duration": 30, "formats": [
            {"format_id": "18", "ext": "mp4", "protocol": "https", "vcodec": "h264", "acodec": "aac", "width": 640, "height": 360, "url": "https://secret-stream"}]}
        ctx = self.ctx()
        result = self.engine.run(ctx)
        data = json.loads(result.output_path.read_text(encoding="utf-8"))
        self.assertEqual(result.content_type, "application/json")
        self.assertEqual(data["options"][data["formats"][0]["id"]]["components"], ["18"])
        self.assertNotIn("secret-stream", result.output_path.read_text())
        self.assertIs(self.runner_mock.inspect.call_args.args[2], ctx.cancel)
        self.proxy_mock.return_value.close.assert_called_once()

    def test_live_playlist_and_unknown_duration_fail_closed(self):
        for info in ({"_type": "playlist"}, {"is_live": True}, {"duration": None}):
            with self.subTest(info=info):
                self.runner_mock.inspect.return_value = info
                with self.assertRaises(RuntimeError):
                    self.engine.run(self.ctx())

    def test_failure_never_returns_tool_stderr(self):
        from mediaworker.downloader.safety import ServiceError
        source_error = ServiceError("MEDIA_UNAVAILABLE", "secret signed URL", 422)
        source_error.diagnostic_code = "HTTP_RATE_LIMITED"
        source_error.phase = "info"
        self.runner_mock.inspect.side_effect = source_error
        with self.assertRaisesRegex(RuntimeError, "^MEDIA_UNAVAILABLE$"):
            try:
                self.engine.run(self.ctx())
            except RuntimeError as error:
                self.assertEqual(error.diagnostic_code, "HTTP_RATE_LIMITED")
                self.assertEqual(error.phase, "info")
                raise
        self.proxy_mock.return_value.close.assert_called_once()

    def test_download_reuses_progress_cancellation_and_container_type(self):
        ctx = self.ctx("download")
        self.runner_mock.inspect.return_value = {"duration": 30, "formats": [
            {"format_id": "22", "ext": "webm", "protocol": "https", "vcodec": "vp9", "acodec": "opus", "width": 640, "height": 360}]}
        ctx.params["option"] = {"public": {"ext": "webm"}, "components": ["22"], "audio": False}
        self.runner_mock.download.return_value = self.root / "result.webm"
        self.runner_mock.output_details = (640, 360, 1.0)
        result = self.engine.run(ctx)
        self.assertEqual(result.content_type, "video/webm")
        self.assertIs(self.runner_mock.download.call_args.args[0]["cancel"], ctx.cancel)
        self.assertIs(self.runner_mock.download.call_args.args[-1], ctx.progress)

    def test_current_task_bounds_are_accepted_without_clamping(self):
        limits = dict(DOWNLOAD_LIMITS, maxHeight=240, maxWidth=426, maxDuration=90)
        effective = resolve_job_config(limits, self.engine.config)
        self.assertEqual((effective.max_height, effective.max_width, effective.max_duration), (240, 426, 90))
        for field, value in (("maxHeight", 721), ("maxWidth", 1281), ("maxDuration", 301)):
            with self.subTest(field=field), self.assertRaises(ServiceError):
                resolve_job_config(dict(DOWNLOAD_LIMITS, **{field:value}), self.engine.config)

    def test_only_media_upload_is_unbounded(self):
        self.assertIsNone(self.engine.max_result_bytes({"limits": dict(DOWNLOAD_LIMITS), "operation": "download"}))
        self.assertEqual(self.engine.max_result_bytes({"limits": dict(DOWNLOAD_LIMITS), "operation": "info"}), 512*1024)
        self.assertEqual(self.engine.max_result_bytes({"limits": {}}), 0)

    def test_missing_unknown_or_invalid_claim_limits_fail_closed(self):
        invalid = [
            None,
            {},
            {**DOWNLOAD_LIMITS, "version": 1},
            {**DOWNLOAD_LIMITS, "version": True},
            {**DOWNLOAD_LIMITS, "maxHeight": 0},
            {**DOWNLOAD_LIMITS, "maxWidth": True},
            {**DOWNLOAD_LIMITS, "requireDuration": False},
            {**DOWNLOAD_LIMITS, "requireKnownHeight": False},
            {**DOWNLOAD_LIMITS, "unexpected": 1},
        ]
        for contract in invalid:
            with self.subTest(contract=contract), self.assertRaises(ServiceError) as failure:
                resolve_job_config(contract, self.engine.config)
            self.assertEqual(failure.exception.code, "INVALID_LIMITS")

    def test_task_missing_limits_fails_before_proxy_or_extractor_start(self):
        ctx = self.ctx()
        ctx.params.pop("limits")
        with self.assertRaisesRegex(RuntimeError, "^INVALID_LIMITS$"):
            self.engine.run(ctx)
        self.proxy_mock.assert_not_called()
        self.runner_factory.assert_not_called()

    def test_task_limits_are_applied_to_its_runner_without_mutating_worker_config(self):
        ctx = self.ctx()
        ctx.params["limits"].update(maxHeight=240, maxWidth=426, maxDuration=90)
        self.runner_mock.inspect.return_value = {"title": "Demo", "duration": 30, "formats": [
            {"format_id": "18", "ext": "mp4", "protocol": "https", "vcodec": "h264", "acodec": "aac", "width": 426, "height": 240}]}
        original = (self.engine.config.max_height, self.engine.config.max_width,
                    self.engine.config.max_duration)
        self.engine.run(ctx)
        effective = self.runner_factory.call_args.args[0]
        self.assertEqual((effective.max_height, effective.max_width), (240, 426))
        self.assertEqual(effective.max_duration, 90)
        self.assertEqual((self.engine.config.max_height, self.engine.config.max_width,
                          self.engine.config.max_duration), original)

    def test_unknown_operation_cannot_execute(self):
        with self.assertRaises(ValueError):
            self.engine.run(self.ctx("exec"))


class DownloadWorkerLockTests(unittest.TestCase):
    def test_second_process_cannot_acquire_download_node_lock(self):
        import subprocess
        import sys
        from mediaworker.single_instance import FileLock, WorkerAlreadyRunning

        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "download-worker.lock"
            with FileLock(lock_path):
                probe = (
                    "from pathlib import Path; from mediaworker.single_instance import FileLock, WorkerAlreadyRunning; "
                    "lock=FileLock(Path(r'" + str(lock_path) + "')); "
                    "\ntry: lock.acquire()\nexcept WorkerAlreadyRunning: print('already-running')\nelse: print('unexpectedly-acquired'); lock.release()"
                )
                result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), "already-running")

            with FileLock(lock_path):
                self.assertTrue(lock_path.exists())


class StreamingUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_retry_reopens_stream_and_http_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "video.mp4"
            output.write_bytes(b"video")
            real_client = httpx.AsyncClient
            attempts = []
            class RetryTransport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    attempts.append(b"".join([chunk async for chunk in request.stream]))
                    if len(attempts) == 1:
                        raise httpx.ConnectError("network unavailable", request=request)
                    return httpx.Response(200)
            with patch("mediaworker.storage.httpx.AsyncClient", side_effect=lambda **kwargs: real_client(transport=RetryTransport(), **kwargs)), patch("mediaworker.storage.asyncio.sleep"):
                await storage.upload("https://s3.example.test/upload", output)
            self.assertEqual(attempts, [b"video", b"video"])
            attempts.clear()
            class DeniedTransport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    attempts.append(1)
                    return httpx.Response(403)
            with patch("mediaworker.storage.httpx.AsyncClient", side_effect=lambda **kwargs: real_client(transport=DeniedTransport(), **kwargs)):
                with self.assertRaises(httpx.HTTPStatusError):
                    await storage.upload("https://s3.example.test/upload", output)
            self.assertEqual(len(attempts), 1)

    async def test_upload_streams_chunks_and_known_length(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "video.mp4"
            data = b"x" * (1024 * 1024 + 3)
            output.write_bytes(data)
            received = []
            async def handler(request):
                self.assertEqual(request.headers["content-length"], str(len(data)))
                async for chunk in request.stream:
                    received.append(chunk)
                return httpx.Response(200)
            real_client = httpx.AsyncClient
            class StreamingTransport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    return await handler(request)
            with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read forbidden")), patch("mediaworker.storage.httpx.AsyncClient", side_effect=lambda **kwargs: real_client(transport=StreamingTransport(), **kwargs)):
                await storage.upload("https://s3.example.test/upload", output, "video/mp4", len(data))
            self.assertEqual(b"".join(received), data)
            self.assertGreater(len(received), 1)
