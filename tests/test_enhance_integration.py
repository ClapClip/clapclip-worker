from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from mediaworker import model_registry
from mediaworker.config import Config
from mediaworker.engine.registry import build_registry
from mediaworker.engine.base import JobContext


class EnhanceIntegrationTests(unittest.TestCase):
    def setUp(self):
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("ENHANCE_") and key not in {"CAPABILITIES", "MEDIAWORKER_MOCK"}
        }
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.cfg = Config.from_env(load_env=False)

    def test_enhance_defaults_enable_the_capability(self):
        self.assertEqual(self.cfg.capabilities, ["faceswap", "cutout", "enhance", "download"])
        self.assertEqual(
            self.cfg.enhance_models_dir, Path.home() / ".mediaworker/models/enhance"
        )
        self.assertEqual(
            self.cfg.enhance_models, ["standard", "fast", "deblur", "animation"]
        )
        self.assertEqual(self.cfg.enhance_face_models, ["gfpgan", "gpen"])

    def test_enhance_configuration_and_explicit_empty_lists(self):
        with patch.dict(
            os.environ,
            {
                "CAPABILITIES": "enhance",
                "ENHANCE_MODELS_DIR": "~/custom-enhance",
                "ENHANCE_MODELS": "fast, animation",
                "ENHANCE_FACE_MODELS": "",
            },
        ):
            cfg = Config.from_env(load_env=False)
            self.assertEqual(cfg.capabilities, ["enhance"])
            self.assertEqual(
                cfg.enhance_models_dir, (Path.home() / "custom-enhance").resolve()
            )
            self.assertEqual(cfg.enhance_models, ["fast", "animation"])
            self.assertEqual(cfg.enhance_face_models, [])
            with patch.dict(os.environ, {"ENHANCE_MODELS": "", "ENHANCE_FACE_MODELS": "gpen"}):
                portrait_cfg = Config.from_env(load_env=False)
            self.assertEqual(portrait_cfg.enhance_models, [])
            self.assertEqual(portrait_cfg.enhance_face_models, ["gpen"])

    def test_model_sync_keeps_enhance_for_local_engine_validation_without_network(self):
        self.cfg.capabilities = ["enhance", "enhance"]
        with patch("mediaworker.model_registry.httpx.Client") as network:
            self.assertEqual(model_registry.sync_models(self.cfg), ["enhance"])
        network.assert_not_called()

    def test_registry_loads_enhance_and_filters_an_unavailable_engine(self):
        engine = Mock(task_type="enhance")
        factory = Mock(return_value=engine)
        with patch.dict(
            "sys.modules",
            {"mediaworker.engine.enhance_engine": SimpleNamespace(EnhanceEngine=factory)},
        ):
            self.assertEqual(build_registry(self.cfg, ["enhance"]), {"enhance": engine})
            factory.assert_called_once_with(self.cfg)
            engine.load.assert_called_once_with()
            engine.load.side_effect = RuntimeError("GPU unavailable")
            with self.assertLogs("mediaworker.engine.registry", level="ERROR"):
                self.assertEqual(build_registry(self.cfg, ["enhance"]), {})

    def test_enhance_mock_advertises_models_and_echoes_named_image_as_png(self):
        with patch.dict(os.environ, {"MEDIAWORKER_MOCK": "1"}):
            engine = build_registry(self.cfg, ["enhance"])["enhance"]
        self.assertEqual(
            engine.available_models, ["standard", "fast", "deblur", "animation", "portrait"]
        )
        self.cfg.enhance_face_models = []
        self.assertNotIn("portrait", engine.available_models)
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary)
            source = workdir / "image.png"
            Image.new("RGBA", (7, 5), (10, 20, 30, 100)).save(source)
            context = JobContext(
                "enhance-local-mock", "enhance", {},
                {"target": workdir / "must-not-use.jpg", "image": source}, workdir,
            )
            result = engine.run(context)
            self.assertEqual(result.content_type, "image/png")
            self.assertEqual((result.width, result.height), (7, 5))
            with Image.open(result.output_path) as output:
                self.assertEqual(output.format, "PNG")
                self.assertEqual(output.getpixel((0, 0)), (10, 20, 30, 100))


if __name__ == "__main__":
    unittest.main()
