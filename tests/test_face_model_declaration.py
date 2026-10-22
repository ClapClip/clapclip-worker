from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from mediaworker.client import WorkerClient
from mediaworker.config import Config
from mediaworker.engine.enhance_engine import EnhanceEngine
from mediaworker import scheduling


class FaceModelDeclarationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.dict(os.environ, {}, clear=True), \
                patch("mediaworker.config.Path.home", return_value=Path(temporary.name)):
            self.cfg = Config.from_env(load_env=False)
        self.cfg.runtime_dir = Path(temporary.name)
        self.cfg.api_base_url = "https://offline.test"
        self.cfg.node_token = "offline-token"

    def test_configuration_does_not_advertise_unloaded_face_models(self):
        self.cfg.enhance_face_models = ["gfpgan", "gpen"]
        engine = EnhanceEngine(self.cfg)
        self.assertEqual(engine.available_face_models, [])
        engine._faces = {"gpen": object(), "gfpgan": object()}
        self.assertEqual(engine.available_face_models, [])
        engine._detector = object()
        self.assertEqual(engine.available_face_models, ["gfpgan", "gpen"])
        engine._faces.pop("gfpgan")
        self.assertEqual(engine.available_face_models, ["gpen"])

    def test_partial_load_declares_only_successful_restorer(self):
        self.cfg.enhance_models = []
        self.cfg.enhance_face_models = ["gfpgan", "gpen"]

        def load(path, _providers):
            if path.name == "gfpgan_1.4.onnx":
                raise RuntimeError("unavailable fixture")
            size = 640 if path.name == "yoloface_8n.onnx" else 256
            return SimpleNamespace(shape=(1, 3, size, size))

        engine = EnhanceEngine(self.cfg)
        with patch("mediaworker.engine.enhance_engine.OnnxModel", side_effect=load), \
                self.assertLogs("mediaworker.engine.enhance_engine", level="ERROR"):
            engine.load()
        self.assertEqual(engine.available_models, ["portrait"])
        self.assertEqual(engine.available_face_models, ["gpen"])

    def test_detector_failure_cannot_declare_face_models(self):
        self.cfg.enhance_models = []
        self.cfg.enhance_face_models = ["gfpgan", "gpen"]
        engine = EnhanceEngine(self.cfg)
        with patch("mediaworker.engine.enhance_engine.OnnxModel", side_effect=RuntimeError("unavailable fixture")), \
                self.assertLogs("mediaworker.engine.enhance_engine", level="ERROR"), \
                self.assertRaisesRegex(RuntimeError, "no enhance GPU models"):
            engine.load()
        self.assertEqual(engine.available_face_models, [])

    async def capture_claim(self, capabilities, models, **options):
        requests = []

        def handler(request):
            requests.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"job": None})

        async with WorkerClient(self.cfg) as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url=self.cfg.api_base_url,
                                            transport=httpx.MockTransport(handler))
            self.assertIsNone(await client.claim(capabilities, models, **options))
        self.assertEqual(len(requests), 1)
        return requests[0]

    async def test_enhance_claim_adds_face_models_without_changing_old_fields(self):
        path, body = await self.capture_claim(
            ["faceswap", "cutout", "enhance"], ["standard", "portrait"],
            face_models=["gpen", "gfpgan", "gpen"])
        self.assertEqual(path, "/api/worker/claim")
        self.assertEqual(body, {
            "node_id": self.cfg.node_id,
            "capabilities": ["faceswap", "cutout", "enhance"],
            "trust_tier": self.cfg.trust_tier,
            "models": ["standard", "portrait"],
            "faceModels": ["gfpgan", "gpen"],
        })

    async def test_enhance_without_loaded_faces_explicitly_declares_empty_list(self):
        for options in ({}, {"face_models": []}):
            with self.subTest(options=options):
                _, body = await self.capture_claim(["enhance"], ["standard"], **options)
                self.assertEqual(body["faceModels"], [])

    async def test_other_claims_cannot_leak_enhance_face_declaration(self):
        for capabilities, expected in ((["download"], "/api/worker/download/claim"),
                                       (["faceswap", "cutout"], "/api/worker/claim")):
            with self.subTest(capabilities=capabilities):
                path, body = await self.capture_claim(capabilities, [], face_models=["gfpgan"])
                self.assertEqual(path, expected)
                self.assertNotIn("faceModels", body)
                if capabilities == ["download"]:
                    self.assertIn("downloadPolicyVersions", body)

    async def test_poller_forwards_loaded_faces_only_to_enhance_group(self):
        registry = {
            "enhance": SimpleNamespace(available_models=["standard", "portrait"],
                                       available_face_models=["gpen"]),
            "download": SimpleNamespace(available_models=[]),
        }
        for group, expected_models, expected_options in (
            (["enhance"], ["portrait", "standard"], {"face_models": ["gpen"]}),
            (["download"], [], {}),
        ):
            with self.subTest(group=group):
                stop = asyncio.Event()
                seen = []

                async def claim(capabilities, models, **options):
                    seen.append((capabilities, models, options))
                    stop.set()
                    return None

                await scheduling._worker_loop(0, self.cfg, SimpleNamespace(claim=claim),
                                              registry, stop, group)
                self.assertEqual(seen, [(group, expected_models, expected_options)])

    async def test_legacy_or_mock_enhance_engine_does_not_claim_face_support(self):
        stop = asyncio.Event()
        seen = []

        async def claim(capabilities, models, **options):
            seen.append(options)
            stop.set()
            return None

        registry = {"enhance": SimpleNamespace(available_models=["standard", "portrait"])}
        await scheduling._worker_loop(0, self.cfg, SimpleNamespace(claim=claim),
                                      registry, stop, ["enhance"])
        self.assertEqual(seen, [{"face_models": []}])


if __name__ == "__main__":
    unittest.main()
