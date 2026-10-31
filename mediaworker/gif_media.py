"""Bounded GIF inspection and pixel-exact streaming encoding, without queue I/O."""
from dataclasses import dataclass
from pathlib import Path
import struct
import time
import warnings

from PIL import Image, ImageChops, GifImagePlugin


GIF_LIMITS = {"maxBytes": 6 * 1024 * 1024, "maxDimension": 1024,
              "maxFrames": 300, "maxTotalPixels": 60_000_000,
              "maxDurationMs": 30_000, "minFrameDurationMs": 20,
              "maxFrameDurationMs": 10_000, "maxResultBytes": 32 * 1024 * 1024,
              "maxProcessingSeconds": 120}


class GifDeadline:
    """Cooperative deadline covers validation, inference and encoding together."""
    def __init__(self, cancel):
        self.cancel = cancel
        self.deadline = time.monotonic() + GIF_LIMITS["maxProcessingSeconds"]

    def is_set(self):
        if self.cancel.is_set():
            return True
        if time.monotonic() >= self.deadline:
            raise RuntimeError("GIF exceeds processing time budget")
        return False


@dataclass(frozen=True)
class GifInfo:
    width: int
    height: int
    durations: tuple[int, ...]
    loop: int | None


def check_cancel(cancel):
    if cancel.is_set():
        raise RuntimeError("GIF processing cancelled")


def inspect_gif(path: Path, cfg, cancel) -> GifInfo:
    """Scan bounded compressed blocks before Pillow allocates any raster."""
    if not 0 < path.stat().st_size <= min(cfg.max_image_bytes, GIF_LIMITS["maxBytes"]):
        raise ValueError("GIF exceeds byte limit")
    data = path.read_bytes()
    offset = 0

    def take(count):
        nonlocal offset
        if count < 0 or offset + count > len(data):
            raise ValueError("truncated GIF")
        result = data[offset:offset + count]
        offset += count
        return result

    def blocks():
        result = bytearray()
        while True:
            check_cancel(cancel)
            size = take(1)[0]
            if not size:
                return bytes(result)
            result.extend(take(size))

    if take(6) not in (b"GIF87a", b"GIF89a"):
        raise ValueError("targetMedia='gif' requires GIF bytes")
    width, height, packed, _, _ = struct.unpack("<HHBBB", take(7))
    if (min(width, height) < 1 or max(width, height) > min(cfg.max_image_dimension, GIF_LIMITS["maxDimension"])
            or width * height > min(cfg.max_image_pixels, cfg.max_output_pixels)):
        raise ValueError("GIF exceeds canvas limit")
    if packed & 128:
        take(3 * (2 << (packed & 7)))
    durations, loop, pending = [], None, None
    while True:
        check_cancel(cancel)
        marker = take(1)
        if marker == b";":
            if offset != len(data):
                raise ValueError("trailing GIF data")
            break
        if marker == b"!":
            label = take(1)
            if label == b"\xf9":
                if take(1) != b"\x04":
                    raise ValueError("invalid GIF control block")
                control, delay, _ = struct.unpack("<BHB", take(4))
                if take(1) != b"\0" or (control >> 2) & 7 > 3:
                    raise ValueError("invalid GIF disposal")
                pending = delay * 10
            elif label == b"\xff":
                app = take(take(1)[0])
                body = blocks()
                if app in (b"NETSCAPE2.0", b"ANIMEXTS1.0"):
                    if len(body) != 3 or body[0] != 1 or loop is not None:
                        raise ValueError("invalid GIF loop extension")
                    loop = int.from_bytes(body[1:], "little")
            elif label == b"\xfe":
                blocks()  # Comments never reach the output.
            else:
                raise ValueError("unsupported GIF extension")
            continue
        if marker != b",":
            raise ValueError("invalid GIF block")
        x, y, w, h, flags = struct.unpack("<HHHHB", take(9))
        if min(w, h) < 1 or x + w > width or y + h > height:
            raise ValueError("GIF frame outside canvas")
        if flags & 128:
            take(3 * (2 << (flags & 7)))
        if not 2 <= take(1)[0] <= 8:
            raise ValueError("invalid GIF LZW code size")
        duration = 100 if pending is None else pending
        # Match browser playback for missing/0/10 ms delays, then budget the
        # effective duration. Normal GIF delays remain unchanged.
        if duration <= 10:
            duration = 100
        if not GIF_LIMITS["minFrameDurationMs"] <= duration <= GIF_LIMITS["maxFrameDurationMs"]:
            raise ValueError("GIF frame duration must be 20..10000 ms")
        durations.append(duration)
        pending = None
        if (len(durations) > GIF_LIMITS["maxFrames"]
                or len(durations) * width * height > GIF_LIMITS["maxTotalPixels"]
                or sum(durations) > GIF_LIMITS["maxDurationMs"]):
            raise ValueError("GIF exceeds frame, total pixel or duration limit")
        blocks()
    if len(durations) < 2:
        raise ValueError("GIF target must contain at least two frames")
    info = GifInfo(width, height, tuple(durations), loop)
    # Validate every compressed frame before spending GPU time. Pillow applies
    # source disposal to produce composited frames; never copy disposal to output.
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            for index in range(len(durations)):
                check_cancel(cancel)
                image.seek(index)
                image.load()
                if image.size != (width, height):
                    raise ValueError("GIF decoder canvas mismatch")
            try:
                image.seek(len(durations))
            except EOFError:
                pass
            else:
                raise ValueError("GIF decoder frame count mismatch")
    return info


class GifWriter:
    """Bounded lookahead: preserve every quantized display frame, encode changes."""
    def __init__(self, path, info, limit, cancel):
        self.path, self.info, self.limit, self.cancel = path, info, limit, cancel
        self.file = path.open("wb")
        self.count = 0
        self._pending = None
        self._previous_rgb = None
        self._first_transparent = False

    def _write(self, chunks):
        for chunk in chunks:
            check_cancel(self.cancel)
            if self.file.tell() + len(chunk) > self.limit:
                raise ValueError("GIF result exceeds byte limit")
            self.file.write(chunk)

    def append(self, rgba, duration):
        check_cancel(self.cancel)
        frame = rgba.convert("RGB").quantize(colors=255, dither=Image.Dither.NONE)
        palette = frame.getpalette()
        frame.putpalette(palette[:765] + [0] * (768 - len(palette[:765])))
        alpha = rgba.getchannel("A")
        frame.paste(255, mask=alpha.point([255] * 128 + [0] * 128))
        frame.info.clear()
        transparent = alpha.getextrema()[0] < 128
        if self._pending is not None:
            self._flush(transparent)
        else:
            self._first_transparent = transparent
        self._pending = (frame, duration, transparent)

    def _flush(self, next_transparent):
        frame, duration, transparent = self._pending
        # A frame before transparency must clear the FULL canvas, not merely a
        # delta rectangle. One-frame lookahead permits this without ghosting.
        clear = transparent or next_transparent
        rgb = frame.convert("RGB") if not transparent else None
        encoded, offset, local_palette = frame, (0, 0), True
        if not clear and self._previous_rgb is not None:
            difference = ImageChops.difference(rgb, self._previous_rgb)
            bbox = difference.getbbox()
            if bbox is None:
                # Retain a real frame and its own delay, even when unchanged.
                encoded = frame.crop((0, 0, 1, 1))
                encoded.paste(255, (0, 0, 1, 1))
                local_palette = False  # Only global transparency index 255 is used.
            else:
                encoded = frame.crop(bbox)
                red, green, blue = difference.crop(bbox).split()
                unchanged = ImageChops.lighter(ImageChops.lighter(red, green), blue).point([255] + [0] * 255)
                encoded.paste(255, mask=unchanged)
                offset = bbox[:2]
        if self.count == 0:
            options = {"background": 255, "transparency": 255, "optimize": False}
            if self.info.loop is not None:
                options["loop"] = self.info.loop
            self._write(GifImagePlugin.getheader(frame, info=options)[0])
        chunks = GifImagePlugin.getdata(encoded, offset=offset, duration=duration, disposal=2 if clear else 1,
                                       transparency=255, include_color_table=local_palette)
        if encoded is not frame and local_palette:
            # High-motion or noisy frames may compress better without a sparse
            # transparent delta. Never expand output just to use a delta frame.
            full = GifImagePlugin.getdata(frame, duration=duration, disposal=1,
                                         transparency=255, include_color_table=True)
            if sum(map(len, full)) < sum(map(len, chunks)):
                chunks = full
        self._write(chunks)
        self.count += 1
        self._previous_rgb = None if clear else rgb
        self._pending = None

    def close(self, success=False):
        try:
            if success:
                if self._pending is not None:
                    # Clear the final full frame before a transparent first
                    # frame is replayed in a finite or infinite loop.
                    self._flush(self._first_transparent)
                self._write([b";"])
        except BaseException:
            success = False
            raise
        finally:
            self.file.close()
            if not success:
                self.path.unlink(missing_ok=True)
