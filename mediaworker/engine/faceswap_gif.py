"""Explicit animated target adapter; static browser crop protocol stays intact."""
import logging

import cv2
from dataclasses import replace
import numpy as np
from PIL import Image

from ..gif_media import GIF_LIMITS, GifDeadline, GifWriter, check_cancel, inspect_gif
from ..images import validate_image
from .base import RunResult, resolve_providers, session_options
from .enhance_portrait import _face_indices


log = logging.getLogger(__name__)


class NoUsableGifFace(ValueError):
    """No face met the current detector/identity gates; not proof of no face."""


class GifFaceSwap:
    def __init__(self, cfg, engine):
        self.cfg, self.engine = cfg, engine

    def load(self):
        import onnxruntime as ort
        providers = resolve_providers(self.cfg.onnx_providers)
        self.detector = ort.InferenceSession(
            str(self.cfg.enhance_models_dir / "yoloface_8n.onnx"),
            providers=providers, sess_options=session_options(providers))
        inputs = self.detector.get_inputs()
        if len(inputs) != 1 or tuple(inputs[0].shape) != (1, 3, 640, 640):
            raise ValueError("invalid GIF face detector input")
        if inputs[0].type not in ("tensor(float)", "tensor(float16)"):
            raise ValueError("invalid GIF face detector dtype")
        self.input_name = inputs[0].name
        self.dtype = np.float16 if inputs[0].type == "tensor(float16)" else np.float32
        # Actually run the detector before declaring support, not just find a file.
        self._faces(np.zeros((128, 128, 3), dtype=np.uint8))

    def _faces(self, bgr):
        from insightface.app.common import Face
        height, width = bgr.shape[:2]
        factor = min(1, 640 / max(width, height))
        dw, dh = max(1, int(width * factor)), max(1, int(height * factor))
        padded = np.zeros((640, 640, 3), dtype=np.uint8)
        padded[:dh, :dw] = cv2.resize(bgr, (dw, dh))
        array = ((padded.astype(np.float32) - 127.5) / 128).transpose(2, 0, 1)[None]
        prediction = np.asarray(self.engine._infer(self.detector.run, None,
            {self.input_name: np.ascontiguousarray(array, dtype=self.dtype)})[0])
        if prediction.ndim != 3 or prediction.shape[:2] != (1, 20) or not np.isfinite(prediction).all():
            raise ValueError("invalid GIF face detector output")
        rows = prediction[0]
        faces = []
        for i in sorted(_face_indices(rows), key=lambda i: rows[2, i] * rows[3, i], reverse=True)[:8]:
            center, size = rows[:2, i] * [width / dw, height / dh], rows[2:4, i] * [width / dw, height / dh]
            points = rows[5:, i].reshape(5, 3)
            kps = points[:, :2] * [width / dw, height / dh]
            if (min(size) < 24 or (points[:, 2] < .5).any() or (kps < 0).any()
                    or (kps >= [width, height]).any() or np.linalg.norm(kps[1] - kps[0]) < 4):
                continue
            faces.append(Face(kps=kps.astype(np.float32), bbox=np.concatenate((center - size / 2, center + size / 2))))
        return faces

    def _select(self, image, reference, cancel, diagnostics=None):
        faces = self._faces(image)
        if not faces:
            if diagnostics is not None:
                diagnostics["no_usable_detection"] += 1
            return None, reference
        candidates = []
        for face in faces if reference is not None else faces[:1]:
            check_cancel(cancel)
            self.engine._infer(self.engine._recognition.get, image, face)
            embedding = np.asarray(face.normed_embedding)
            if embedding.ndim != 1 or not np.isfinite(embedding).all() or np.linalg.norm(embedding) < .9:
                raise ValueError("invalid GIF face identity")
            if reference is None:
                return face, embedding.copy()
            candidates.append((float(embedding @ reference), face))
        candidates.sort(key=lambda row: row[0], reverse=True)
        # A fixed reference prevents drift. Ambiguous or missing identities keep
        # the frame unchanged; never switch to whichever face is now largest.
        if candidates[0][0] < .4 or len(candidates) > 1 and candidates[0][0] - candidates[1][0] < .05:
            if diagnostics is not None:
                diagnostics["identity_rejected"] += 1
            return None, reference
        return candidates[0][1], reference

    def run(self, ctx):
        from insightface.app.common import Face
        from insightface.utils.face_align import arcface_dst
        from .faceswap_engine import SWAP_MODELS, _clip_setting
        ctx = replace(ctx, cancel=GifDeadline(ctx.cancel))
        if ctx.params.get("mode") != "gif" or ctx.params.get("targetMedia") != "gif":
            raise ValueError("GIF requires mode='gif' and targetMedia='gif'")
        protocol = ctx.params.get("gifProtocol", 1)
        if type(protocol) is not int or protocol != 1 or ctx.params.get("outputFormat", "gif") != "gif":
            raise ValueError("GIF requires gifProtocol=1 and outputFormat='gif'")
        if ctx.params.get("faceSelection", "primary") != "primary":
            raise ValueError("GIF supports faceSelection='primary' only")
        model_key = str(ctx.params.get("model") or "inswapper_128")
        if model_key not in self.engine.available_models:
            raise ValueError("GIF swap model is not loaded")
        spec = SWAP_MODELS[model_key]
        target, source = ctx.inputs["target"], ctx.inputs["source"]
        info = inspect_gif(target, self.cfg, ctx.cancel)
        if validate_image(source, self.cfg) != (spec.size, spec.size):
            raise ValueError(f"aligned source must be {spec.size}x{spec.size}")
        check_cancel(ctx.cancel)
        source_image = cv2.imread(str(source))
        if source_image is None:
            raise ValueError("failed to decode GIF source crop")
        points = arcface_dst.copy()
        points[:, 0] += 8
        source_face = Face(kps=points * (spec.size / 128))
        self.engine._infer(self.engine._recognition.get, source_image, source_face)
        blur = _clip_setting(ctx.params.get("edgeBlur"), 0, 1, .4)
        area = int(round(_clip_setting(ctx.params.get("fusionArea"), 0, 50, 50)))
        output = ctx.workdir / "result.gif"
        writer = GifWriter(output, info, min(self.cfg.max_result_bytes, GIF_LIMITS["maxResultBytes"]), ctx.cancel)
        reference, swapped, success = None, 0, False
        diagnostics = {"no_usable_detection": 0, "identity_rejected": 0}
        try:
            with Image.open(target) as image:
                for index, duration in enumerate(info.durations):
                    check_cancel(ctx.cancel)
                    image.seek(index)
                    rgba = image.convert("RGBA")
                    bgr = np.asarray(rgba.convert("RGB"))[..., ::-1].copy()
                    face, reference = self._select(bgr, reference, ctx.cancel, diagnostics)
                    check_cancel(ctx.cancel)
                    if face is not None:
                        if spec.kind == "inswapper":
                            result = self.engine._swap_inswapper(bgr, face, source_face, blur, area, True)
                        else:
                            result = self.engine._swap_hyperswap(model_key, bgr, face, source_face, blur, area, True)
                        check_cancel(ctx.cancel)
                        processed = Image.fromarray(result[..., ::-1])
                        processed.putalpha(rgba.getchannel("A"))
                        rgba = processed
                        swapped += 1
                    writer.append(rgba, duration)
                    ctx.progress("faceswap", int(95 * (index + 1) / len(info.durations)))
            if not swapped:
                log.warning("job %s GIF face selection frames=%d matched=0 no_usable_detection=%d identity_rejected=%d",
                            ctx.job_id, len(info.durations), diagnostics["no_usable_detection"], diagnostics["identity_rejected"])
                raise NoUsableGifFace("NO_USABLE_TARGET_FACE")
            check_cancel(ctx.cancel)
            success = True
        finally:
            writer.close(success)
        return RunResult(output, info.width, info.height, "image/gif")
