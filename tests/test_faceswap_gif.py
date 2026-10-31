"""Generated animation fixtures; no network, private configuration or queue."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import httpx
from PIL import Image, ImageDraw

from mediaworker.config import Config
from mediaworker.engine.base import JobContext
from mediaworker.engine.faceswap_engine import FaceSwapEngine
from mediaworker.engine.faceswap_gif import GifFaceSwap
from mediaworker.gif_media import GifDeadline, GifInfo, GifWriter, inspect_gif
from mediaworker import scheduling
from mediaworker.client import WorkerClient


class GifTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config.from_env(load_env=False)
        self.cancel = threading.Event()

    def fixture(self, *, loop=None, disposal=2, durations=(40, 90, 150)):
        frames = []
        for x in (1, 7, 13):
            frame = Image.new("RGBA", (24, 20))
            ImageDraw.Draw(frame).rectangle((x, 4, x + 5, 12), fill=(200, 60, 20, 255))
            frames.append(frame)
        path = self.root / "target.gif"
        frames[0].save(path, save_all=True, append_images=frames[1:],
                       duration=list(durations), disposal=disposal,
                       **({"loop": loop} if loop is not None else {}))
        return path

    def test_timing_loop_transparency_and_disposal_roundtrip(self):
        for loop in (None, 0, 3):
            for disposal in (1, 2, 3):
                with self.subTest(loop=loop, disposal=disposal):
                    path = self.fixture(loop=loop, disposal=disposal)
                    info = inspect_gif(path, self.cfg, self.cancel)
                    output = self.root / "output.gif"
                    writer = GifWriter(output, info, self.cfg.max_result_bytes, self.cancel)
                    original = []
                    with Image.open(path) as image:
                        for i, duration in enumerate(info.durations):
                            image.seek(i)
                            frame = image.convert("RGBA")
                            original.append(np.asarray(frame))
                            writer.append(frame, duration)
                    writer.close(True)
                    self.assertEqual(inspect_gif(output, self.cfg, self.cancel), info)
                    with Image.open(output) as image:
                        self.assertEqual(image.info.get("loop"), loop)
                        self.assertEqual(image.n_frames, 3)
                        for i, expected in enumerate(original):
                            image.seek(i)
                            actual = np.asarray(image.convert("RGBA"))
                            np.testing.assert_array_equal(actual[..., 3], expected[..., 3])
                            opaque = expected[..., 3] > 0
                            np.testing.assert_array_equal(actual[opaque], expected[opaque])

    def test_identical_frames_are_not_collapsed(self):
        output = self.root / "identical.gif"
        info = GifInfo(8, 8, (40, 90), None)
        writer = GifWriter(output, info, 10000, self.cancel)
        for duration in info.durations:
            writer.append(Image.new("RGBA", (8, 8), "red"), duration)
        writer.close(True)
        self.assertEqual(inspect_gif(output, self.cfg, self.cancel), info)

    def test_budgets_rejected_before_raster_decode(self):
        from mediaworker import gif_media
        path = self.fixture()
        for limit, value in (("maxFrames", 2), ("maxTotalPixels", 1000),
                             ("maxDurationMs", 200), ("maxDimension", 16), ("maxBytes", 10)):
            with self.subTest(limit=limit), patch.dict(gif_media.GIF_LIMITS, {limit: value}), \
                    patch.object(Image, "open", side_effect=AssertionError("raster allocated")):
                with self.assertRaises(ValueError):
                    inspect_gif(path, self.cfg, self.cancel)

    def test_malformed_and_truncated_gifs(self):
        data = self.fixture().read_bytes()
        cases = [b"notgif", data[:20], data[:-2], data + b"secret", data.replace(b"GIF89a", b"GIFxxxxx", 1)]
        for i, raw in enumerate(cases):
            path = self.root / f"bad-{i}"
            path.write_bytes(raw)
            with self.subTest(i=i), self.assertRaises((ValueError, OSError, EOFError)):
                inspect_gif(path, self.cfg, self.cancel)

    def test_browser_short_delay_normalization_and_excessive_delay(self):
        for durations in ((0, 90, 150), (10, 90, 150)):
            with self.subTest(durations=durations):
                self.assertEqual(inspect_gif(self.fixture(durations=durations), self.cfg, self.cancel).durations, (100, 90, 150))
        with self.assertRaisesRegex(ValueError, "duration"):
            inspect_gif(self.fixture(durations=(10010, 90, 150)), self.cfg, self.cancel)

    def test_trailer_failure_also_removes_partial_output(self):
        path = self.root / "trailer.gif"
        for cancelled in (False, True):
            event = threading.Event()
            writer = GifWriter(path, GifInfo(8, 8, (40, 90), None), 10000, event)
            writer.append(Image.new("RGBA", (8, 8), "red"), 40)
            if cancelled:
                event.set()
            else:
                writer.limit = writer.file.tell()
            with self.assertRaises((ValueError, RuntimeError)):
                writer.close(True)
            self.assertFalse(path.exists())

    def test_processing_deadline_covers_encoding_and_cleanup(self):
        path = self.root / "deadline.gif"
        with patch("mediaworker.gif_media.time.monotonic", return_value=0):
            deadline = GifDeadline(self.cancel)
        writer = GifWriter(path, GifInfo(8, 8, (40, 90), None), 10000, deadline)
        with patch("mediaworker.gif_media.time.monotonic", return_value=121):
            with self.assertRaisesRegex(RuntimeError, "time budget"):
                writer.close(True)
        self.assertFalse(path.exists())

    def test_encoder_cancel_and_byte_budget_remove_partial_output(self):
        for cancelled in (True, False):
            path = self.root / "cancel.gif"
            event = threading.Event()
            writer = GifWriter(path, GifInfo(8, 8, (40, 90), None), 10, event)
            if cancelled:
                event.set()
            try:
                with self.assertRaises((ValueError, RuntimeError)):
                    writer.append(Image.new("RGBA", (8, 8), "red"), 40)
            finally:
                writer.close()
            self.assertFalse(path.exists())

    def context(self):
        path = self.fixture(loop=2)
        source = self.root / "source.png"
        Image.new("RGB", (128, 128), "blue").save(source)
        return JobContext("offline-gif", "faceswap", {"mode": "gif", "targetMedia": "gif"},
                          {"target": path, "source": source}, self.root, self.cancel)

    def engine(self):
        engine = FaceSwapEngine(self.cfg)
        engine._models = {"inswapper_128": Mock()}
        engine._recognition = Mock()
        engine._gif = GifFaceSwap(self.cfg, engine)
        return engine

    def test_gif_execution_keeps_animation_and_reports_progress(self):
        engine, ctx = self.engine(), self.context()
        ctx.progress = Mock()
        with patch.object(engine._gif, "_select", return_value=(Mock(), np.ones(512))), \
                patch.object(engine, "_swap_inswapper", side_effect=lambda image, *_: 255 - image):
            result = engine.run(ctx)
        self.assertEqual((result.content_type, result.output_path.suffix), ("image/gif", ".gif"))
        self.assertEqual(inspect_gif(result.output_path, self.cfg, self.cancel).durations, (40, 90, 150))
        self.assertEqual(ctx.progress.call_count, 3)
        self.assertEqual(ctx.progress.call_args.args, ("faceswap", 95))

    def test_no_faces_is_failure_with_cleanup_and_static_gif_is_rejected(self):
        engine, ctx = self.engine(), self.context()
        with patch.object(engine._gif, "_select", return_value=(None, None)), \
                self.assertRaisesRegex(ValueError, "no usable"):
            engine.run(ctx)
        self.assertFalse((self.root / "result.gif").exists())
        with self.assertRaisesRegex(ValueError, "unsupported image"):
            engine.run(replace(ctx, params={"mode": "face"}))

    def test_cancel_between_frames_stops_inference_and_removes_output(self):
        engine, ctx = self.engine(), self.context()
        ctx.progress = lambda *_: ctx.cancel.set()
        with patch.object(engine._gif, "_select", return_value=(Mock(), np.ones(512))), \
                patch.object(engine, "_swap_inswapper", side_effect=lambda image, *_: image) as swap, \
                self.assertRaisesRegex(RuntimeError, "cancelled"):
            engine.run(ctx)
        self.assertEqual(swap.call_count, 1)
        self.assertFalse((self.root / "result.gif").exists())

    def test_primary_identity_does_not_follow_a_different_larger_face(self):
        engine = self.engine()
        identity = np.array([1., 0.])
        first = SimpleNamespace(normed_embedding=identity)
        other = SimpleNamespace(normed_embedding=np.array([0., 1.]))
        image = np.zeros((128, 128, 3), np.uint8)
        with patch.object(engine._gif, "_faces", return_value=[other, first]):
            face, _ = engine._gif._select(image, identity, self.cancel)
            self.assertIs(face, first)
        with patch.object(engine._gif, "_faces", return_value=[other]):
            self.assertIsNone(engine._gif._select(image, identity, self.cancel)[0])
        with patch.object(engine._gif, "_faces", return_value=[first, first]):
            self.assertIsNone(engine._gif._select(image, identity, self.cancel)[0])

    def test_media_advertisement_requires_loaded_models_and_detector(self):
        engine = FaceSwapEngine(self.cfg)
        self.assertEqual(engine.available_media, [])
        engine._models = {"inswapper_128": object()}
        engine._recognition = object()
        self.assertEqual(engine.available_media, ["image"])
        engine._gif = object()
        self.assertEqual(engine.available_media, ["image", "gif"])


class GifClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_wire_media_gate_is_top_level_and_not_sent_to_download(self):
        cfg = Config.from_env(load_env=False)
        cfg.api_base_url, cfg.node_token = "https://offline.test", "offline"
        for caps in (["faceswap"], ["download"]):
            bodies = []
            def respond(request):
                bodies.append(json.loads(request.content))
                return httpx.Response(200, json={"job": None})
            async with WorkerClient(cfg) as client:
                await client._http.aclose()
                client._http = httpx.AsyncClient(base_url=cfg.api_base_url, transport=httpx.MockTransport(respond))
                await client.claim(caps, [], faceswap_media=["image", "gif", "gif"])
            if "faceswap" in caps:
                self.assertEqual(bodies[0]["faceswapMedia"], ["gif", "image"])
            else:
                self.assertNotIn("faceswapMedia", bodies[0])

    async def test_only_faceswap_poller_declares_loaded_gif_support(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config.from_env(load_env=False)
            cfg.runtime_dir = Path(directory)
            for group, expected in ((["faceswap"], {"faceswap_media": ["image", "gif"]}), (["download"], {})):
                stop, seen = asyncio.Event(), []
                async def claim(_capabilities, _models, **options):
                    seen.append(options)
                    stop.set()
                registry = {"faceswap": SimpleNamespace(available_models=[], available_media=["image", "gif"]),
                            "download": SimpleNamespace(available_models=[])}
                await scheduling._worker_loop(0, cfg, SimpleNamespace(claim=claim), registry, stop, group)
                self.assertEqual(seen, [expected])


if __name__ == "__main__":
    unittest.main()
