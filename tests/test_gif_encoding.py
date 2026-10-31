"""Lossless display-frame checks for bounded GIF delta encoding."""
from pathlib import Path
import tempfile
import threading
import unittest

import numpy as np
from PIL import Image, ImageDraw, GifImagePlugin

from mediaworker.gif_media import GifInfo, GifWriter


def full_reference(path, frames, delays, loop):
    """Simple full-canvas reference, independent of delta/lookahead decisions."""
    with path.open("wb") as output:
        for index, rgba in enumerate(frames):
            frame = rgba.convert("RGB").quantize(colors=255, dither=Image.Dither.NONE)
            palette = frame.getpalette()
            frame.putpalette(palette[:765] + [0] * (768 - len(palette[:765])))
            mask = rgba.getchannel("A").point([255] * 128 + [0] * 128)
            frame.paste(255, mask=mask)
            frame.info.clear()
            if index == 0:
                options = {"background":255,"transparency":255,"optimize":False}
                if loop is not None:
                    options["loop"] = loop
                output.write(b"".join(GifImagePlugin.getheader(frame,info=options)[0]))
            output.write(b"".join(GifImagePlugin.getdata(frame,duration=delays[index],disposal=2,
                                                       transparency=255,include_color_table=True)))
        output.write(b";")


class GifEncodingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def compare(self, frames, loop=None):
        delays = tuple(40 + i * 10 for i in range(len(frames)))
        reference, actual = self.root / "full.gif", self.root / "delta.gif"
        full_reference(reference, frames, delays, loop)
        writer = GifWriter(actual, GifInfo(*frames[0].size, delays, loop), 32 * 1024 * 1024, threading.Event())
        for frame, delay in zip(frames, delays):
            writer.append(frame, delay)
        writer.close(True)
        with Image.open(reference) as left, Image.open(actual) as right:
            self.assertEqual(left.size, right.size)
            self.assertEqual(left.n_frames, right.n_frames)
            self.assertEqual(left.n_frames, len(frames))
            self.assertEqual(left.info.get("loop"), right.info.get("loop"))
            for index in range(len(frames)):
                left.seek(index); right.seek(index)
                self.assertEqual(left.info.get("duration"), right.info.get("duration"))
                self.assertEqual(left.convert("RGBA").tobytes(), right.convert("RGBA").tobytes(), index)
            if frames[0].getchannel("A").getextrema()[0] < 128:
                self.assertEqual(right.disposal_method, 2, "last frame must clear before transparent loop start")
        self.assertLessEqual(actual.stat().st_size, reference.stat().st_size)
        return reference.stat().st_size, actual.stat().st_size

    def test_sparse_continuously_changing_opaque_frames(self):
        frames = []
        for index in range(15):
            image = Image.new("RGBA", (96,64), (30,60,90,255))
            ImageDraw.Draw(image).rectangle((index*4,10,index*4+10,25),fill=(200,20,80,255))
            frames.append(image)
        before, after = self.compare(frames, 3)
        self.assertLess(after, before)

    def test_opaque_transparent_transitions_and_loop_boundary(self):
        frames = []
        for index in range(8):
            image = Image.new("RGBA", (96,64), (10,40,90,255) if index % 3 == 2 else (0,0,0,0))
            ImageDraw.Draw(image).rectangle((index*5,10,index*5+10,25),fill=(200,20,80,255))
            frames.append(image)
        for loop in (None,0,2):
            with self.subTest(loop=loop):
                self.compare(frames,loop)

    def test_local_source_disposal_frames_are_composited_correctly(self):
        frames = []
        for index in range(4):
            image = Image.new("RGBA",(64,48))
            ImageDraw.Draw(image).ellipse((index*8,10,index*8+14,28),fill=(20,150,60,255))
            frames.append(image)
        source = self.root / "source.gif"
        frames[0].save(source,save_all=True,append_images=frames[1:],duration=[40,50,60,70],
                       disposal=[1,3,2,1],loop=0,optimize=True)
        with Image.open(source) as image:
            composited = []
            for index in range(image.n_frames):
                image.seek(index);composited.append(image.convert("RGBA"))
        self.compare(composited,0)

    def test_each_frame_has_a_different_palette(self):
        frames = []
        for index in range(5):
            image = Image.fromarray(np.indices((48,64)).sum(axis=0).astype(np.uint8) % 4).convert("P")
            palette = [((color*47 + index*31) % 256) for color in range(768)]
            image.putpalette(palette)
            frames.append(image.convert("RGBA"))
        self.compare(frames,2)

    def test_repeated_frames_keep_individual_delays(self):
        image = Image.new("RGBA",(64,48),"orange")
        before, after = self.compare([image.copy() for _ in range(12)],None)
        self.assertLess(after,before)

    def test_high_motion_noise_preserves_full_quantized_pixels(self):
        rng = np.random.default_rng(1927)
        frames = [Image.fromarray(rng.integers(0,256,(48,64,3),dtype=np.uint8)).convert("RGBA") for _ in range(5)]
        self.compare(frames,0)

    def test_cancel_with_pending_frame_removes_result(self):
        path = self.root / "pending.gif"
        cancel = threading.Event()
        writer = GifWriter(path,GifInfo(8,8,(40,50),0),10000,cancel)
        writer.append(Image.new("RGBA",(8,8),"red"),40)
        cancel.set()
        with self.assertRaisesRegex(RuntimeError,"cancelled"):
            writer.close(True)
        self.assertFalse(path.exists())

    def test_byte_limit_rejects_exact_trailer_overrun(self):
        frame = Image.new("RGBA",(8,8),"red")
        info = GifInfo(8,8,(40,50),None)
        valid = self.root / "valid.gif"
        writer = GifWriter(valid,info,10000,threading.Event())
        writer.append(frame,40);writer.append(frame,50);writer.close(True)
        limited = self.root / "limited.gif"
        writer = GifWriter(limited,info,valid.stat().st_size-1,threading.Event())
        writer.append(frame,40);writer.append(frame,50)
        with self.assertRaisesRegex(ValueError,"byte limit"):
            writer.close(True)
        self.assertFalse(limited.exists())
