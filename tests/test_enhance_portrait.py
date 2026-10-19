from __future__ import annotations

import unittest

import cv2
import numpy as np

from mediaworker.engine.enhance_portrait import restore_portraits


_LANDMARKS = np.array(
    [[192.98138, 239.94708], [318.90277, 240.1936], [256.63416, 314.01935],
     [201.26117, 371.41043], [313.08905, 371.15118]],
    dtype=np.float32,
)


def face_row(scale=0.3, offset=(30, 40), score=0.9, detector_scale=1):
    row = np.zeros(20, dtype=np.float32)
    row[:2] = (np.array(offset) + 256 * scale) * detector_scale
    row[2:4] = 512 * scale * detector_scale
    row[4] = score
    landmarks = row[5:].reshape(5, 3)
    landmarks[:, :2] = (_LANDMARKS * scale + offset) * detector_scale
    landmarks[:, 2] = 0.9
    return row


class StubModel:
    def __init__(self, shape, output):
        self.shape = shape
        self.output = output
        self.calls = []

    def run(self, rgb, divisor=255, bias=0, bgr=False):
        self.calls.append((rgb.copy(), divisor, bias, bgr))
        return self.output


def detector_for(*rows):
    output = np.stack(rows, axis=1)[None] if rows else np.zeros((1, 20, 0), dtype=np.float32)
    return StubModel((1, 3, 640, 640), output)


def restorer_for(size=512, color=(230, 40, 80)):
    channels = np.array(color, dtype=np.float32) / 127.5 - 1
    output = np.broadcast_to(channels[None, :, None, None], (1, 3, size, size)).copy()
    return StubModel((1, 3, size, size), output)


class PortraitTests(unittest.TestCase):
    def setUp(self):
        self.original = np.full((300, 200, 3), (10, 20, 30), dtype=np.uint8)
        self.enhanced = np.full((600, 400, 3), (60, 70, 80), dtype=np.uint8)

    def restore(self, detector, restorer, **options):
        params = {"strength": 1.0, "edge_blend": 0.0, "fusion_area": 50}
        params.update(options)
        return restore_portraits(self.original, self.enhanced, detector, restorer, **params)

    def test_no_face_preserves_base_result_and_small_images_are_not_upscaled(self):
        detector, restorer = detector_for(), restorer_for()
        result = self.restore(detector, restorer)
        np.testing.assert_array_equal(result, self.enhanced)
        self.assertFalse(np.shares_memory(result, self.enhanced))
        self.assertEqual(restorer.calls, [])
        padded, divisor, bias, bgr = detector.calls[0]
        self.assertEqual(padded.shape, (640, 640, 3))
        np.testing.assert_array_equal(padded[:300, :200], self.original)
        self.assertFalse(padded[300:].any())
        self.assertFalse(padded[:, 200:].any())
        self.assertEqual((divisor, bias, bgr), (128, -127.5, True))

    def test_both_portrait_models_restore_original_and_blend_at_enhanced_size(self):
        for size in (256, 512):
            with self.subTest(size=size):
                detector, restorer = detector_for(face_row()), restorer_for(size)
                result = self.restore(detector, restorer)
                self.assertEqual(result.shape, self.enhanced.shape)
                self.assertEqual(result.dtype, np.uint8)
                np.testing.assert_array_equal(result[234, 214], (230, 40, 80))
                np.testing.assert_array_equal(result[0, 0], (60, 70, 80))
                np.testing.assert_array_equal(result[500, 214], (60, 70, 80))
                crop, divisor, bias, bgr = restorer.calls[0]
                self.assertEqual(crop.shape, (size, size, 3))
                np.testing.assert_array_equal(crop[size // 2, size // 2], (10, 20, 30))
                self.assertEqual((divisor, bias, bgr), (127.5, -127.5, False))
        np.testing.assert_array_equal(self.original[150, 100], (10, 20, 30))
        np.testing.assert_array_equal(self.enhanced[234, 214], (60, 70, 80))

    def test_strength_zero_skips_all_inference_and_partial_strength_blends(self):
        detector, restorer = detector_for(face_row()), restorer_for()
        np.testing.assert_array_equal(self.restore(detector, restorer, strength=0), self.enhanced)
        self.assertEqual(detector.calls, [])
        self.assertEqual(restorer.calls, [])
        result = self.restore(detector, restorer, strength=0.5)
        np.testing.assert_array_equal(result[234, 214], (145, 55, 80))

    def test_fusion_area_and_edge_blur_keep_native_independent_controls(self):
        detector, restorer = detector_for(face_row()), restorer_for()
        full = self.restore(detector, restorer)
        inset = self.restore(detector, restorer, fusion_area=25)
        empty = self.restore(detector, restorer, fusion_area=0)
        blurred = self.restore(detector, restorer, edge_blend=0.8)
        # 这个位置在全脸范围内、缩小融合范围外；中央始终能修复。
        np.testing.assert_array_equal(full[234, 98], (230, 40, 80))
        np.testing.assert_array_equal(inset[234, 98], (60, 70, 80))
        np.testing.assert_array_equal(inset[234, 214], (230, 40, 80))
        np.testing.assert_array_equal(empty, self.enhanced)
        self.assertGreater(int(blurred[234, 132, 0]), 60)
        self.assertLess(int(blurred[234, 132, 0]), int(full[234, 132, 0]))

    def test_large_image_letterbox_restores_face_coordinates(self):
        self.original = np.full((720, 1280, 3), (10, 20, 30), dtype=np.uint8)
        self.enhanced = np.full_like(self.original, (60, 70, 80))
        detector = detector_for(face_row(scale=0.6, offset=(400, 180), detector_scale=0.5))
        result = self.restore(detector, restorer_for())
        padded = detector.calls[0][0]
        np.testing.assert_array_equal(padded[:360], cv2.resize(self.original, (640, 360)))
        self.assertFalse(padded[360:].any())
        np.testing.assert_array_equal(result[334, 554], (230, 40, 80))
        np.testing.assert_array_equal(result[150, 280], (60, 70, 80))

    def test_overlap_is_suppressed_before_restoration(self):
        detector = detector_for(face_row(score=0.7), face_row(score=0.9), face_row(score=0.5))
        restorer = restorer_for()
        self.restore(detector, restorer)
        self.assertEqual(len(restorer.calls), 1)

    def test_rotated_face_uses_both_output_scale_axes(self):
        self.original = np.full((400, 400, 3), (10, 20, 30), dtype=np.uint8)
        self.enhanced = np.full((600, 800, 3), (60, 70, 80), dtype=np.uint8)
        row = face_row()
        rotation = np.array([[0.8, -0.6], [0.6, 0.8]], dtype=np.float32)
        row[5:].reshape(5, 3)[:, :2] = _LANDMARKS @ rotation.T * 0.4 + (180, 30)
        row[:4] = (200, 175, 260, 260)
        result = self.restore(detector_for(row), restorer_for())
        center = np.array([256, 256]) @ rotation.T * 0.4 + (180, 30)
        x, y = np.rint(center * (2, 1.5)).astype(int)
        np.testing.assert_array_equal(result[y, x], (230, 40, 80))
        np.testing.assert_array_equal(result[0, 0], (60, 70, 80))

    def test_many_faces_keep_native_32_face_limit(self):
        self.original = np.full((480, 480, 3), (10, 20, 30), dtype=np.uint8)
        self.enhanced = np.full_like(self.original, (60, 70, 80))
        rows = [
            face_row(scale=0.1, offset=(10 + index % 6 * 75, 10 + index // 6 * 75))
            for index in range(33)
        ]
        restorer = restorer_for(size=256)
        result = self.restore(detector_for(*rows), restorer)
        self.assertEqual(len(restorer.calls), 32)
        np.testing.assert_array_equal(result[36, 36], (230, 40, 80))
        np.testing.assert_array_equal(result[411, 186], (60, 70, 80))

    def test_invalid_landmarks_and_small_faces_are_skipped(self):
        invalid_rows = []
        low_confidence = face_row()
        low_confidence[7] = 0.49
        invalid_rows.append(low_confidence)
        outside = face_row()
        outside[5] = -1
        invalid_rows.append(outside)
        close_eyes = face_row()
        close_eyes[8:10] = close_eyes[5:7]
        invalid_rows.append(close_eyes)
        inconsistent_geometry = face_row()
        inconsistent_geometry[5:].reshape(5, 3)[:, :2] = (
            (10, 10), (190, 280), (190, 10), (10, 280), (100, 150)
        )
        invalid_rows.append(inconsistent_geometry)
        invalid_rows.append(face_row(scale=0.03))
        for row in invalid_rows:
            with self.subTest(row=row):
                restorer = restorer_for()
                np.testing.assert_array_equal(self.restore(detector_for(row), restorer), self.enhanced)
                self.assertEqual(restorer.calls, [])

    def test_border_face_is_clipped_to_image_and_uses_replicated_crop_edges(self):
        detector, restorer = detector_for(face_row(scale=0.4, offset=(-20, -20))), restorer_for()
        result = self.restore(detector, restorer)
        np.testing.assert_array_equal(restorer.calls[0][0][0, 0], (10, 20, 30))
        np.testing.assert_array_equal(result[150, 150], (230, 40, 80))
        np.testing.assert_array_equal(result[-1, -1], (60, 70, 80))

    def test_model_shapes_and_prediction_layouts_fail_explicitly(self):
        detector, restorer = detector_for(face_row()), restorer_for()
        detector.shape = (1, 3, 320, 320)
        with self.assertRaisesRegex(ValueError, "model shape"):
            self.restore(detector, restorer)
        detector = detector_for(face_row())
        detector.output = np.zeros((1, 1, 20), dtype=np.float32)
        with self.assertRaisesRegex(ValueError, "detector output"):
            self.restore(detector, restorer)
        detector = detector_for(face_row())
        restorer.output = np.zeros((1, 3, 256, 256), dtype=np.float32)
        with self.assertRaisesRegex(ValueError, "portrait output"):
            self.restore(detector, restorer)


if __name__ == "__main__":
    unittest.main()
