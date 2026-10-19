from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ["MEDIAWORKER_LOAD_DOTENV"] = "0"

import cv2
import numpy as np
from PIL import Image, ImageOps

from mediaworker.config import Config
from mediaworker.engine.base import JobContext
from mediaworker.engine.enhance_engine import (
    EnhanceEngine,
    MODEL_SPECS,
    ModelSpec,
    OnnxModel,
    restore_image,
)


class IdentityModel:
    """Small deterministic RGB model: tests exercise real tiling and image I/O."""

    def __init__(self, spec, *, reference=False, dynamic=False, multiple=1):
        window = spec.tile + 2 * spec.padding
        self.shape = (1, 3, "height", "width") if dynamic else (1, 3, window, window)
        self.reference_tiles = reference
        self.multiple = multiple
        self.native_scale = spec.native_scale
        self.inputs = []

    def run(self, rgb):
        self.inputs.append(rgb.copy())
        # Stay away from integer boundaries to avoid floating-point roundoff in
        # the weighted reference path changing a pixel by one unit.
        output = (rgb.astype(np.float32) + 0.25) / 255
        output = output.repeat(self.native_scale, 0).repeat(self.native_scale, 1)
        return output.transpose(2, 0, 1)[None]


def rgb_pattern(height, width):
    values = np.arange(height * width, dtype=np.uint16).reshape(height, width)
    return np.stack((values * 3 % 251, values * 5 % 251, values * 7 % 251), -1).astype(np.uint8)


class TilingTests(unittest.TestCase):
    def test_reference_tiles_keep_rgb_edges_and_odd_dimensions(self):
        spec = ModelSpec("test.onnx", 2, 4, 1)
        rgb = rgb_pattern(9, 11)
        for scale in (1, 2):
            with self.subTest(scale=scale):
                model = IdentityModel(spec, reference=True)
                restored = restore_image(rgb, model, spec, scale)
                np.testing.assert_array_equal(restored, rgb.repeat(scale, 0).repeat(scale, 1))
                self.assertGreater(len(model.inputs), 1)
                self.assertTrue(all(tile.shape == (6, 6, 3) for tile in model.inputs))
                np.testing.assert_array_equal(model.inputs[0], rgb[:6, :6])
                np.testing.assert_array_equal(model.inputs[-1], rgb[-6:, -6:])

    def test_small_reference_image_pads_only_right_and_bottom(self):
        spec = ModelSpec("test.onnx", 1, 4, 1)
        for height, width in ((2, 3), (1, 1)):
            with self.subTest(size=(width, height)):
                model = IdentityModel(spec, reference=True)
                rgb = rgb_pattern(height, width)
                np.testing.assert_array_equal(restore_image(rgb, model, spec, 1), rgb)
                expected = cv2.copyMakeBorder(rgb, 0, 6 - height, 0, 6 - width, cv2.BORDER_REFLECT_101)
                np.testing.assert_array_equal(model.inputs[0], expected)

    def test_reference_overlap_blends_tile_predictions(self):
        spec = ModelSpec("test.onnx", 1, 2, 2)
        model = IdentityModel(spec, reference=True)
        model.run = Mock(side_effect=[np.zeros((1, 3, 6, 6), np.float32),
                                      np.ones((1, 3, 6, 6), np.float32)])
        restored = restore_image(np.zeros((6, 8, 3), np.uint8), model, spec, 1)
        expected = np.array([0, 0, 85, 127, 127, 170, 255, 255], np.uint8)
        np.testing.assert_array_equal(restored, np.broadcast_to(expected[None, :, None], (6, 8, 3)))

    def test_reference_quantizes_before_resizing_and_uses_truncation(self):
        spec = ModelSpec("test.onnx", 2, 2, 1)
        output = np.full((1, 3, 8, 8), 0.5, np.float32)
        rgb = np.zeros((4, 4, 3), np.uint8)
        for reference, expected in ((True, 127), (False, 128)):
            with self.subTest(reference=reference):
                model = IdentityModel(spec, reference=reference)
                model.run = Mock(return_value=output)
                restored = restore_image(rgb, model, spec, 1)
                np.testing.assert_array_equal(restored, np.full_like(rgb, expected))

        # Quantizing this 2x2 pattern first produces [0, 0; 0, 1], whose
        # uint8 area average is 0; resizing the floats first would produce 1.
        output = np.full((1, 3, 8, 8), 0.99 / 255, np.float32)
        output[:, :, 1::2, 1::2] = 1.99 / 255
        model = IdentityModel(spec, reference=True)
        model.run = Mock(return_value=output)
        np.testing.assert_array_equal(restore_image(rgb, model, spec, 1), np.zeros_like(rgb))

    def test_fixed_tiles_crop_context_without_seams(self):
        spec = ModelSpec("test.onnx", 2, 4, 1)
        model = IdentityModel(spec)
        rgb = rgb_pattern(7, 9)
        restored = restore_image(rgb, model, spec, 2)
        np.testing.assert_array_equal(restored, rgb.repeat(2, 0).repeat(2, 1))
        self.assertEqual(len(model.inputs), 6)
        self.assertTrue(all(tile.shape == (6, 6, 3) for tile in model.inputs))

    def test_dynamic_tiles_align_context_to_model_multiple(self):
        spec = ModelSpec("test.onnx", 4, 4, 1)
        model = IdentityModel(spec, dynamic=True, multiple=2)
        rgb = rgb_pattern(5, 7)
        restored = restore_image(rgb, model, spec, 4)
        np.testing.assert_array_equal(restored, rgb.repeat(4, 0).repeat(4, 1))
        self.assertGreater(len(model.inputs), 1)
        self.assertTrue(all(tile.shape[0] % 2 == tile.shape[1] % 2 == 0 for tile in model.inputs))

    def test_dynamic_tiles_honor_spec_multiple_without_model_metadata(self):
        spec = ModelSpec("test.onnx", 2, 4, 1, input_multiple=4)
        model = IdentityModel(spec, dynamic=True)
        rgb = rgb_pattern(5, 7)
        restored = restore_image(rgb, model, spec, 2)
        np.testing.assert_array_equal(restored, rgb.repeat(2, 0).repeat(2, 1))
        self.assertTrue(all(tile.shape[0] % 4 == tile.shape[1] % 4 == 0 for tile in model.inputs))

    def test_invalid_model_window_and_prediction_shape_are_rejected(self):
        spec = ModelSpec("test.onnx", 2, 4, 1)
        rgb = rgb_pattern(3, 3)
        model = IdentityModel(spec)
        model.shape = (1, 3, 8, 8)
        with self.assertRaisesRegex(ValueError, "tile size"):
            restore_image(rgb, model, spec, 1)
        model = IdentityModel(spec, dynamic=True, reference=True)
        with self.assertRaisesRegex(ValueError, "fixed input"):
            restore_image(rgb, model, spec, 1)
        model = IdentityModel(spec)
        model.run = Mock(return_value=np.zeros((1, 3, 6, 6), np.float32))
        with self.assertRaisesRegex(ValueError, "dimensions"):
            restore_image(rgb, model, spec, 1)


class OnnxModelTests(unittest.TestCase):
    def session(self, *, providers=None, shape=(1, 3, 6, 6), dtype="tensor(float)", metadata=None):
        session = Mock()
        session.get_providers.return_value = providers or ["DmlExecutionProvider"]
        session.get_inputs.return_value = [SimpleNamespace(name="input", shape=shape, type=dtype)]
        session.get_outputs.return_value = [SimpleNamespace(name="output")]
        session.get_modelmeta.return_value = SimpleNamespace(custom_metadata_map=metadata or {})
        return session

    def load(self, session):
        with patch("onnxruntime.get_available_providers", return_value=["DmlExecutionProvider", "CPUExecutionProvider"]), \
                patch("onnxruntime.InferenceSession", return_value=session) as create:
            model = OnnxModel(Path("offline.onnx"), ["CPUExecutionProvider", "DmlExecutionProvider"])
        return model, create

    def test_directml_session_disables_cpu_fallback(self):
        session = self.session(metadata={"clapclip.input_multiple": "2", "clapclip.reference_tiles": "1"})
        model, create = self.load(session)
        self.assertEqual(create.call_args.kwargs["providers"], ["DmlExecutionProvider"])
        options = create.call_args.kwargs["sess_options"]
        self.assertEqual(options.get_session_config_entry("session.disable_cpu_ep_fallback"), "1")
        self.assertFalse(options.enable_mem_pattern)
        session.disable_fallback.assert_called_once_with()
        self.assertEqual(model.multiple, 2)
        self.assertTrue(model.reference_tiles)

    def test_no_gpu_never_constructs_a_cpu_session(self):
        with patch("onnxruntime.get_available_providers", return_value=["CPUExecutionProvider"]), \
                patch("onnxruntime.InferenceSession") as create:
            with self.assertRaisesRegex(RuntimeError, "DirectML or CUDA"):
                OnnxModel(Path("offline.onnx"), ["DmlExecutionProvider", "CPUExecutionProvider"])
        create.assert_not_called()

    def test_session_that_silently_falls_back_to_cpu_is_rejected(self):
        session = self.session(providers=["CPUExecutionProvider"])
        with self.assertRaisesRegex(RuntimeError, "GPU provider was not loaded"):
            self.load(session)

    def test_float_and_half_input_preprocessing_and_output(self):
        rgb = np.array([[[3, 117, 241], [29, 65, 193]]], np.uint8)
        for dtype, np_dtype in (("tensor(float)", np.float32), ("tensor(float16)", np.float16)):
            with self.subTest(dtype=dtype):
                session = self.session(dtype=dtype)
                session.run.side_effect = lambda names, inputs: [inputs["input"]]
                model, _ = self.load(session)
                actual = model.run(rgb)
                expected = (rgb.astype(np_dtype).astype(np.float32) / 255).transpose(2, 0, 1)[None].astype(np_dtype)
                array = session.run.call_args.args[1]["input"]
                self.assertEqual(array.dtype, np_dtype)
                self.assertTrue(array.flags.c_contiguous)
                np.testing.assert_array_equal(array, expected)
                np.testing.assert_array_equal(actual, expected.astype(np.float32))

    def test_invalid_output_and_inference_errors_propagate_without_fallback(self):
        session = self.session()
        model, _ = self.load(session)
        for invalid in (np.zeros((1, 3, 1, 1), np.uint8), np.full((1, 3, 1, 1), np.nan)):
            session.run.return_value = [invalid]
            with self.assertRaisesRegex(ValueError, "non-finite or non-floating"):
                model.run(np.zeros((1, 1, 3), np.uint8))
        session.run.side_effect = RuntimeError("GPU inference failed")
        with patch("onnxruntime.InferenceSession") as create:
            with self.assertRaisesRegex(RuntimeError, "GPU inference failed"):
                model.run(np.zeros((1, 1, 3), np.uint8))
        create.assert_not_called()

    def test_invalid_model_input_or_alignment_is_rejected(self):
        for args in ({"shape": (1, 4, 6, 6)}, {"shape": (2, 3, 6, 6)},
                     {"dtype": "tensor(uint8)"}, {"metadata": {"clapclip.input_multiple": "0"}},
                     {"metadata": {"clapclip.input_multiple": "65"}}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.load(self.session(**args))


class EnhanceEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = Config.from_env(load_env=False)
        self.cfg.enhance_models_dir = self.root / "models"
        self.cfg.enhance_models_dir.mkdir()
        self.cfg.enhance_models = ["standard"]
        self.cfg.enhance_face_models = []
        self.cfg.onnx_providers = ["DmlExecutionProvider"]
        self.cfg.max_image_bytes = 1024 * 1024
        self.cfg.max_image_dimension = 1000
        self.cfg.max_image_pixels = self.cfg.max_output_pixels = 1_000_000
        self.engine = EnhanceEngine(self.cfg)
        self.spec = ModelSpec("test.onnx", 2, 4, 1)
        self.model = IdentityModel(self.spec, reference=True)
        self.engine._models["standard"] = [(self.spec, self.model)]
        self.source = self.root / "source.png"
        Image.fromarray(rgb_pattern(5, 7)).save(self.source)

    def ctx(self, params=None, source=None, name="job"):
        workdir = self.root / name
        workdir.mkdir(exist_ok=True)
        return JobContext(name, "enhance", params or {}, {"image": source or self.source}, workdir)

    def test_image_job_writes_png_at_requested_size_and_reuses_loaded_model(self):
        with patch("mediaworker.engine.enhance_engine.OnnxModel") as create:
            for scale in (1, 2):
                result = self.engine.run(self.ctx({"scale": scale}, name=f"job-{scale}"))
                self.assertEqual((result.width, result.height), (7 * scale, 5 * scale))
                self.assertEqual(result.content_type, "image/png")
                with Image.open(result.output_path) as output:
                    self.assertEqual(output.format, "PNG")
                    self.assertEqual(output.mode, "RGB")
                    np.testing.assert_array_equal(np.asarray(output), rgb_pattern(5, 7).repeat(scale, 0).repeat(scale, 1))
        create.assert_not_called()

    def test_alpha_channel_is_preserved_and_resized(self):
        rgba = np.concatenate((rgb_pattern(5, 7), np.arange(35, dtype=np.uint8).reshape(5, 7, 1) * 7), -1)
        Image.fromarray(rgba).save(self.source)
        for scale in (1, 2):
            with self.subTest(scale=scale):
                result = self.engine.run(self.ctx({"scale": scale}))
                with Image.open(result.output_path) as output:
                    self.assertEqual(output.mode, "RGBA")
                    expected = Image.fromarray(rgba[:, :, 3]).resize(output.size, Image.Resampling.LANCZOS)
                    np.testing.assert_array_equal(np.asarray(output.getchannel("A")), np.asarray(expected))

    def test_palette_png_transparency_is_preserved(self):
        image = Image.new("P", (7, 5))
        image.putpalette([100, 30, 5, 10, 120, 240] + [0] * 762)
        image.putdata([index % 2 for index in range(35)])
        image.save(self.source, transparency=0)
        with Image.open(self.source) as saved:
            expected = np.asarray(saved.convert("RGBA"))
        result = self.engine.run(self.ctx())
        with Image.open(result.output_path) as output:
            self.assertEqual(output.mode, "RGBA")
            np.testing.assert_array_equal(np.asarray(output), expected)

    def test_jpeg_exif_orientation_is_applied_once_and_metadata_is_stripped(self):
        source = self.root / "rotated.jpg"
        exif = Image.Exif()
        exif[274], exif[270] = 6, "source metadata"
        Image.fromarray(rgb_pattern(5, 7)).save(source, exif=exif)
        with Image.open(source) as opened:
            expected = np.asarray(ImageOps.exif_transpose(opened).convert("RGB"))
        result = self.engine.run(self.ctx(source=source))
        self.assertEqual((result.width, result.height), (5, 7))
        with Image.open(result.output_path) as output:
            np.testing.assert_array_equal(np.asarray(output), expected)
            self.assertFalse(output.getexif())
            self.assertEqual(ImageOps.exif_transpose(output).size, output.size)

    def test_single_frame_webp_is_accepted_and_animated_input_is_rejected(self):
        source = self.root / "source.webp"
        frame = Image.fromarray(rgb_pattern(5, 7))
        frame.save(source, lossless=True)
        result = self.engine.run(self.ctx(source=source))
        self.assertEqual((result.width, result.height), (7, 5))
        frame.save(source, save_all=True, append_images=[Image.new("RGB", frame.size, "red")], duration=100, loop=0)
        self.model.inputs.clear()
        with self.assertRaisesRegex(ValueError, "animated"):
            self.engine.run(self.ctx(source=source, name="animated"))
        self.assertFalse(self.model.inputs)
        self.assertFalse((self.root / "animated/result.png").exists())

    def test_unsupported_image_format_is_rejected_before_inference(self):
        source = self.root / "source.bmp"
        Image.new("RGB", (7, 5)).save(source)
        with self.assertRaisesRegex(ValueError, "unsupported image format"):
            self.engine.run(self.ctx(source=source))
        self.assertFalse(self.model.inputs)

    def test_output_and_intermediate_pixel_limits_precede_inference(self):
        self.cfg.max_output_pixels = 100
        with self.assertRaisesRegex(ValueError, "output exceeds"):
            self.engine.run(self.ctx({"scale": 2}))
        with self.assertRaisesRegex(ValueError, "intermediate image exceeds"):
            self.engine.run(self.ctx({"scale": 1}))
        self.assertFalse(self.model.inputs)

    def test_failed_inference_publishes_no_output_and_allows_next_job(self):
        ctx = self.ctx(name="failed")
        with patch.object(self.model, "run", side_effect=RuntimeError("GPU failed")):
            with self.assertRaisesRegex(RuntimeError, "GPU failed"):
                self.engine.run(ctx)
        self.assertFalse((ctx.workdir / "result.png").exists())
        self.assertTrue(self.engine.run(self.ctx(name="retry")).output_path.is_file())

    def test_invalid_parameters_fail_before_any_model_run(self):
        for params in ({"scale": True}, {"scale": 3}, {"scale": "2"},
                       {"model": "missing"}, {"model": []}, {"face": "missing"},
                       {"face": "gfpgan"}, {"faceStrength": float("nan")},
                       {"faceStrength": True}, {"faceStrength": 1.1}, {"faceEdgeBlend": -1},
                       {"faceFusionArea": 51}, {"faceFusionArea": 1.5}, {"unknown": 1}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.engine.run(self.ctx(params))
        self.assertFalse(self.model.inputs)

    def test_image_task_rejects_wrong_task_and_additional_inputs(self):
        ctx = self.ctx()
        ctx.task_type = "upscale"
        with self.assertRaisesRegex(ValueError, "enhancement tasks"):
            self.engine.run(ctx)
        ctx.task_type = "enhance"
        ctx.inputs["video"] = self.source
        with self.assertRaisesRegex(ValueError, "exactly one image"):
            self.engine.run(ctx)
        self.assertFalse(self.model.inputs)

    def test_animation_selects_native_four_times_model_only_for_four_times_output(self):
        spec4 = ModelSpec("animation4.onnx", 4, 4, 1)
        model4 = IdentityModel(spec4, dynamic=True, multiple=2)
        self.engine._models["animation"] = [(self.spec, self.model), (spec4, model4)]
        for scale in (1, 2, 4):
            with self.subTest(scale=scale):
                self.model.inputs.clear()
                model4.inputs.clear()
                result = self.engine.run(self.ctx({"model": "animation", "scale": scale}))
                self.assertEqual((result.width, result.height), (7 * scale, 5 * scale))
                self.assertEqual(bool(model4.inputs), scale == 4)
                self.assertEqual(bool(self.model.inputs), scale != 4)

    def test_base_model_scale_support_matches_image_protocol(self):
        deblur_spec = ModelSpec("deblur.onnx", 1, 4, 1)
        self.engine._models["deblur"] = [(deblur_spec, IdentityModel(deblur_spec, reference=True))]
        self.engine._models["fast"] = [(self.spec, self.model)]
        for name, allowed in (("standard", (1, 2, 4)), ("fast", (1, 2, 4)), ("deblur", (1,))):
            for scale in (1, 2, 4):
                with self.subTest(model=name, scale=scale):
                    ctx = self.ctx({"model": name, "scale": scale})
                    if scale in allowed:
                        result = self.engine.run(ctx)
                        self.assertEqual((result.width, result.height), (7 * scale, 5 * scale))
                    else:
                        with self.assertRaisesRegex(ValueError, "unsupported enhance scale"):
                            self.engine.run(ctx)

    def test_load_deduplicates_reuses_sessions_and_omits_missing_models(self):
        self.cfg.enhance_models = ["standard", "fast", "standard"]
        spec = MODEL_SPECS["standard"][0]
        (self.cfg.enhance_models_dir / spec.filename).touch()
        model = IdentityModel(spec, reference=True)
        engine = EnhanceEngine(self.cfg)
        with patch("mediaworker.engine.enhance_engine.OnnxModel", return_value=model) as create, \
                self.assertLogs("mediaworker.engine.enhance_engine", level="ERROR"):
            engine.load()
            engine.load()
        create.assert_called_once_with(self.cfg.enhance_models_dir / spec.filename, self.cfg.onnx_providers)
        self.assertEqual(engine.available_models, ["standard"])

    def test_unknown_portrait_configuration_fails_before_loading_any_model(self):
        self.cfg.enhance_face_models = ["gfpgan", "unknown"]
        engine = EnhanceEngine(self.cfg)
        with patch("mediaworker.engine.enhance_engine.OnnxModel") as create:
            with self.assertRaisesRegex(ValueError, "unsupported enhance model configuration"):
                engine.load()
        create.assert_not_called()
        self.assertEqual(engine.available_models, [])
        self.assertEqual(engine._models, {})
        self.assertEqual(engine._faces, {})
        self.assertIsNone(engine._detector)

    def test_load_omits_dynamic_reference_model_but_keeps_healthy_model(self):
        self.cfg.enhance_models = ["standard", "fast"]
        standard_spec, fast_spec = MODEL_SPECS["standard"][0], MODEL_SPECS["fast"][0]
        for spec in (standard_spec, fast_spec):
            (self.cfg.enhance_models_dir / spec.filename).touch()
        invalid = IdentityModel(standard_spec, reference=True, dynamic=True)
        healthy = IdentityModel(fast_spec, reference=True)
        engine = EnhanceEngine(self.cfg)
        with patch("mediaworker.engine.enhance_engine.OnnxModel", side_effect=[invalid, healthy]) as create, \
                self.assertLogs("mediaworker.engine.enhance_engine", level="ERROR"):
            engine.load()
        self.assertEqual(create.call_count, 2)
        self.assertEqual(engine.available_models, ["fast"])
        self.assertIs(engine._models["fast"][0][1], healthy)

    def test_unavailable_gpu_models_cannot_advertise_or_run(self):
        spec = MODEL_SPECS["standard"][0]
        (self.cfg.enhance_models_dir / spec.filename).touch()
        engine = EnhanceEngine(self.cfg)
        with patch("mediaworker.engine.enhance_engine.OnnxModel", side_effect=RuntimeError("no GPU")), \
                self.assertLogs("mediaworker.engine.enhance_engine", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "no enhance GPU models"):
                engine.load()
        self.assertEqual(engine.available_models, [])
        with self.assertRaisesRegex(RuntimeError, "not loaded"):
            engine.run(self.ctx())


if __name__ == "__main__":
    unittest.main()
