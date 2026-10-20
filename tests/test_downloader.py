import json
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from mediaworker.downloader.egress import EgressProxy
from mediaworker.downloader.safety import ServiceError, public_addresses, public_ip, validate_url
from mediaworker.downloader.worker import Config, ProcessRunner, format_options, resolve_format_option

VIDEO = "https://www.youtube.com/watch?v=publicvideo"


def source_info():
    return {"title": "A public video", "duration": 12, "formats": [
        {"format_id": "18", "ext": "mp4", "protocol": "https", "width": 640, "height": 360, "vcodec": "avc1", "acodec": "mp4a", "filesize": 100},
        {"format_id": "137", "ext": "mp4", "protocol": "https", "width": 854, "height": 480, "vcodec": "avc1", "acodec": "none", "filesize": 200},
        {"format_id": "140", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a", "abr": 128, "filesize": 50},
    ]}


class AddressTests(unittest.TestCase):
    def test_private_special_and_transition_addresses_blocked(self):
        for address in ["127.0.0.1", "0.0.0.0", "10.0.0.1", "172.16.1.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "192.0.2.1", "198.18.0.1", "224.0.0.1", "240.0.0.1", "255.255.255.255", "::1", "::", "fe80::1", "fc00::1", "ff02::1", "::ffff:127.0.0.1", "64:ff9b::a00:1", "2002:7f00:1::", "2001:db8::1", "2001::1"]:
            with self.subTest(address=address):
                self.assertFalse(public_ip(address))
        for address in ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "2001:4860:4860::8888"]:
            self.assertTrue(public_ip(address))

    def test_urls_reject_credentials_literal_hosts_and_suffix_tricks(self):
        for url in ["http://127.0.0.1/", "http://2130706433/", "http://[::1]/", "https://localhost/", "file:///etc/passwd", "https://youtube.com.evil.test/x", "https://youtube.com@127.0.0.1/", "https://youtube.com:8080/x", "https://youtube.com\\@evil.test", "https://youtube.com./x", "https://user:secret@youtube.com/x", "https://youtube.com/\n"]:
            with self.subTest(url=url), self.assertRaises(ServiceError):
                validate_url(url)
        self.assertEqual(validate_url(VIDEO)[1], "YouTube")
        self.assertEqual(validate_url("https://media.w3.org/video.mp4", ("media.w3.org",))[1], "Video")
        with self.assertRaises(ServiceError):
            validate_url("https://child.media.w3.org/video.mp4", ("media.w3.org",))

    def test_dns_all_addresses_checked_and_ports_restricted(self):
        def mixed(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with self.assertRaises(ServiceError):
            public_addresses("video.example", 443, mixed)
        with self.assertRaises(ServiceError):
            public_addresses("video.example", 22, mixed)

    def test_proxy_rejects_private_destinations_on_http_and_connect(self):
        proxy = EgressProxy(2)
        try:
            for request in ["GET http://127.0.0.1/private HTTP/1.0", "CONNECT 169.254.169.254:443 HTTP/1.0", "CONNECT [::1]:443 HTTP/1.0"]:
                with socket.create_connection(("127.0.0.1", proxy.port)) as connection:
                    connection.sendall(f"{request}\r\nProxy-Authorization: {proxy.server.authorization}\r\n\r\n".encode())
                    self.assertIn(b"403", connection.recv(4096).split(b"\r\n")[0])
            with socket.create_connection(("127.0.0.1", proxy.port)) as connection:
                connection.sendall(b"CONNECT example.com:443 HTTP/1.0\r\n\r\n")
                self.assertIn(b"407", connection.recv(4096).split(b"\r\n")[0])
        finally:
            proxy.close()


class FormatTests(unittest.TestCase):
    def test_merge_and_mp3_only_from_audio_evidence(self):
        options = list(format_options(source_info()).values())
        self.assertEqual(options[0]["components"], ["137", "140"])
        self.assertEqual(options[0]["component_video"], [True, False])
        self.assertNotIn("filesize", options[0]["public"])
        self.assertTrue(options[-1]["audio"])
        self.assertEqual(options[-1]["component_video"], [False])
        for item in source_info()["formats"]:
            item["acodec"] = "none"
        silent = source_info()
        for item in silent["formats"]:
            item["acodec"] = "none"
        silent_options = list(format_options(silent).values())
        self.assertEqual(len(silent_options), 2)
        self.assertFalse(any(option["audio"] for option in silent_options))
        self.assertTrue(all("video only" in option["public"]["label"] for option in silent_options))

    def test_drm_external_protocols_huge_and_unsafe_ids_are_excluded(self):
        info = source_info()
        info["formats"][0]["protocol"] = "rtmp"
        info["formats"][1]["has_drm"] = True
        info["formats"][2]["format_id"] = "140;exec(unsafe)"
        self.assertEqual(format_options(info), {})
        large=source_info(); large["formats"][0]["filesize"]=2**40
        self.assertTrue(format_options(large))

    def test_unknown_video_dimensions_are_excluded_larger_sources_are_supported(self):
        info = {"duration": 15, "formats": [
            {"format_id": "unknown", "ext": "mp4", "protocol": "https", "vcodec": "h264", "acodec": "none"},
            {"format_id": "wide", "ext": "mp4", "protocol": "https", "width": 1280, "height": 720, "vcodec": "h264", "acodec": "aac"},
            {"format_id": "ok", "ext": "mp4", "protocol": "https", "width": 854, "height": 480, "vcodec": "h264", "acodec": "aac"},
        ]}
        options = format_options(info)
        videos = [item for item in options.values() if not item["audio"]]
        self.assertEqual([item["components"] for item in videos], [["wide"], ["ok"]])

    def test_generic_unknown_codecs_do_not_claim_audio(self):
        info = {"extractor": "generic", "formats": [{"format_id": "mp4", "ext": "mp4", "video_ext": "mp4", "protocol": "https", "vcodec": None}]}
        options = list(format_options(info).values())
        self.assertEqual(options, [])


class SelectedFormatTests(unittest.TestCase):
    def setUp(self):
        self.info = source_info()
        options = list(format_options(self.info).values())
        self.video = next(option for option in options if option["components"] == ["137", "140"])
        self.audio = next(option for option in options if option["audio"])

    def test_same_resolution_reordering_does_not_invalidate_available_streams(self):
        duplicate = dict(self.info["formats"][1], format_id="replacement")
        refreshed = {**self.info, "formats": [duplicate, *self.info["formats"]]}
        self.assertNotIn(self.video["components"], [o["components"] for o in format_options(refreshed).values()])
        selected = resolve_format_option(refreshed, self.video)
        self.assertEqual(selected["components"], ["137", "140"])
        self.assertEqual(selected["component_video"], [True, False])

    def test_better_audio_does_not_replace_the_original_video_or_mp3_selection(self):
        refreshed = {**self.info, "formats": [*self.info["formats"],
            dict(self.info["formats"][2], format_id="better-audio", abr=192)]}
        menu = list(format_options(refreshed).values())
        for option in (self.video, self.audio):
            with self.subTest(audio=option["audio"]):
                self.assertNotIn(option["components"], [o["components"] for o in menu])
                self.assertEqual(resolve_format_option(refreshed, option)["components"], option["components"])

    def test_display_limit_does_not_invalidate_a_lower_resolution_selection(self):
        low = next(o for o in format_options(self.info).values() if o["components"] == ["18"])
        higher = [dict(self.info["formats"][1], format_id=f"extra-{i}", width=1000, height=500+i*20)
                  for i in range(8)]
        refreshed = {**self.info, "formats": [*higher, *self.info["formats"]]}
        self.assertNotIn(low["components"], [o["components"] for o in format_options(refreshed).values()])
        self.assertEqual(resolve_format_option(refreshed, low)["components"], ["18"])

    def test_revalidation_keeps_scaling_and_no_product_byte_cap(self):
        self.info["duration"] = 900
        self.info["formats"][1].update(width=3840, height=2160, filesize=2**40)
        selected = resolve_format_option(self.info, self.video)
        self.assertEqual((selected["public"]["width"], selected["public"]["height"]), (1280, 720))
        self.assertEqual(selected["components"], ["137", "140"])
        self.assertNotIn("filesize", selected["public"])

    def test_missing_or_unsafe_selected_streams_are_rejected(self):
        changes = [{"has_drm": True}, {"protocol": "rtmp"}, {"ext": "flv"},
                   {"vcodec": "none"}, {"width": None}, {"height": True}, {"width": 1},
                   {"format_id": "missing"}]
        for change in changes:
            refreshed = source_info()
            refreshed["formats"][1].update(change)
            with self.subTest(change=change), self.assertRaises(ServiceError) as failure:
                resolve_format_option(refreshed, self.video)
            self.assertEqual(failure.exception.code, "NO_FORMATS")
        for change in ({"acodec": "none"}, {"has_drm": True}, {"protocol": "rtmp"},
                       {"ext": "opus"}, {"vcodec": "h264"}):
            refreshed = source_info()
            refreshed["formats"][2].update(change)
            with self.subTest(audio_change=change), self.assertRaises(ServiceError):
                resolve_format_option(refreshed, self.video)

    def test_malformed_options_and_wrong_output_types_are_rejected(self):
        invalid = [None, {}, {**self.video, "components": "137"},
                   {**self.video, "components": ["137", "137"]},
                   {**self.video, "components": ["137;unsafe"]},
                   {**self.video, "components": ["137", "140", "18"]},
                   {**self.video, "components": ["140", "137"]},
                   {**self.video, "audio": 0}, {**self.video, "public": {"ext": []}},
                   {**self.video, "public": {"ext": "webm"}},
                   {**self.video, "public": {"ext": "mp3"}},
                   {**self.audio, "public": {"ext": "mp4"}}]
        for option in invalid:
            with self.subTest(option=option), self.assertRaises(ServiceError):
                resolve_format_option(self.info, option)


class ProcessTests(unittest.TestCase):
    def test_isolated_extractor_blocks_direct_sockets_and_external_tools(self):
        script = """
import socket, subprocess
from mediaworker.downloader.yt_runner import install_guards
install_guards(65534, '')
for attempt in (lambda: socket.socket().connect(('127.0.0.1', 80)), lambda: socket.socket().connect(('8.8.8.8', 443)), lambda: subprocess.Popen(['ffmpeg', '-version'])):
    try:
        attempt()
        raise AssertionError('Operation unexpectedly allowed')
    except OSError:
        print('blocked')
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, check=True)
        self.assertEqual(result.stdout.decode().split(), ["blocked"] * 3)

    def test_process_timeout_output_limit_and_credential_isolation(self):
        class Proxy:
            port, url = 12345, "http://worker:local@127.0.0.1:12345"
            def allow_media(self):
                pass
        runner = ProcessRunner(Config(), Proxy())
        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.dict("os.environ", {"DOWNLOAD_API_KEY": "secret-never-child", "AWS_SECRET_ACCESS_KEY": "private"}):
                result = runner.run([sys.executable, "-c", "import os; print(os.environ.get('DOWNLOAD_API_KEY'), os.environ.get('AWS_SECRET_ACCESS_KEY'))"], directory, cancel, 2)
                self.assertEqual(result.strip(), "None None")
            with self.assertRaises(ServiceError) as timeout:
                runner.run([sys.executable, "-c", "import time; time.sleep(10)"], directory, cancel, 0.1)
            self.assertEqual(timeout.exception.code, "TIMEOUT")
            with self.assertRaises(ServiceError):
                runner.run([sys.executable, "-c", "print('a'*10000)"], directory, cancel, 2, output_limit=100)

    def test_extractor_failure_logs_only_safe_rate_limit_category(self):
        class Proxy:
            port, url = 12345, "http://worker:local@127.0.0.1:12345"
            def allow_media(self):
                pass
        runner = ProcessRunner(Config(), Proxy())
        with tempfile.TemporaryDirectory() as temporary:
            command = [sys.executable, "-c", "import sys; print('https://secret.example/signed?token=private HTTP Error 429: Too Many Requests', file=sys.stderr); raise SystemExit(1)"]
            with self.assertRaises(ServiceError) as failure:
                runner.run(command, Path(temporary), threading.Event(), 2, failure_phase="info")
        self.assertEqual(failure.exception.code, "MEDIA_UNAVAILABLE")
        self.assertEqual(failure.exception.diagnostic_code, "HTTP_RATE_LIMITED")
        self.assertEqual(failure.exception.phase, "info")
        self.assertNotIn("secret.example", str(failure.exception))

    def test_extractor_failure_categories_are_stable_and_bounded(self):
        from mediaworker.downloader.worker import _extractor_failure_code
        cases = {
            "HTTP Error 503: Service Unavailable": "HTTP_TRANSIENT",
            "HTTP Error 403: Forbidden": "HTTP_FORBIDDEN",
            "HTTP/1.0 403 Destination unavailable": "EGRESS_REJECTED",
            "This video is private": "SOURCE_RESTRICTED",
            "Requested format is not available": "FORMAT_UNAVAILABLE",
            "Unsupported URL": "UNSUPPORTED_MEDIA",
            "unrecognized source response": "EXTRACTOR_FAILED",
        }
        for stderr, expected in cases.items():
            with self.subTest(stderr=stderr):
                self.assertEqual(_extractor_failure_code(stderr), expected)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
class LocalMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="clapclip-media-test-")
        cls.root = Path(cls.temporary.name)
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10:d=1", "-an", "-c:v", "libx264", str(cls.root / "video.mp4")], check=True, capture_output=True)
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", str(cls.root / "audio.m4a")], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_real_local_ffmpeg_merge_and_mp3(self):
        fixtures = self.root
        class Proxy:
            port, url = 12345, "http://worker:local@127.0.0.1:12345"
            def allow_media(self):
                pass
        class FixtureRunner(ProcessRunner):
            selectors = []
            def run(self, args, directory, cancel, timeout, on_line=None, output_limit=8 * 1024 * 1024, failure_phase="process"):
                if "--output" in args:
                    target = Path(args[args.index("--output") + 1])
                    selection = args[args.index("--format") + 1]
                    self.selectors.append(selection)
                    shutil.copyfile(fixtures / ("video.mp4" if selection.startswith("137[") else "audio.m4a"), target)
                    return ""
                return super().run(args, directory, cancel, timeout, on_line, output_limit, failure_phase)
        runner = FixtureRunner(Config(), Proxy())
        # Discovery preferences changed, but the previously selected streams
        # still exist. Exercise actual merge/MP3 using that original selection.
        refreshed = source_info()
        refreshed["formats"].insert(0, dict(refreshed["formats"][1], format_id="new-video"))
        refreshed["formats"].append(dict(refreshed["formats"][-1], format_id="new-audio", abr=192))
        runner.inspect = lambda url, directory, cancel: refreshed
        available = list(format_options(source_info(), runner.config.max_width, runner.config.max_height).values())
        merge = next(option for option in available if option["components"] == ["137", "140"])
        mp3 = next(option for option in available if option["audio"])
        for option in (merge, mp3):
            audio, ext = option["audio"], option["public"]["ext"]
            with self.subTest(ext=ext), tempfile.TemporaryDirectory() as temporary:
                result = runner.download({"cancel": threading.Event()}, {"url": VIDEO}, option, Path(temporary), lambda *args: None)
                probe = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(result)], check=True, capture_output=True)
                streams = json.loads(probe.stdout)["streams"]
                self.assertTrue(any(item["codec_type"] == "audio" for item in streams))
                self.assertEqual(any(item["codec_type"] == "video" for item in streams), not audio)
                if audio:
                    self.assertEqual(streams[0]["codec_name"], "mp3")
        video_selector, audio_selector, mp3_selector = runner.selectors
        self.assertIn("137[protocol=", video_selector)
        self.assertNotIn("height<=", video_selector)
        self.assertNotIn("height<=", audio_selector)
        self.assertNotIn("width<=", audio_selector)
        self.assertNotIn("height<=", mp3_selector)
        self.assertNotIn("width<=", mp3_selector)


if __name__ == "__main__":
    unittest.main()
