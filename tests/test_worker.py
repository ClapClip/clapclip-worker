from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import hashlib
import json
import logging
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zlib

os.environ["MEDIAWORKER_LOAD_DOTENV"] = "0"

import httpx
import numpy as np
from PIL import Image

from mediaworker.client import LeaseLost, WorkerClient, _read_shared_token, _write_shared_token, bootstrap_node_token
from mediaworker.config import Config
from mediaworker.engine.base import Engine as BaseEngine, JobContext, RunResult
from mediaworker.images import validate_image
from mediaworker.models import ClaimedJob
from mediaworker import lifecycle, model_registry, runner, scheduling, storage

JOB_ID = "a86eeb49-a664-4249-bfb7-20c6001be743"
LEASE = "d8c5b4a4-feb2-490a-a98d-f3965c91a47a"


def job():
    return ClaimedJob(id=JOB_ID, lease_token=LEASE, attempt=1, task_type="faceswap", inputs={},
                      result={"put_url": "https://offline.test/put", "key": "attempt/result.jpg"})


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config.from_env(load_env=False)
        self.cfg.work_dir = Path(self.tmp.name)
        self.cfg.api_base_url = "https://offline.test"
        self.cfg.node_token = "offline"
        self.cfg.faceswap_model_repository = "example/models"

    def test_concurrency_is_limited_to_three_jobs(self):
        with patch.dict(os.environ, {"CONCURRENCY": "9"}):
            self.assertEqual(Config.from_env(load_env=False).concurrency, 3)

    def test_default_capabilities_include_cutout_and_enhance(self):
        with patch.dict(os.environ, {"CAPABILITIES": ""}):
            cfg = Config.from_env(load_env=False)
        self.assertEqual(cfg.capabilities, ["faceswap", "cutout", "enhance", "download"])
        self.assertIsNone(cfg.claim_capabilities)
        self.assertEqual(cfg.cutout_model, "isnet-general-use")

    def test_shared_runtime_limits_all_worker_processes_to_three_active_jobs(self):
        runtime_dir = Path(self.tmp.name) / "runtime"
        slots = [scheduling.acquire_worker_slot(runtime_dir) for _ in range(4)]
        self.assertTrue(all(slots[:3]))
        self.assertIsNone(slots[3])
        slots[1].release()
        replacement = scheduling.acquire_worker_slot(runtime_dir)
        self.assertIsNotNone(replacement)
        for slot in (*slots[:3], replacement):
            if slot:
                slot.release()

    def test_download_and_image_pollers_stay_separate_under_one_node(self):
        groups = scheduling.worker_capability_groups(
            ["faceswap", "cutout", "enhance", "download"], 3
        )
        self.assertEqual(groups, [
            ["faceswap", "cutout", "enhance"],
            ["faceswap", "cutout", "enhance"],
            ["faceswap", "cutout", "enhance"],
            ["download"],
        ])

    async def test_workers_reuse_the_shared_token_rotated_by_another_runtime(self):
        self.cfg.node_token_file = self.cfg.work_dir / "node-token.json"
        self.cfg.cognito_username = "worker@example.test"
        self.cfg.cognito_password = "test-password"
        _write_shared_token(self.cfg, "fresh-token", time.time() + 3600)
        with patch("mediaworker.client._cognito_id_token", side_effect=AssertionError("must not rotate twice")):
            token = await bootstrap_node_token(self.cfg, force=True, previous_token="expired-token")
        self.assertEqual(token, "fresh-token")
        cached = _read_shared_token(self.cfg)
        self.assertIsNotNone(cached)
        self.assertEqual(cached[0], "fresh-token")
        self.assertGreater(cached[1], time.time())

    async def test_shared_node_login_is_order_independent_for_image_and_download_runtimes(self):
        real_async_client = httpx.AsyncClient
        for first_runtime in ("image", "download"):
            with self.subTest(first_runtime=first_runtime), tempfile.TemporaryDirectory() as directory:
                login_requests = []

                def login_handler(request):
                    login_requests.append(json.loads(request.content))
                    return httpx.Response(200, json={
                        "nodeId": "WORKER-EXAMPLE", "nodeToken": "shared-token",
                        "tier": "trusted", "capabilities": ["faceswap", "cutout", "enhance", "download"],
                        "expiresIn": 3600,
                    })

                transport = httpx.MockTransport(login_handler)
                image_cfg = Config.from_env(load_env=False)
                download_cfg = Config.from_env(load_env=False)
                for cfg in (image_cfg, download_cfg):
                    cfg.api_base_url = "https://worker.example.test"
                    cfg.node_id = "WORKER-EXAMPLE"
                    cfg.node_token = ""
                    cfg.node_token_file = Path(directory) / "node-token.json"
                    cfg.cognito_username = "worker@example.test"
                    cfg.cognito_password = "test-password"
                    cfg.capabilities = ["faceswap", "cutout", "enhance", "download"]
                image_cfg.claim_capabilities = ["faceswap", "cutout", "enhance"]
                download_cfg.claim_capabilities = ["download"]

                def client_factory(**kwargs):
                    return real_async_client(transport=transport, **kwargs)

                order = (image_cfg, download_cfg) if first_runtime == "image" else (download_cfg, image_cfg)
                with patch("mediaworker.client.httpx.AsyncClient", side_effect=client_factory), \
                     patch("mediaworker.client._cognito_id_token", return_value="verified-id-token"):
                    tokens = await asyncio.gather(*(bootstrap_node_token(cfg) for cfg in order))
                self.assertEqual(login_requests, [{
                    "idToken": "verified-id-token", "nodeId": "WORKER-EXAMPLE",
                    "capabilities": ["faceswap", "cutout", "enhance", "download"],
                }])
                self.assertEqual(tokens, ["shared-token", "shared-token"])

                image_client, download_client = WorkerClient(image_cfg), WorkerClient(download_cfg)
                self.addAsyncCleanup(image_client._http.aclose)
                self.addAsyncCleanup(download_client._http.aclose)
                image_post = AsyncMock(return_value={"job": None})
                download_post = AsyncMock(return_value={"job": None})
                with patch.object(image_client, "_post", new=image_post), \
                     patch.object(download_client, "_post", new=download_post):
                    await image_client.claim(image_cfg.claim_capabilities, [])
                    await download_client.claim(download_cfg.claim_capabilities, [])
                self.assertEqual(image_post.await_args.args[0], "/api/worker/claim")
                self.assertEqual(download_post.await_args.args[0], "/api/worker/download/claim")
                self.assertEqual(image_post.await_args.args[1]["node_id"], download_post.await_args.args[1]["node_id"])
                self.assertEqual(image_client._http.headers["Authorization"], download_client._http.headers["Authorization"])

    async def test_revoked_shared_node_token_blocks_both_task_namespaces(self):
        with tempfile.TemporaryDirectory() as directory:
            self.cfg.api_base_url = "https://worker.example.test"
            self.cfg.node_id = "WORKER-EXAMPLE"
            self.cfg.node_token = "revoked-token"
            self.cfg.node_token_file = Path(directory) / "node-token.json"
            self.cfg.cognito_username = "worker@example.test"
            self.cfg.cognito_password = "test-password"
            _write_shared_token(self.cfg, "revoked-token", time.time() + 3600)
            requests = []

            def handler(request):
                requests.append((request.url.path, request.headers.get("Authorization")))
                return httpx.Response(403 if request.url.path == "/api/worker/login" else 401, json={})

            transport = httpx.MockTransport(handler)
            real_async_client = httpx.AsyncClient
            image_client = WorkerClient(self.cfg)
            download_cfg = Config.from_env(load_env=False)
            download_cfg.api_base_url = self.cfg.api_base_url
            download_cfg.node_id = self.cfg.node_id
            download_cfg.node_token = self.cfg.node_token
            download_cfg.node_token_file = self.cfg.node_token_file
            download_cfg.cognito_username = self.cfg.cognito_username
            download_cfg.cognito_password = self.cfg.cognito_password
            download_client = WorkerClient(download_cfg)
            self.addAsyncCleanup(image_client._http.aclose)
            self.addAsyncCleanup(download_client._http.aclose)
            await image_client._http.aclose()
            await download_client._http.aclose()
            image_client._http = real_async_client(base_url=self.cfg.api_base_url,
                headers={"Authorization": "Bearer revoked-token"}, transport=transport)
            download_client._http = real_async_client(base_url=self.cfg.api_base_url,
                headers={"Authorization": "Bearer revoked-token"}, transport=transport)
            with patch("mediaworker.client.httpx.AsyncClient", side_effect=lambda **kwargs: real_async_client(transport=transport, **kwargs)), \
                 patch("mediaworker.client._cognito_id_token", return_value="verified-id-token"):
                results = await asyncio.gather(
                    image_client.claim(["faceswap"], []),
                    download_client.claim(["download"], []),
                    return_exceptions=True,
                )
            self.assertTrue(all(isinstance(result, httpx.HTTPStatusError) for result in results))
            self.assertEqual(sorted(path for path, _ in requests), sorted([
                "/api/worker/claim", "/api/worker/login",
                "/api/worker/download/claim", "/api/worker/login",
            ]))
            self.assertTrue(all(auth == "Bearer revoked-token" for path, auth in requests if path.endswith("/claim")))

    async def test_shared_token_cache_rejects_a_different_node_or_api(self):
        self.cfg.node_token_file = self.cfg.work_dir / "node-token.json"
        self.cfg.node_token = ""
        self.cfg.cognito_username = "worker@example.test"
        self.cfg.cognito_password = "test-password"
        _write_shared_token(self.cfg, "shared-token", time.time() + 3600)
        self.cfg.node_id = "another-node"
        with self.assertRaisesRegex(RuntimeError, "different node or API origin"):
            await bootstrap_node_token(self.cfg)

    async def test_download_runtime_claims_only_its_capability_under_the_shared_node_id(self):
        self.cfg.node_id = "WORKER-EXAMPLE"
        self.cfg.capabilities = ["faceswap", "cutout", "enhance", "download"]
        client = WorkerClient(self.cfg)
        self.addAsyncCleanup(client._http.aclose)
        with patch.object(client, "_post", new=AsyncMock(return_value={"job": None})) as post:
            await client.claim(["download"], [])
        self.assertEqual(post.await_args.args[0], "/api/worker/download/claim")
        payload = post.await_args.args[1]
        self.assertEqual(payload["node_id"], "WORKER-EXAMPLE")
        self.assertEqual(payload["capabilities"], ["download"])
        self.assertEqual(self.cfg.capabilities, ["faceswap", "cutout", "enhance", "download"])

    async def test_download_job_lease_writes_use_the_download_namespace(self):
        client = WorkerClient(self.cfg)
        self.addAsyncCleanup(client._http.aclose)
        client._job_routes[JOB_ID] = self.cfg.download_api_prefix
        client._job_routes["download-job"] = self.cfg.download_api_prefix
        with patch.object(client, "_post", new=AsyncMock(return_value={})) as post:
            await client.heartbeat(JOB_ID, LEASE, progress=25)
            await client.complete(JOB_ID, LEASE, "download/result.mp4", 1, 1)
            await client.fail("download-job", LEASE, "MEDIA_UNAVAILABLE")
        self.assertEqual(
            [call.args[0] for call in post.await_args_list],
            ["/api/worker/download/heartbeat", "/api/worker/download/complete", "/api/worker/download/fail"],
        )

    def test_registry_builds_mock_engines_for_each_known_capability(self):
        from mediaworker.engine.registry import build_registry

        with patch.dict(os.environ, {"MEDIAWORKER_MOCK": "1"}):
            registry = build_registry(
                self.cfg,
                ["faceswap", "cutout", "enhance", "unknown"],
            )
        self.assertEqual(set(registry), {"faceswap", "cutout", "enhance"})
        self.assertEqual(registry["cutout"].task_type, "cutout")

    def test_cutout_engine_reuses_session_and_writes_rgba_png(self):
        from mediaworker.engine.cutout_engine import CutoutEngine

        source = self.cfg.work_dir / "photo.jpg"
        Image.new("RGB", (12, 8), (220, 40, 80)).save(source)
        self.cfg.cutout_models_dir = self.cfg.work_dir / "cutout-models"
        self.cfg.cutout_models_dir.mkdir()
        (self.cfg.cutout_models_dir / "isnet-general-use.onnx").write_bytes(b"model")
        prediction = np.linspace(0, 1, 1024 * 1024, dtype=np.float32).reshape(
            1, 1, 1024, 1024
        )
        session = SimpleNamespace(
            get_providers=lambda: ["CPUExecutionProvider"],
            get_inputs=lambda: [SimpleNamespace(name="input")],
            run=Mock(return_value=[prediction]),
        )
        engine = CutoutEngine(self.cfg)
        context = JobContext(
            "cutout-test",
            "cutout",
            {},
            {"image": source},
            self.cfg.work_dir,
        )
        with (
            patch(
                "mediaworker.engine.cutout_engine.resolve_providers",
                return_value=["CPUExecutionProvider"],
            ),
            patch("mediaworker.engine.cutout_engine.session_options"),
            patch("onnxruntime.InferenceSession", return_value=session) as create,
        ):
            engine.load()
            first = engine.run(context)
            second = engine.run(context)

        self.assertEqual(engine.available_models, ["isnet-general-use"])
        self.assertEqual((first.width, first.height, first.content_type), (12, 8, "image/png"))
        self.assertEqual(first.output_path, second.output_path)
        with Image.open(first.output_path) as output:
            self.assertEqual(output.mode, "RGBA")
            low, high = output.getchannel("A").getextrema()
            self.assertLess(low, 20)
            self.assertGreater(high, 235)
        create.assert_called_once()
        self.assertEqual(session.run.call_count, 2)

    def test_cutout_engine_requires_named_image_input(self):
        from mediaworker.engine.cutout_engine import CutoutEngine

        engine = CutoutEngine(self.cfg)
        engine._session = object()
        context = JobContext("cutout-test", "cutout", {}, {}, self.cfg.work_dir)
        with self.assertRaisesRegex(ValueError, "image input"):
            engine.run(context)


    def test_cutout_preserves_existing_transparency(self):
        from mediaworker.engine.cutout_engine import CutoutEngine

        source = self.cfg.work_dir / "transparent.png"
        image = Image.new("RGBA", (3, 1), (100, 120, 140, 255))
        image.putalpha(Image.fromarray(np.array([[0, 128, 255]], dtype=np.uint8)))
        image.save(source)
        engine = CutoutEngine(self.cfg)
        engine._session = object()
        with patch.object(engine, "_mask", return_value=Image.new("L", (3, 1), 128)):
            result = engine.run(JobContext("cutout", "cutout", {}, {"image": source}, self.cfg.work_dir))
        with Image.open(result.output_path) as output:
            np.testing.assert_array_equal(np.asarray(output.getchannel("A")), [[0, 64, 128]])
            self.assertEqual(output.getpixel((2, 0))[:3], (100, 120, 140))

    def test_cutout_mask_rejects_nonfinite_and_malformed_predictions(self):
        from mediaworker.engine.cutout_engine import CutoutEngine

        engine = CutoutEngine(self.cfg)
        session = SimpleNamespace(get_inputs=lambda: [SimpleNamespace(name="input")], run=Mock())
        engine._session = session
        for prediction in [
            np.full((1, 1, 1024, 1024), np.nan, dtype=np.float32),
            np.full((1, 1, 1024, 1024), np.inf, dtype=np.float32),
            np.zeros((1, 1024, 1024), dtype=np.float32),
        ]:
            with self.subTest(shape=prediction.shape):
                session.run.return_value = [prediction]
                with self.assertRaisesRegex(ValueError, "invalid mask"):
                    engine._mask(Image.new("RGB", (8, 6)))

    def test_cutout_uniform_masks_remain_finite_and_preserve_foreground(self):
        from mediaworker.engine.cutout_engine import CutoutEngine

        engine = CutoutEngine(self.cfg)
        session = SimpleNamespace(get_inputs=lambda: [SimpleNamespace(name="input")], run=Mock())
        engine._session = session
        for value, expected in [(0, 0), (1, 255)]:
            with self.subTest(value=value):
                session.run.return_value = [np.full((1, 1, 1024, 1024), value, dtype=np.float32)]
                mask = engine._mask(Image.new("RGB", (8, 6), "black"))
                self.assertEqual(mask.getextrema(), (expected, expected))
                self.assertTrue(np.isfinite(session.run.call_args.args[1]["input"]).all())

    def test_cutout_serializes_shared_directml_inference(self):
        from mediaworker.engine.cutout_engine import CutoutEngine

        engine = CutoutEngine(self.cfg)
        entered = threading.Event()
        release = threading.Event()
        active = 0
        peak = 0
        guard = threading.Lock()

        def infer(*_):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test inference timed out")
            with guard:
                active -= 1
            return [np.ones((1, 1, 1024, 1024), dtype=np.float32)]

        engine._session = SimpleNamespace(get_inputs=lambda: [SimpleNamespace(name="input")], run=infer)
        engine._serialize_runs = True
        barrier = threading.Barrier(3)

        def run_mask():
            barrier.wait(timeout=5)
            return engine._mask(Image.new("RGB", (8, 6)))

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(run_mask) for _ in range(3)]
            try:
                self.assertTrue(entered.wait(5))
                time.sleep(0.15)
            finally:
                release.set()
            for future in futures:
                self.assertEqual(future.result(timeout=5).size, (8, 6))
        self.assertEqual(peak, 1)

    def test_model_preparation_keeps_healthy_capabilities_when_one_fails(self):
        self.cfg.capabilities = ["faceswap", "cutout"]
        for failed, remaining in [("faceswap", "cutout"), ("cutout", "faceswap")]:
            with (
                self.subTest(failed=failed),
                patch.object(model_registry, "sync_faceswap_models", side_effect=RuntimeError("offline") if failed == "faceswap" else None),
                patch.object(model_registry, "sync_cutout_model", side_effect=RuntimeError("offline") if failed == "cutout" else None),
                self.assertLogs("mediaworker.model_registry", level="ERROR"),
            ):
                self.assertEqual(model_registry.sync_models(self.cfg), [remaining])

    async def test_cutout_queue_uploads_png_with_the_claimed_lease_and_dimensions(self):
        from mediaworker.engine.cutout_engine import CutoutEngine

        source = self.cfg.work_dir / "source.png"
        Image.new("RGB", (12, 8), "blue").save(source)
        engine = CutoutEngine(self.cfg)
        engine._session = object()
        claimed = ClaimedJob(
            id=JOB_ID, lease_token=LEASE, attempt=1, task_type="cutout",
            params={"outputFormat": "png"}, inputs={"image": "https://offline.test/source"},
            result={"put_url": "https://offline.test/put", "key": "attempt/result.png", "content_type": "image/png"},
        )
        client = SimpleNamespace(heartbeat=AsyncMock(), complete=AsyncMock(), fail=AsyncMock())

        async def upload(url, path, content_type, max_bytes):
            self.assertEqual(url, claimed.result.put_url)
            self.assertEqual(content_type, "image/png")
            with Image.open(path) as image:
                self.assertEqual((image.format, image.mode, image.size), ("PNG", "RGBA", (12, 8)))

        with (
            patch.object(engine, "_mask", return_value=Image.new("L", (12, 8), 128)),
            patch.object(storage, "download", AsyncMock(return_value=source)),
            patch.object(storage, "upload", AsyncMock(side_effect=upload)) as put,
        ):
            await lifecycle._process_job(self.cfg, client, {"cutout": engine}, claimed)
        put.assert_awaited_once()
        client.complete.assert_awaited_once_with(JOB_ID, LEASE, "attempt/result.png", 12, 8)
        client.fail.assert_not_awaited()
        self.assertFalse((self.cfg.work_dir / JOB_ID).exists())

    async def test_transient_heartbeat_failure_does_not_end_loop(self):
        client = SimpleNamespace(heartbeat=AsyncMock(side_effect=[TimeoutError("temporary"), None, None]))
        stop, lost = asyncio.Event(), asyncio.Event()
        small_job = SimpleNamespace(id=JOB_ID, lease_token=LEASE, lease_seconds=2)
        task = asyncio.create_task(lifecycle._heartbeat_loop(client, small_job, stop, lost))
        await asyncio.sleep(1.4)
        stop.set()
        await task
        self.assertGreaterEqual(client.heartbeat.await_count, 2)
        self.assertFalse(lost.is_set())

    async def test_update_monitor_requests_graceful_shutdown(self):
        from mediaworker import updater

        shutdown = asyncio.Event()
        with patch.object(updater, "update_checkout", return_value="a" * 40):
            changed = await updater.watch_for_updates(self.cfg, shutdown)
        self.assertTrue(changed)
        self.assertTrue(shutdown.is_set())

    def test_updater_fast_forwards_without_removing_local_files(self):
        from mediaworker.updater import update_checkout

        root = self.cfg.work_dir / "update-fixture"
        remote = root / "remote.git"
        source = root / "source"
        checkout = root / "checkout"
        root.mkdir()

        def git(cwd: Path, *args: str) -> str:
            result = subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        remote.mkdir()
        git(remote, "init", "--bare")
        source.mkdir()
        git(source, "init", "-b", "main")
        (source / "worker.txt").write_text("v1", encoding="utf-8")
        git(source, "add", "worker.txt")
        git(source, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "v1")
        git(source, "remote", "add", "origin", str(remote))
        git(source, "push", "-u", "origin", "main")
        git(root, "clone", "-b", "main", str(remote), str(checkout))

        (source / "worker.txt").write_text("v2", encoding="utf-8")
        git(source, "add", "worker.txt")
        git(source, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "v2")
        git(source, "push", "origin", "main")
        (checkout / "local-note.txt").write_text("keep", encoding="utf-8")

        revision = update_checkout(checkout, "origin", "main")
        self.assertEqual(revision, git(source, "rev-parse", "HEAD"))
        self.assertEqual((checkout / "worker.txt").read_text(encoding="utf-8"), "v2")
        self.assertEqual((checkout / "local-note.txt").read_text(encoding="utf-8"), "keep")

    async def test_stale_heartbeat_marks_attempt_lost(self):
        client = SimpleNamespace(heartbeat=AsyncMock(side_effect=LeaseLost("stale")))
        stop, lost = asyncio.Event(), asyncio.Event()
        await lifecycle._heartbeat_loop(client, SimpleNamespace(id=JOB_ID, lease_token=LEASE, lease_seconds=0.03), stop, lost)
        self.assertTrue(lost.is_set())

    async def test_stale_attempt_never_uploads(self):
        class Engine(BaseEngine):
            def run(_, context):
                path = context.workdir / "result.jpg"
                path.write_bytes(b"result")
                return RunResult(path, 1, 1)
        client = SimpleNamespace(heartbeat=AsyncMock(side_effect=LeaseLost("stale")), complete=AsyncMock(), fail=AsyncMock())
        with patch.object(storage, "upload", new_callable=AsyncMock) as upload:
            await lifecycle._process_job(self.cfg, client, {"faceswap": Engine()}, job())
        upload.assert_not_awaited()
        client.fail.assert_not_awaited()
        self.assertFalse((self.cfg.work_dir / JOB_ID / LEASE).exists())

    async def test_three_jobs_enter_inference_concurrently(self):
        barrier = threading.Barrier(3)
        state_lock = threading.Lock()
        active = 0
        peak = 0

        class Engine(BaseEngine):
            def run(_, context):
                nonlocal active, peak
                with state_lock:
                    active += 1
                    peak = max(peak, active)
                barrier.wait(timeout=2)
                path = context.workdir / "result.jpg"
                path.write_bytes(b"result")
                with state_lock:
                    active -= 1
                return RunResult(path, 1, 1)

        client = SimpleNamespace(
            heartbeat=AsyncMock(), complete=AsyncMock(), fail=AsyncMock()
        )
        jobs = [
            job().model_copy(
                update={
                    "id": f"a86eeb49-a664-4249-bfb7-20c6001be74{index}",
                    "lease_token": f"d8c5b4a4-feb2-490a-a98d-f3965c91a47{index}",
                }
            )
            for index in range(3)
        ]
        engine = Engine()
        with patch.object(storage, "upload", new_callable=AsyncMock):
            await asyncio.wait_for(
                asyncio.gather(
                    *(lifecycle._process_job(self.cfg, client, {"faceswap": engine}, item) for item in jobs)
                ),
                timeout=3,
            )
        self.assertEqual(peak, 3)
        self.assertEqual(client.complete.await_count, 3)

    async def test_completion_failure_reports_fenced_failure_and_cleans(self):
        class Engine(BaseEngine):
            def run(_, context):
                path = context.workdir / "result.jpg"
                path.write_bytes(b"result")
                return RunResult(path, 1, 1)
        client = SimpleNamespace(heartbeat=AsyncMock(), complete=AsyncMock(side_effect=TimeoutError()), fail=AsyncMock())
        with patch.object(storage, "upload", new_callable=AsyncMock):
            await lifecycle._process_job(self.cfg, client, {"faceswap": Engine()}, job())
        self.assertEqual(client.fail.await_args.args[:2], (JOB_ID, LEASE))
        self.assertFalse((self.cfg.work_dir / JOB_ID / LEASE).exists())

    async def test_cancel_waits_for_thread_before_cleanup(self):
        started, wrote = threading.Event(), threading.Event()
        class Engine(BaseEngine):
            def run(_, context):
                started.set()
                time.sleep(0.08)
                path = context.workdir / "result.jpg"
                path.write_bytes(b"finished")
                wrote.set()
                return RunResult(path, 1, 1)
        client = SimpleNamespace(heartbeat=AsyncMock(), complete=AsyncMock(), fail=AsyncMock())
        task = asyncio.create_task(lifecycle._process_job(self.cfg, client, {"faceswap": Engine()}, job()))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self.assertTrue(wrote.is_set())
        self.assertFalse((self.cfg.work_dir / JOB_ID / LEASE).exists())
        client.complete.assert_not_awaited()

    async def test_client_sends_lease_and_rejects_stale(self):
        calls = []
        def handler(request):
            import json
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "stale": True})
        async with WorkerClient(self.cfg) as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url=self.cfg.api_base_url, transport=httpx.MockTransport(handler))
            with self.assertRaises(LeaseLost):
                await client.complete(JOB_ID, LEASE, "result", 1, 1)
            with self.assertRaises(LeaseLost):
                await client.heartbeat(JOB_ID, LEASE)
        self.assertTrue(all(call["lease_token"] == LEASE for call in calls))

    async def test_claim_response_loss_is_not_retried(self):
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(503, json={"error": "offline"})
        async with WorkerClient(self.cfg) as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url=self.cfg.api_base_url, transport=httpx.MockTransport(handler))
            with self.assertRaises(httpx.HTTPStatusError):
                await client.claim(["faceswap"], ["inswapper_128"])
        self.assertEqual(count, 1)

    async def test_expired_cognito_node_token_refreshes_once(self):
        self.cfg.cognito_username = "offline-user"
        self.cfg.cognito_password = "offline-password"
        calls = []
        def handler(request):
            calls.append(request.headers.get("Authorization"))
            return httpx.Response(401 if len(calls) == 1 else 200, json={"ok": True})
        async def refresh(cfg, force=False, previous_token=None):
            self.assertTrue(force)
            self.assertEqual(previous_token, "expired")
            cfg.node_token = "refreshed"
        async with WorkerClient(self.cfg) as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url=self.cfg.api_base_url,
                headers={"Authorization": "Bearer expired"}, transport=httpx.MockTransport(handler))
            with patch("mediaworker.client.bootstrap_node_token", side_effect=refresh) as bootstrap:
                await client.heartbeat(JOB_ID, LEASE)
            bootstrap.assert_awaited_once()
        self.assertEqual(calls, ["Bearer expired", "Bearer refreshed"])

    async def test_concurrent_unauthorized_requests_share_token_refresh(self):
        self.cfg.cognito_username = "offline-user"
        self.cfg.cognito_password = "offline-password"
        expired_calls = 0
        both_expired = asyncio.Event()

        async def handler(request):
            nonlocal expired_calls
            if request.headers.get("Authorization") == "Bearer expired":
                expired_calls += 1
                if expired_calls == 2:
                    both_expired.set()
                await both_expired.wait()
                return httpx.Response(401, json={})
            return httpx.Response(200, json={"ok": True})

        async def refresh(cfg, force=False, previous_token=None):
            cfg.node_token = "refreshed"

        async with WorkerClient(self.cfg) as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url=self.cfg.api_base_url,
                headers={"Authorization": "Bearer expired"}, transport=httpx.MockTransport(handler))
            with patch("mediaworker.client.bootstrap_node_token", side_effect=refresh) as bootstrap:
                await asyncio.wait_for(asyncio.gather(
                    client.heartbeat(JOB_ID, LEASE), client.heartbeat(JOB_ID, LEASE)), timeout=2)
            bootstrap.assert_awaited_once()
        self.assertEqual(expired_calls, 2)

    async def test_download_limits_stream_without_content_length(self):
        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"1234"
                yield b"5678"
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Chunks()))
        real_client = httpx.AsyncClient
        with patch.object(storage.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
            with self.assertRaisesRegex(ValueError, "byte limit"):
                await storage.download("https://offline.test/input", self.cfg.work_dir / "download", max_bytes=6)

    def test_image_pixel_limit_checked_before_decode(self):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        # Header-only test: the excessive dimensions must reject before raster IO.
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 5000, 4000, 8, 0, 0, 0, 0)) + chunk(b"IDAT", b"") + chunk(b"IEND", b"")
        path = self.cfg.work_dir / "large.png"
        path.write_bytes(png)
        with self.assertRaisesRegex(ValueError, "pixel limit"):
            validate_image(path, self.cfg)

    def test_valid_small_image_accepted_and_wrong_format_rejected(self):
        png = self.cfg.work_dir / "input.png"
        Image.new("RGB", (8, 6)).save(png)
        self.assertEqual(validate_image(png, self.cfg), (8, 6))
        gif = self.cfg.work_dir / "input.gif"
        Image.new("RGB", (8, 6)).save(gif)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            validate_image(gif, self.cfg)

    def test_missing_lease_contract_is_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            ClaimedJob(id=JOB_ID, task_type="faceswap", result={"put_url": "x", "key": "y"})

    def test_missing_swap_models_fail_before_identity_load(self):
        from mediaworker.engine.faceswap_engine import FaceSwapEngine
        self.cfg.faceswap_models_dir = self.cfg.work_dir / "missing-models"
        with patch.dict(os.environ, {"FACESWAP_SWAP_MODEL": ""}):
            engine = FaceSwapEngine(self.cfg)
        with self.assertRaisesRegex(FileNotFoundError, "no swap models installed"):
            engine.load()
        engine._models = {"inswapper_128": object()}
        self.assertEqual(engine.available_models, ["inswapper_128"])

    def test_model_registry_reuses_verified_files_without_network(self):
        payload = b"verified-model"
        asset = model_registry.ModelAsset(
            "test.onnx", len(payload), hashlib.sha256(payload).hexdigest()
        )
        self.cfg.faceswap_models_dir = self.cfg.work_dir / "models"
        self.cfg.faceswap_models_dir.mkdir()
        (self.cfg.faceswap_models_dir / asset.name).write_bytes(payload)
        self.cfg.faceswap_model_release = "test-release"
        with patch.dict(model_registry.MODEL_MANIFESTS, {"test-release": (asset,)}):
            model_registry.sync_faceswap_models(
                self.cfg,
                transport=httpx.MockTransport(
                    lambda request: self.fail(f"unexpected request: {request.url}")
                ),
            )

    def test_model_registry_downloads_and_atomically_replaces_invalid_file(self):
        payload = b"fresh-model"
        asset = model_registry.ModelAsset(
            "test.onnx", len(payload), hashlib.sha256(payload).hexdigest()
        )
        self.cfg.faceswap_models_dir = self.cfg.work_dir / "models"
        self.cfg.faceswap_models_dir.mkdir()
        destination = self.cfg.faceswap_models_dir / asset.name
        destination.write_bytes(b"corrupt")
        self.cfg.faceswap_model_release = "test-release"

        def handler(request):
            if request.url.path.endswith("/releases/tags/test-release"):
                return httpx.Response(
                    200,
                    json={"assets": [{"name": asset.name, "url": "https://api.github.test/assets/1"}]},
                )
            if request.url.path == "/assets/1":
                return httpx.Response(200, content=payload)
            return httpx.Response(404)

        with patch.dict(model_registry.MODEL_MANIFESTS, {"test-release": (asset,)}):
            model_registry.sync_faceswap_models(
                self.cfg, transport=httpx.MockTransport(handler)
            )
        self.assertEqual(destination.read_bytes(), payload)
        self.assertFalse(destination.with_name(destination.name + ".part").exists())

    def test_cutout_model_download_is_verified_and_reused(self):
        payload = b"verified-cutout-model"
        asset = model_registry.PublicModelAsset(
            "test-cutout.onnx",
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            "https://models.test/test-cutout.onnx",
        )
        self.cfg.capabilities = ["cutout"]
        self.cfg.cutout_model = "test-cutout"
        self.cfg.cutout_models_dir = self.cfg.work_dir / "cutout-models"
        requests = 0

        def handler(request):
            nonlocal requests
            requests += 1
            return httpx.Response(200, content=payload)

        with patch.dict(model_registry.CUTOUT_MODELS, {"test-cutout": asset}):
            model_registry.sync_cutout_model(
                self.cfg, transport=httpx.MockTransport(handler)
            )
            model_registry.sync_cutout_model(
                self.cfg,
                transport=httpx.MockTransport(
                    lambda request: self.fail(f"unexpected request: {request.url}")
                ),
            )
        self.assertEqual(
            (self.cfg.cutout_models_dir / asset.name).read_bytes(), payload
        )
        self.assertEqual(requests, 1)

    async def test_mock_worker_cannot_join_remote_queue(self):
        with patch.dict(os.environ, {"MEDIAWORKER_MOCK": "1"}):
            with self.assertRaisesRegex(SystemExit, "local API"):
                await runner.run_forever(self.cfg)

    def test_aligned_faces_do_not_depend_on_a_second_detector(self):
        from mediaworker.engine.faceswap_engine import FaceSwapEngine

        for model, size, swap_method in [
            ("inswapper_128", 128, "_swap_inswapper"),
            ("hyperswap_256_a", 256, "_swap_hyperswap"),
        ]:
            with self.subTest(model=model):
                crop = self.cfg.work_dir / f"aligned-{size}.png"
                Image.new("RGB", (size, size), (100, 120, 140)).save(crop)
                engine = FaceSwapEngine(self.cfg)
                recognition = Mock()
                engine._recognition = recognition
                context = JobContext("aligned", "faceswap", {"mode": "face", "model": model},
                                     {"target": crop, "source": crop}, self.cfg.work_dir)
                with patch.object(engine, swap_method, return_value=np.full((size, size, 4), 255, np.uint8)):
                    result = engine.run(context)
                self.assertEqual((result.width, result.height, result.content_type), (size, size, "image/png"))
                with Image.open(result.output_path) as output:
                    self.assertEqual(output.mode, "RGBA")
                recognition.get.assert_called_once()

    def test_full_photo_mode_is_rejected(self):
        from mediaworker.engine.faceswap_engine import FaceSwapEngine

        crop = self.cfg.work_dir / "full-mode.png"
        Image.new("RGB", (128, 128)).save(crop)
        engine = FaceSwapEngine(self.cfg)
        context = JobContext("aligned", "faceswap", {"mode": "full"},
                             {"target": crop, "source": crop}, self.cfg.work_dir)
        with self.assertRaisesRegex(ValueError, "browser-aligned"):
            engine.run(context)

    def test_aligned_mode_rejects_wrong_crop_size(self):
        from mediaworker.engine.faceswap_engine import FaceSwapEngine

        crop = self.cfg.work_dir / "wrong-size.png"
        Image.new("RGB", (256, 256)).save(crop)
        engine = FaceSwapEngine(self.cfg)
        context = JobContext("aligned", "faceswap", {"mode": "face", "model": "inswapper_128"},
                             {"target": crop, "source": crop}, self.cfg.work_dir)
        with self.assertRaisesRegex(ValueError, "128x128"):
            engine.run(context)

    def test_baseline_browser_crop_keeps_pixels_outside_former_ellipse(self):
        from mediaworker.engine.faceswap_engine import FaceSwapEngine

        engine = FaceSwapEngine(self.cfg)
        fake = np.full((128, 128, 3), (31, 127, 219), dtype=np.uint8)
        model = Mock()
        model.get.return_value = (fake, np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32))
        with patch.object(engine, "_get_model", return_value=model):
            for blur, area in [(0, 50), (1, 10)]:
                output = engine._swap_inswapper(fake, Mock(), Mock(), blur, area, False)
                np.testing.assert_array_equal(output[:, :, :3], fake)
                np.testing.assert_array_equal(output[:, :, 3], np.full((128, 128), 255))

    def test_full_baseline_box_mask_has_soft_rectangular_edges(self):
        from mediaworker.engine.faceswap_engine import _box_mask

        mask = _box_mask(128, 0, 50)
        self.assertEqual(mask[5, 5], 1)
        self.assertEqual(mask[0, 64], 0)
        soft = _box_mask(128, 0.4, 50)
        self.assertGreater(soft[12, 64], 0)
        self.assertLess(soft[12, 64], 1)
        np.testing.assert_allclose(soft, soft.T, atol=1e-6)

    def test_directml_sessions_use_required_execution_options(self):
        from mediaworker.engine.base import session_options

        options = session_options(["DmlExecutionProvider", "CPUExecutionProvider"])
        self.assertFalse(options.enable_mem_pattern)
        self.assertEqual(options.execution_mode.name, "ORT_SEQUENTIAL")

    def test_directml_can_disable_graph_fusion_for_fp16_hyperswap(self):
        from mediaworker.engine.base import session_options

        options = session_options(
            ["DmlExecutionProvider", "CPUExecutionProvider"],
            disable_graph_optimizations=True,
        )
        self.assertEqual(
            options.graph_optimization_level.name,
            "ORT_DISABLE_ALL",
        )

    def test_hyperswap_disables_graph_fusion_when_using_directml(self):
        import onnxruntime as ort
        from mediaworker.engine import faceswap_engine

        self.cfg.faceswap_models_dir = self.cfg.work_dir / "models"
        self.cfg.faceswap_models_dir.mkdir()
        (self.cfg.faceswap_models_dir / "hyperswap_1a_256.onnx").write_bytes(b"model")
        engine = faceswap_engine.FaceSwapEngine(self.cfg)
        session = SimpleNamespace(
            get_providers=lambda: ["DmlExecutionProvider", "CPUExecutionProvider"]
        )
        with (
            patch.object(
                faceswap_engine,
                "resolve_providers",
                return_value=["DmlExecutionProvider", "CPUExecutionProvider"],
            ),
            patch.object(ort, "InferenceSession", return_value=session) as create_session,
        ):
            engine._get_model("hyperswap_256_a")

        options = create_session.call_args.kwargs["sess_options"]
        self.assertEqual(options.graph_optimization_level.name, "ORT_DISABLE_ALL")

    def test_windows_cuda_runtime_is_preloaded_when_selected(self):
        import onnxruntime as ort
        from mediaworker.engine import base

        with (
            patch.object(base.sys, "platform", "win32"),
            patch.object(ort, "get_available_providers", return_value=[
                "CUDAExecutionProvider", "CPUExecutionProvider"
            ]),
            patch.object(ort, "preload_dlls") as preload,
        ):
            providers = base.resolve_providers([
                "CUDAExecutionProvider", "CPUExecutionProvider"
            ])
        self.assertEqual(providers, ["CUDAExecutionProvider", "CPUExecutionProvider"])
        preload.assert_called_once_with(directory="")

    def test_pythonw_writes_a_real_rotating_log(self):
        from mediaworker.__main__ import _configure_logging
        root_logger = logging.getLogger()
        previous = list(root_logger.handlers)
        logfile = self.cfg.work_dir / "logs" / "mediaworker.log"
        try:
            with patch.dict(os.environ, {"LOG_FILE": str(logfile)}), patch("sys.stderr", None):
                _configure_logging()
                logging.info("offline-log-test")
                for handler in root_logger.handlers:
                    handler.flush()
            self.assertIn("offline-log-test", logfile.read_text(encoding="utf-8"))
        finally:
            for handler in root_logger.handlers:
                handler.close()
            root_logger.handlers = previous


if __name__ == "__main__":
    unittest.main()
