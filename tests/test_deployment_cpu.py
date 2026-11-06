"""Real native runtime smoke test; generated data only, no model downloads."""
from importlib.util import find_spec
from pathlib import Path
import tempfile
import unittest


@unittest.skipUnless(find_spec("onnx") and find_spec("onnxruntime") and find_spec("numpy"),
                     "ONNX/ORT/numpy are intentionally absent in the minimal download environment")
class PortableCpuTests(unittest.TestCase):
    def test_generated_onnx_model_runs_on_cpu_with_expected_output(self):
        # Installed-but-broken native modules must fail here, not be skipped.
        import numpy as np
        import onnx
        from onnx import TensorProto, helper
        import onnxruntime as ort

        graph = helper.make_graph(
            [helper.make_node("Add", ["input", "offset"], ["output"])],
            "portable-native-smoke",
            [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3])],
            [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3])],
            [helper.make_tensor("offset", TensorProto.FLOAT, [1, 3], [1, 2, 3])],
        )
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)])
        model.ir_version = 10  # Supported by both the portable 1.23.2 and existing DML 1.22.0.
        onnx.checker.check_model(model)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.onnx"
            onnx.save(model, path)
            session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
            self.assertIn("CPUExecutionProvider", session.get_providers())
            output = session.run(["output"], {"input": np.array([[2, 4, 8]], dtype=np.float32)})[0]
        np.testing.assert_array_equal(output, np.array([[3, 6, 11]], dtype=np.float32))


if __name__ == "__main__":
    unittest.main()
