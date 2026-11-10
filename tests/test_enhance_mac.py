"""Mac inference with generated models, never private media or configuration."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
from PIL import Image
from mediaworker.config import Config
from mediaworker.engine.base import JobContext
from mediaworker.engine.enhance_engine import EnhanceEngine, OnnxModel, enhance_providers
from mediaworker import deployment

class MacProviderTests(unittest.TestCase):
    def test_platform_admission_honors_explicit_selection(self):
        providers = ['CoreMLExecutionProvider', 'CPUExecutionProvider']
        for platform, expected in [('darwin', providers), ('win32', []), ('linux', [])]:
            with self.subTest(platform=platform), patch('sys.platform', platform):
                self.assertEqual(enhance_providers(providers, providers), expected)
        with patch('sys.platform', 'darwin'):
            self.assertEqual(enhance_providers(['CoreMLExecutionProvider'], ['CPUExecutionProvider']), [])

    def test_coreml_cpu_partition_is_explicit_and_inference_errors_do_not_retry(self):
        session = Mock()
        session.get_providers.return_value = ['CoreMLExecutionProvider', 'CPUExecutionProvider']
        session.get_inputs.return_value = [SimpleNamespace(name='input', shape=[1, 3, 4, 4], type='tensor(float)')]
        session.get_outputs.return_value = [SimpleNamespace(name='output')]
        session.get_modelmeta.return_value = SimpleNamespace(custom_metadata_map={})
        with patch('sys.platform', 'darwin'), patch('onnxruntime.get_available_providers', return_value=session.get_providers()), \
             patch('onnxruntime.InferenceSession', return_value=session) as create:
            model = OnnxModel(Path('generated.onnx'), ['CoreMLExecutionProvider', 'CPUExecutionProvider'])
            self.assertEqual(create.call_args.kwargs['providers'], session.get_providers())
            session.disable_fallback.assert_called_once()
            session.run.side_effect = RuntimeError('inference failure')
            with self.assertRaisesRegex(RuntimeError, 'inference failure'):
                model.run(np.zeros((4, 4, 3), np.uint8))
            self.assertEqual(create.call_count, 1)
            # Without explicit CPU selection, graph partition fallback stays disabled.
            session.get_providers.return_value = ['CoreMLExecutionProvider']
            OnnxModel(Path('generated.onnx'), ['CoreMLExecutionProvider'])
            self.assertEqual(create.call_args.kwargs['providers'], ['CoreMLExecutionProvider'])
            self.assertEqual(create.call_args.kwargs['sess_options'].get_session_config_entry('session.disable_cpu_ep_fallback'), '1')

    def test_nafnet_selects_configured_cpu_before_inference(self):
        session = Mock()
        session.get_providers.return_value = ['CPUExecutionProvider']
        session.get_inputs.return_value = [SimpleNamespace(name='input', shape=[1, 3, 512, 512], type='tensor(float)')]
        session.get_outputs.return_value = [SimpleNamespace(name='output')]
        session.get_modelmeta.return_value = SimpleNamespace(custom_metadata_map={})
        path = Path('nafnet_deblur_512_fp32.onnx')
        with patch('sys.platform', 'darwin'), patch('onnxruntime.get_available_providers', return_value=['CoreMLExecutionProvider', 'CPUExecutionProvider']), \
             patch('onnxruntime.InferenceSession', return_value=session) as create:
            model = OnnxModel(path, ['CoreMLExecutionProvider', 'CPUExecutionProvider'])
            self.assertEqual(model.provider, 'CPUExecutionProvider')
            self.assertEqual(create.call_args.kwargs['providers'], ['CPUExecutionProvider'])
            self.assertEqual(create.call_count, 1)
            create.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'configured CoreML/CPU'):
                OnnxModel(path, ['CoreMLExecutionProvider'])
            create.assert_not_called()

    def test_doctor_requires_models_before_readiness(self):
        fake = SimpleNamespace(get_available_providers=lambda: ['CoreMLExecutionProvider', 'CPUExecutionProvider'])
        with deployment.configuration(None) as cfg, patch('sys.platform', 'darwin'), \
             patch.object(deployment, 'platform_problem', return_value=None), \
             patch.object(deployment, 'import_module', return_value=fake), \
             patch.object(deployment, 'model_inventory', return_value={'missing': ['generated.onnx']}):
            cfg.capabilities = ['enhance']
            cfg.onnx_providers = ['CoreMLExecutionProvider', 'CPUExecutionProvider']
            report = deployment.inspect(cfg, mode='local', explicit=False)
            self.assertEqual(report['capabilities']['enhance']['status'], 'missing_models')
            self.assertEqual(report['ready_capabilities'], [])

@unittest.skipUnless(sys.platform == 'darwin', 'native Mac execution')
class MacInferenceTests(unittest.TestCase):
    def test_generated_engine_inference(self):
        import onnx
        from onnx import TensorProto, helper, numpy_helper
        import onnxruntime as ort
        for providers in (['CPUExecutionProvider'], ['CoreMLExecutionProvider', 'CPUExecutionProvider']):
            if not set(providers) <= set(ort.get_available_providers()):
                self.fail('Mac runtime lacks expected provider')
            with self.subTest(providers=providers), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                graph = helper.make_graph([helper.make_node('Add', ['input', 'offset'], ['output'])], 'generated',
                    [helper.make_tensor_value_info('input', TensorProto.FLOAT, [1, 3, 'h', 'w'])],
                    [helper.make_tensor_value_info('output', TensorProto.FLOAT, [1, 3, 'h', 'w'])],
                    [numpy_helper.from_array(np.array([0.1], np.float32), name='offset')])
                model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17)])
                model.ir_version = 9
                onnx.save(model, root / 'nafnet_deblur_512_fp32.onnx')
                # Exercise CoreML itself too: production NAFNet deliberately selects CPU.
                onnx.save(model, root / 'generated_rgb.onnx')
                native = OnnxModel(root / 'generated_rgb.onnx', providers)
                self.assertEqual(native.provider, providers[0])
                rgb = np.full((19, 27, 3), 50, np.uint8)
                np.testing.assert_allclose(native.run(rgb), (50 / 255) + 0.1, atol=1e-6)
                cfg = Config.from_env(load_env=False)
                cfg.enhance_models_dir = root
                cfg.enhance_models, cfg.enhance_face_models = ['deblur'], []
                cfg.onnx_providers = providers
                engine = EnhanceEngine(cfg); engine.load()
                self.assertEqual(engine.available_models, ['deblur'])
                source = root / 'image.png'
                Image.fromarray(np.full((19, 27, 3), 50, np.uint8)).save(source)
                result = engine.run(JobContext('generated', 'enhance', {'model': 'deblur'}, {'image': source}, root))
                self.assertEqual((result.width, result.height), (27, 19))
                with Image.open(result.output_path) as image:
                    np.testing.assert_allclose(np.asarray(image), 76, atol=1)
