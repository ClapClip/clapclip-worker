import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch

import numpy as np
from PIL import Image

from mediaworker.config import Config
from mediaworker.engine.base import RunResult, JobContext
from mediaworker.local_web import Workbench, LocalServer
from mediaworker.local_faceswap import similarity
from mediaworker.single_instance import FileLock


class FakeEngine:
    available_models = ["test"]
    def run(self, ctx):
        output = ctx.workdir / "result.png"
        output.write_bytes(ctx.inputs["image"].read_bytes())
        return RunResult(output, 16, 16, "image/png")


class LocalWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = Config.from_env(load_env=False)
        self.cfg.runtime_dir = self.root / "runtime"
        self.app = Workbench(self.cfg, self.root / "jobs", {"cutout": FakeEngine()})
        self.server = LocalServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(buffer, "PNG")
        self.image = base64.b64encode(buffer.getvalue()).decode()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.close()
        self.temp.cleanup()

    def request(self, path, data=None, token=True, origin=None):
        headers = {}
        if data is not None:
            headers = {"Content-Type": "application/json", "Origin": origin or self.server.origin}
            if token:
                headers["X-Local-Token"] = self.app.token
        return urlopen(Request(self.server.origin + path, data=json.dumps(data).encode() if data is not None else None, headers=headers), timeout=3)

    def submit(self):
        with self.request("/api/jobs", {"tool": "cutout", "params": {}, "inputs": {"image": self.image}}) as response:
            return json.load(response)["id"]

    def wait(self, identifier):
        for _ in range(100):
            with self.request("/api/jobs/" + identifier) as response:
                job = json.load(response)
            if job["status"] in {"ready", "cancelled", "failed"}:
                return job
            time.sleep(.02)
        self.fail("job did not finish")

    def test_real_http_image_and_cleanup(self):
        identifier = self.submit()
        job = self.wait(identifier)
        self.assertEqual(job["status"], "ready")
        with self.request(job["result"]) as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.read(), base64.b64decode(self.image))
        with self.request("/api/jobs/" + identifier + "/delete", {}) as response:
            self.assertEqual(response.status, 200)
        self.assertFalse((self.app.root / identifier).exists())

    def test_csrf_and_origin_rejected(self):
        for padding in ("", "x" * 8192):
            for kwargs in ({"token": False}, {"origin": "https://evil.example"}):
                with self.assertRaises(HTTPError) as error:
                    self.request("/api/jobs", {"tool": "cutout", "padding": padding}, **kwargs)
                self.assertEqual(error.exception.code, 403)
        self.assertFalse(self.app.jobs)

    def test_dns_rebinding_host_and_traversal(self):
        request = Request(self.server.origin + "/api/state", headers={"Host": "evil.example"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)
        for path in ("/../config.py", "/api/jobs/../../.env/result"):
            with self.assertRaises(HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, 404)

    def test_invalid_image_cleans_attempt(self):
        with self.assertRaises(HTTPError):
            self.request("/api/jobs", {"tool": "cutout", "inputs": {"image": "aGVsbG8="}})
        self.assertFalse(list(self.app.root.iterdir()))

    def test_shared_slots_and_queued_cancellation(self):
        locks = [FileLock(self.cfg.runtime_dir / "slots" / f"slot-{i}.lock").acquire() for i in range(3)]
        try:
            identifier = self.submit()
            time.sleep(.1)
            self.assertEqual(self.app.jobs[identifier].status, "queued")
            with self.request("/api/jobs/" + identifier + "/cancel", {}):
                pass
            self.assertEqual(self.wait(identifier)["status"], "cancelled")
        finally:
            for lock in locks:
                lock.release()

    def test_download_only_accepts_server_owned_format(self):
        self.app.engines["download"] = FakeEngine()
        self.app.states["download"] = "ready"
        with self.assertRaises(ValueError):
            self.app.submit({"tool": "download", "params": {"operation": "download", "infoId": "not-owned", "option": {"path": "C:/secret"}}})

    def test_download_info_owns_options_and_no_image_required(self):
        class Download:
            available_models = []
            def run(inner, ctx):
                from mediaworker.downloader.policy import DOWNLOAD_LIMITS
                self.assertEqual(ctx.params["limits"], DOWNLOAD_LIMITS)
                path = ctx.workdir / "result.json"
                if ctx.params["operation"] == "info":
                    path.write_text(json.dumps({"title": "Generated fixture", "duration": 10, "formats": [{"id": "mp4", "label": "MP4"}], "options": {"mp4": {"private": "server-owned"}}}))
                    return RunResult(path, 1, 1, "application/json")
                self.assertEqual(ctx.params["option"], {"private": "server-owned"})
                path = ctx.workdir / "result.mp4"
                path.write_bytes(b"fixture")
                return RunResult(path, 16, 16, "video/mp4")
        self.app.engines["download"], self.app.states["download"] = Download(), "ready"
        data = self.app.submit({"tool": "download", "params": {"operation": "info", "url": "https://example.com/video"}})
        metadata = self.wait(data["id"])
        self.assertNotIn("options", metadata["media"])
        result = self.app.submit({"tool": "download", "params": {"operation": "download", "infoId": data["id"], "formatId": "mp4", "option": {"private": "attacker"}}})
        self.assertEqual(self.wait(result["id"])["status"], "ready")

    def test_api_never_returns_secrets_or_paths(self):
        with self.request("/api/state") as response:
            data = response.read().decode()
        self.assertNotIn(str(self.root), data)
        self.assertNotIn("cognito", data)
        self.assertNotIn("node_token", data)

    def test_portrait_requires_ready_detector_and_face_model(self):
        engine = FakeEngine()
        engine.available_models = ["portrait"]
        engine._faces = {"gfpgan": object()}
        self.app.engines["enhance"], self.app.states["enhance"] = engine, "ready"
        self.assertEqual(self.app.state()["tools"]["enhance"]["state"], "unavailable")
        engine._detector = object()
        state = self.app.state()
        self.assertEqual(state["faceModels"], ["gfpgan"])
        self.assertEqual(state["tools"]["enhance"]["models"], ["portrait"])


class AlignmentTests(unittest.TestCase):
    def test_similarity_preserves_geometry(self):
        points = np.array([[20, 30], [60, 30], [40, 50], [25, 70], [55, 70]], dtype=float)
        target = points * 2 + [5, 9]
        matrix = similarity(points, target)
        np.testing.assert_allclose(matrix, [[2, 0, 5], [0, 2, 9]], atol=1e-5)

    def test_degenerate_alignment_rejected(self):
        with self.assertRaises(ValueError):
            similarity(np.zeros((5, 2)), np.ones((5, 2)))

    def test_full_photo_composite_retains_outside_pixels(self):
        import cv2
        from mediaworker.local_faceswap import LocalFaceSwap
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cfg = Config.from_env(load_env=False)
            adapter = LocalFaceSwap(cfg)
            class CropEngine:
                available_models = ["inswapper_128"]
                def run(self, ctx):
                    for path in ctx.inputs.values():
                        self_image = cv2.imread(str(path))
                        if self_image.shape[:2] != (128, 128):
                            raise AssertionError("wrong native size")
                    output = ctx.workdir / "result.png"
                    rgba = np.full((128, 128, 4), 255, dtype=np.uint8)
                    rgba[..., :3] = [0, 0, 255]
                    cv2.imwrite(str(output), rgba)
                    return RunResult(output, 128, 128, "image/png")
            adapter.engine = CropEngine()
            matrix = np.array([[1, 0, -64], [0, 1, -64]], dtype=np.float32)
            adapter.align = lambda image, size: (image[64:192, 64:192], matrix)
            inputs = {}
            for name in ("target", "source"):
                inputs[name] = root / (name + ".png")
                Image.new("RGB", (256, 256), "blue").save(inputs[name])
            result = adapter.run(JobContext("fixture", "faceswap", {"model": "inswapper_128", "edgeBlur": 0, "fusionArea": 50}, inputs, root))
            image = cv2.imread(str(result.output_path))
            self.assertEqual(image.shape[:2], (256, 256))
            np.testing.assert_array_equal(image[0, 0], [255, 0, 0])
            np.testing.assert_array_equal(image[128, 128], [0, 0, 255])
