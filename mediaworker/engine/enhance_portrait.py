from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    from .enhance_engine import OnnxModel


# 与本地 Enhance 的 GFPGAN/GPEN 五点模板一致，GPEN 按 256/512 缩放。
_FACE_TARGET = np.array(
    [
        [192.98138, 239.94708],
        [318.90277, 240.1936],
        [256.63416, 314.01935],
        [201.26117, 371.41043],
        [313.08905, 371.15118],
    ],
    dtype=np.float32,
)


def _face_indices(rows: np.ndarray) -> list[int]:
    candidates = np.flatnonzero(rows[4] >= 0.6)
    order = candidates[np.argsort(-rows[4, candidates], kind="stable")][:1000]
    boxes: list[np.ndarray] = []
    keep: list[int] = []
    for index in order:
        center, size = rows[:2, index], rows[2:4, index]
        box = np.concatenate((center - size / 2, center + size / 2))
        area = float(size[0] * size[1])
        suppressed = False
        for previous in boxes:
            overlap = np.maximum(
                0, np.minimum(box[2:], previous[2:]) - np.maximum(box[:2], previous[:2])
            )
            intersection = float(overlap[0] * overlap[1])
            previous_size = previous[2:] - previous[:2]
            union = area + float(previous_size[0] * previous_size[1]) - intersection
            if intersection / (union + 1e-6) > 0.4:
                suppressed = True
                break
        if not suppressed:
            boxes.append(box)
            keep.append(int(index))
    return keep


def _alignment(
    row: np.ndarray, source_width: int, source_height: int,
    detector_width: int, detector_height: int, size: int,
) -> np.ndarray | None:
    if min(
        row[2] * source_width / detector_width,
        row[3] * source_height / detector_height,
    ) < 24:
        return None
    landmarks = row[5:].reshape(5, 3)
    points = landmarks[:, :2].copy()
    points[:, 0] = points[:, 0] * source_width / detector_width
    points[:, 1] = points[:, 1] * source_height / detector_height
    if (
        np.any(landmarks[:, 2] < 0.5)
        or np.any(points < 0)
        or np.any(points[:, 0] >= source_width)
        or np.any(points[:, 1] >= source_height)
        or np.linalg.norm(points[1] - points[0]) < 4
    ):
        return None

    # 解四参数相似变换，保留原生实现的五点残差检查，不使用随机 RANSAC。
    design = np.zeros((10, 4), dtype=np.float64)
    design[0::2, 0] = points[:, 0]
    design[0::2, 1] = -points[:, 1]
    design[0::2, 2] = 1
    design[1::2, 0] = points[:, 1]
    design[1::2, 1] = points[:, 0]
    design[1::2, 3] = 1
    target = (_FACE_TARGET.astype(np.float64) / 512 * size).reshape(10, 1)
    solved, fit = cv2.solve(design, target, flags=cv2.DECOMP_SVD)
    if not solved or np.linalg.norm(design @ fit - target) / np.sqrt(10) > size * 0.08:
        return None
    a, b, tx, ty = fit[:, 0]
    return np.array([[a, -b, tx], [b, a, ty]], dtype=np.float32)


def _fusion_mask(size: int, strength: float, edge_blend: float, fusion_area: int) -> np.ndarray:
    # 边缘模糊和融合面积是独立参数，数值/取整与 SuperResolution 保持一致。
    blur_amount = int(size * 0.5 * edge_blend)
    padding = max(blur_amount // 2, 1, size * (50 - fusion_area) // 100)
    mask = np.zeros((size, size), dtype=np.float32)
    if size > 2 * padding:
        mask[padding:size - padding, padding:size - padding] = 1
    if blur_amount > 0:
        mask = cv2.GaussianBlur(mask, (0, 0), blur_amount * 0.25)
    return mask * strength


def _blend_face(
    result: np.ndarray, face: np.ndarray, mask: np.ndarray,
    affine: np.ndarray, source_width: int, source_height: int,
) -> bool:
    height, width = result.shape[:2]
    size = face.shape[0]
    inverse = cv2.invertAffineTransform(affine)
    inverse[0] *= width / source_width
    inverse[1] *= height / source_height
    corners = np.array([[[0, 0], [size, 0], [0, size], [size, size]]], dtype=np.float32)
    warped_corners = cv2.transform(corners, inverse)[0]
    x0, y0 = np.maximum(0, np.floor(warped_corners.min(axis=0))).astype(int)
    x1, y1 = np.minimum([width, height], np.ceil(warped_corners.max(axis=0))).astype(int)
    if x1 <= x0 or y1 <= y0:
        return False
    inverse[:, 2] -= [x0, y0]
    warped = cv2.warpAffine(face, inverse, (x1 - x0, y1 - y0))
    alpha = cv2.warpAffine(mask, inverse, (x1 - x0, y1 - y0))[:, :, None]
    destination = result[y0:y1, x0:x1]
    mixed = destination * (1 - alpha) + warped * alpha
    # OpenCV saturate_cast<uint8_t> 先舍入再截断范围，而非直接截去小数。
    destination[:] = np.clip(np.rint(mixed), 0, 255).astype(np.uint8)
    return True


def restore_portraits(
    original_rgb: np.ndarray,
    enhanced_rgb: np.ndarray,
    detector: OnnxModel,
    restorer: OnnxModel,
    *,
    strength: float,
    edge_blend: float,
    fusion_area: int,
) -> np.ndarray:
    """Detect/restore on the original RGB image and blend at the enhanced size.

    The caller validates image/option limits and owns GPU sessions. Neither
    input is modified; absence of a usable face keeps the base result intact.
    """
    result = enhanced_rgb.copy()
    if strength == 0:
        return result
    if tuple(detector.shape) != (1, 3, 640, 640) or tuple(restorer.shape) not in (
        (1, 3, 256, 256), (1, 3, 512, 512),
    ):
        raise ValueError("Invalid portrait model shape")
    size = int(restorer.shape[2])
    height, width = original_rgb.shape[:2]
    factor = min(1.0, 640 / max(width, height))
    detector_width = max(1, int(width * factor))
    detector_height = max(1, int(height * factor))
    padded = np.zeros((640, 640, 3), dtype=np.uint8)
    padded[:detector_height, :detector_width] = cv2.resize(
        original_rgb, (detector_width, detector_height), interpolation=cv2.INTER_LINEAR
    )
    # 此 YOLO 人脸模型使用 BGR[-1,1]，不是普通 YOLO 的 RGB[0,1]。
    prediction = detector.run(padded, divisor=128, bias=-127.5, bgr=True)
    if prediction.ndim != 3 or prediction.shape[:2] != (1, 20):
        raise ValueError("Invalid detector output")
    rows = prediction[0].astype(np.float32, copy=False)
    mask = _fusion_mask(size, strength, edge_blend, fusion_area)
    count = 0
    for index in _face_indices(rows):
        affine = _alignment(rows[:, index], width, height, detector_width, detector_height, size)
        if affine is None:
            continue
        crop = cv2.warpAffine(
            original_rgb, affine, (size, size), flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
        restored = restorer.run(crop, divisor=127.5, bias=-127.5)
        if restored.shape != (1, 3, size, size):
            raise ValueError("Invalid portrait output")
        face = np.clip(
            (restored[0].transpose(1, 2, 0).astype(np.float32) + 1) * 127.5,
            0, 255,
        )
        if _blend_face(result, face, mask, affine, width, height):
            count += 1
            if count == 32:
                break
    return result
