import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediaworker.downloader.browser_direct import attach_candidates, candidate
from mediaworker.downloader.policy import DOWNLOAD_LIMITS
from mediaworker.downloader.worker import format_options, resolve_format_option
from mediaworker.engine.base import JobContext
from mediaworker.engine.download_engine import DownloadEngine

NOW = 1700000000


def metadata(ext="mp4"):
    return {"_type": "video", "id": "generated", "title": "Generated fixture", "duration": 597,
            "formats": [{"format_id": "muxed", "ext": ext, "protocol": "https",
                         "url": "https://media.example.invalid/file." + ext,
                         "width": 3840, "height": 2160,
                         "vcodec": "avc1.640028" if ext == "mp4" else "vp9",
                         "acodec": "mp4a.40.2" if ext == "mp4" else "opus"}]}


def selected(info):
    return next(value for value in format_options(info).values() if not value["audio"])


class BrowserDirectTests(unittest.TestCase):
    def test_private_three_fields_long_large_source_and_default_headers_no_network(self):
        for ext in ("mp4", "webm"):
            with self.subTest(ext=ext):
                info = metadata(ext)
                info["http_headers"] = {"User-Agent": "generated standard browser hint", "Accept": "*/*",
                                        "Accept-Language": "en", "Sec-Fetch-Mode": "navigate"}
                options = format_options(info)
                before = copy.deepcopy([row["public"] for row in options.values()])
                with patch("socket.getaddrinfo", side_effect=AssertionError("no DNS in projection")) as dns:
                    attach_candidates(info, options, now=NOW)
                dns.assert_not_called()
                option = next(row for row in options.values() if not row["audio"])
                self.assertEqual(option["public"]["width"], 1280)
                self.assertEqual(option["public"]["height"], 720)
                self.assertEqual(option["browserDirect"], {"url": info["formats"][0]["url"],
                                                          "expiresAt": "2023-11-14T22:14:20Z", "ext": ext})
                self.assertEqual(before, [row["public"] for row in options.values()])
                self.assertTrue(all("browserDirect" not in row["public"] for row in options.values()))
                self.assertTrue(all("browserDirect" not in row for row in options.values() if row["audio"]))
                self.assertNotIn("headers", option["browserDirect"])

    def test_expiry_is_local_ttl_only_and_source_hint_can_only_shorten(self):
        info = metadata()
        option = selected(info)
        for query, expected in (("", NOW + 60), ("?expire=" + str(NOW + 600), NOW + 60),
                                ("?Expires=" + str(NOW + 20), NOW + 20),
                                ("?expires=" + str(NOW + 45) + "&expire=" + str(NOW + 25), NOW + 25)):
            with self.subTest(query=query):
                info["formats"][0]["url"] = "https://media.example.invalid/file.mp4" + query
                value = candidate(info, option, now=NOW)
                stamp = datetime.fromisoformat(value["expiresAt"].replace("Z", "+00:00"))
                self.assertEqual(int(stamp.timestamp()), expected)
                self.assertEqual(stamp.tzinfo, timezone.utc)
        for value in ("", "expired", str(NOW), str(NOW - 1), "9999999999999999"):
            info["formats"][0]["url"] = "https://media.example.invalid/file.mp4?expire=" + value
            self.assertIsNone(candidate(info, option, now=NOW))

    def test_credentials_custom_or_malformed_headers_preserve_worker_option(self):
        for location in ("top", "format"):
            for headers in ({"Cookie": "fixture"}, {"Authorization": "fixture"}, {"Proxy-Authorization": "fixture"},
                            {"Referer": "https://example.invalid/"}, {"Origin": "https://example.invalid"},
                            {"X-Required": "fixture"}, {"Range": "bytes=0-1"}, {"Sec-Fetch-Mode": "cors"},
                            {"User-Agent": "bad\r\nheader"}, {"Accept": []}, [], "", 0):
                with self.subTest(location=location, header_type=type(headers).__name__):
                    info = metadata()
                    destination = info if location == "top" else info["formats"][0]
                    destination["http_headers"] = headers
                    options = format_options(info)
                    expected = copy.deepcopy(options)
                    attach_candidates(info, options, now=NOW)
                    self.assertEqual(options, expected)
            for name in ("cookies", "cookiefile", "cookiesfrombrowser", "cookies_from_browser"):
                info = metadata()
                (info if location == "top" else info["formats"][0])[name] = "generated credential marker"
                self.assertIsNone(candidate(info, selected(info), now=NOW))

    def test_https_lexical_admission_rejects_ip_credentials_ports_controls_and_manifests(self):
        urls = ["http://media.example.invalid/v.mp4", "file:///generated.mp4", "https://127.0.0.1/v.mp4",
                "https://8.8.8.8/v.mp4", "https://[::1]/v.mp4", "https://127.0.0.01/v.mp4",
                "https://0x7f.0.0.1/v.mp4", "https://u:p@media.example.invalid/v.mp4", "https://@media.example.invalid/v.mp4",
                "https://media.example.invalid:80/v.mp4", "https://media.example.invalid:444/v.mp4",
                "https://media.example.invalid/v.mp4#fragment", "https://media.example.invalid/v.mp4\n",
                "https://media.example.invalid\\other/v.mp4", "https://media.example.invalid/v.m3u8",
                "https://media.example.invalid/v.mpd", "https://media.example.invalid/" + "x" * 8192]
        info = metadata()
        option = selected(info)
        for url in urls:
            with self.subTest(case=urls.index(url)):
                info["formats"][0]["url"] = url
                self.assertIsNone(candidate(info, option, now=NOW))
        info["formats"][0]["url"] = "https://media.example.invalid:443/v.mp4?sig=generated"
        self.assertIsNotNone(candidate(info, option, now=NOW))
        for key in ("X-Amz-SignedHeaders", "X-Goog-SignedHeaders"):
            for value in ("host%3Buser-agent", "host%3Bauthorization", "host%3Bx-required", ""):
                info["formats"][0]["url"] = "https://media.example.invalid/v.mp4?" + key + "=" + value
                self.assertIsNone(candidate(info, option, now=NOW))
            info["formats"][0]["url"] = "https://media.example.invalid/v.mp4?" + key + "=host"
            self.assertIsNotNone(candidate(info, option, now=NOW))

    def test_non_progressive_split_silent_unknown_codec_and_drm_do_not_get_hint(self):
        for field, value in (("protocol", "m3u8_native"), ("protocol", "http_dash_segments"),
                             ("protocol", "file"), ("fragments", []), ("fragment_base_url", "https://example.invalid/"),
                             ("manifest_url", "https://example.invalid/v.m3u8"), ("has_drm", True),
                             ("acodec", "none"), ("vcodec", None), ("vcodec", "unknown"), ("acodec", "unknown")):
            with self.subTest(field=field, value_type=type(value).__name__):
                info = metadata()
                option = selected(info)
                info["formats"][0][field] = value
                self.assertIsNone(candidate(info, option, now=NOW))
        info = metadata()
        option = selected(info)
        option["components"].append("audio")
        option["component_video"].append(False)
        self.assertIsNone(candidate(info, option, now=NOW))
        audio = next(row for row in format_options(metadata()).values() if row["audio"])
        self.assertIsNone(candidate(metadata(), audio, now=NOW))

    def test_final_video_and_selected_identity_are_required_unknown_shape_is_omitted(self):
        for field, value in (("_type", "playlist"), ("_type", "url"), ("_type", "url_transparent"),
                             ("entries", []), ("is_live", True), ("live_status", "is_upcoming"),
                             ("has_drm", []), ("is_live", 0), ("live_status", {}),
                             ("duration", float("nan")), ("duration", True), ("duration", 1 << 10000)):
            info = metadata()
            option = selected(info)
            info[field] = value
            self.assertIsNone(candidate(info, option, now=NOW))
        for value in (None, [], "bad"):
            self.assertIsNone(candidate(value, {}, now=NOW))
        info = metadata()
        option = selected(info)
        info["formats"].append(copy.deepcopy(info["formats"][0]))
        self.assertIsNone(candidate(info, option, now=NOW))
        for changed in ({"components": ["changed"]}, {"components": []}, {"component_video": [1]},
                        {"public": None}, {"public": {"ext": []}}):
            option = selected(metadata())
            option.update(changed)
            self.assertIsNone(candidate(metadata(), option, now=NOW))

    def test_media_resolve_discards_hint_and_keeps_fresh_format_binding(self):
        info = metadata()
        option = selected(info)
        option["browserDirect"] = {"url": "https://127.0.0.1/ignored", "expiresAt": "bad", "ext": "mp4"}
        resolved = resolve_format_option(info, option)
        self.assertEqual(resolved["components"], ["muxed"])
        self.assertNotIn("browserDirect", resolved)
        options = {"generated": option}
        info["formats"][0]["http_headers"] = {"Cookie": "fixture"}
        attach_candidates(info, options, now=NOW)
        self.assertNotIn("browserDirect", option)

    def test_actual_info_engine_only_private_option_gets_hint_without_proxy_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            context = JobContext("generated-info", "download",
                                 {"operation": "info", "url": "https://www.youtube.com/watch?v=generated",
                                  "limits": dict(DOWNLOAD_LIMITS)}, {}, Path(directory))
            with patch("mediaworker.engine.download_engine.EgressProxy") as proxy, \
                 patch("mediaworker.engine.download_engine.ProcessRunner") as runner, \
                 patch("mediaworker.downloader.browser_direct.time.time", return_value=NOW), \
                 self.assertLogs("mediaworker.engine.download_engine", level="INFO") as logs:
                runner.return_value.inspect.return_value = metadata()
                engine = DownloadEngine(None)
                result = engine.run(context)
            data = json.loads(result.output_path.read_text())
            option = next(row for row in data["options"].values() if not row["audio"])
            self.assertEqual(set(option["browserDirect"]), {"url", "expiresAt", "ext"})
            self.assertEqual(data["duration"], 597)
            self.assertTrue(all("browserDirect" not in row for row in data["formats"]))
            self.assertNotIn("media.example.invalid", "\n".join(logs.output))
            runner.return_value.inspect.assert_called_once()
            runner.return_value.download.assert_not_called()
            proxy.return_value.close.assert_called_once()
            self.assertEqual(result.content_type, "application/json")
            self.assertEqual(engine.result_byte_limit(context.params, 1), 512 * 1024)

    def test_unencodable_optional_url_does_not_break_actual_info_json(self):
        info = metadata()
        info["formats"][0]["url"] = "https://media.example.invalid/" + chr(0xD800) + ".mp4"
        option = selected(info)
        self.assertIsNone(candidate(info, option, now=NOW))
        with tempfile.TemporaryDirectory() as directory:
            context = JobContext("generated-info", "download",
                                 {"operation": "info", "url": "https://www.youtube.com/watch?v=generated",
                                  "limits": dict(DOWNLOAD_LIMITS)}, {}, Path(directory))
            with patch("mediaworker.engine.download_engine.EgressProxy") as proxy, \
                 patch("mediaworker.engine.download_engine.ProcessRunner") as runner, \
                 patch("mediaworker.downloader.browser_direct.time.time", return_value=NOW):
                runner.return_value.inspect.return_value = info
                result = DownloadEngine(None).run(context)
            data = json.loads(result.output_path.read_text(encoding="utf-8"))
            self.assertEqual(data["duration"], 597)
            self.assertTrue(data["formats"])
            self.assertTrue(all("browserDirect" not in row for row in data["options"].values()))
            self.assertEqual(next(row for row in data["options"].values() if not row["audio"])["components"], ["muxed"])
            proxy.return_value.close.assert_called_once()

    def test_signed_required_headers_preserve_actual_info_and_worker_option(self):
        for key in ("X-Amz-SignedHeaders", "X-Goog-SignedHeaders"):
            for header in ("user-agent", "range", "authorization"):
                with self.subTest(key=key, header=header), tempfile.TemporaryDirectory() as directory:
                    info = metadata()
                    info["formats"][0]["url"] += "?" + key + "=host%3B" + header
                    self.assertIsNone(candidate(info, selected(info), now=NOW))
                    context = JobContext("generated-info", "download",
                                         {"operation": "info", "url": "https://www.youtube.com/watch?v=generated",
                                          "limits": dict(DOWNLOAD_LIMITS)}, {}, Path(directory))
                    with patch("mediaworker.engine.download_engine.EgressProxy") as proxy, \
                         patch("mediaworker.engine.download_engine.ProcessRunner") as runner, \
                         patch("mediaworker.downloader.browser_direct.time.time", return_value=NOW):
                        runner.return_value.inspect.return_value = info
                        result = DownloadEngine(None).run(context)
                    data = json.loads(result.output_path.read_text(encoding="utf-8"))
                    private = next(row for row in data["options"].values() if not row["audio"])
                    self.assertEqual(private["components"], ["muxed"])
                    self.assertNotIn("browserDirect", private)
                    self.assertTrue(data["formats"])
                    self.assertEqual(data["duration"], 597)
                    self.assertNotIn("X-Amz-SignedHeaders", result.output_path.read_text(encoding="utf-8"))
                    self.assertNotIn("X-Goog-SignedHeaders", result.output_path.read_text(encoding="utf-8"))
                    proxy.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
