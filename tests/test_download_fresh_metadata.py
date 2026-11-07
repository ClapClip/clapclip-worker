import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

from mediaworker.downloader.fresh_metadata import build_component, encode_component, read_component, MAX_FRESH_BYTES
from mediaworker.downloader.yt_runner import download_fresh


def video(protocol="https", ext="mp4"):
    row = {"format_id":"133","url":"https://media.example.invalid/stream", "protocol":protocol,
           "ext":ext,"vcodec":"avc1","acodec":"none","width":256,"height":144}
    if protocol == "http_dash_segments":
        row.update(fragment_base_url="https://media.example.invalid/fragments/",
                   fragments=[{"path":"one","duration":5}])
    return {"id":"public-fixture","title":"fixture","duration":5,"extractor":"youtube", "extractor_key":"Youtube","formats":[row]}


class MetadataTests(unittest.TestCase):
    def test_only_necessary_component_fields_survive_credentials_and_execution_injections(self):
        info=video();info.update(additional_urls=["https://evil.invalid"],cookies="private",requested_downloads=[{}],__post_extractor="private",filepath="evil")
        info["formats"][0].update(cookies="private",filepath="evil",requested_formats=[{}],
            http_headers={"User-Agent":"fixture","Cookie":"private","Authorization":"private","Proxy-Authorization":"private","X-Forwarded-For":"private"},
            downloader_options={"http_chunk_size":10<<20,"external_downloader":"evil"})
        result=build_component(info,"133");raw=json.dumps(result)
        for secret in ("private","evil","additional_urls","requested_formats","filepath"):
            self.assertNotIn(secret,raw)
        self.assertEqual(result["formats"][0]["http_headers"],{"User-Agent":"fixture"})
        self.assertEqual(result["formats"][0]["downloader_options"],{"http_chunk_size":10<<20})

    def test_final_video_protocol_url_and_fragment_types_fail_closed(self):
        cases=[]
        for kind in ("url","url_transparent","playlist","multi_video"):
            info=video();info["_type"]=kind;cases.append(info)
        for field,value in (("protocol","file"),("url","file:///private"),("url","https://u:p@example.invalid/"),("fragments","generator repr"),("http_headers",[])):
            info=video();info["formats"][0][field]=value;cases.append(info)
        info=video();info["entries"]=[];cases.append(info)
        for info in cases:
            with self.subTest(info_type=info.get("_type")),self.assertRaises(ValueError):build_component(info,"133")

    def test_http_hls_dash_keep_needed_safe_fields_and_reject_aes_override(self):
        for protocol in ("http","https","m3u8_native","http_dash_segments"):
            with self.subTest(protocol=protocol):
                info=video(protocol);info["formats"][0].update(extra_param_to_segment_url="a=b",extra_param_to_key_url="c=d")
                result=build_component(info,"133")["formats"][0]
                self.assertEqual(result["protocol"],protocol)
                self.assertEqual(result["extra_param_to_key_url"],"c=d")
                if protocol=="http_dash_segments":self.assertEqual(result["fragments"],[{"path":"one","duration":5}])
        info=video("m3u8_native");info["formats"][0]["hls_aes"]={"key":"private"}
        with self.assertRaisesRegex(ValueError,"UNSUPPORTED_FRESH_FORMAT"):build_component(info,"133")

    def test_json_is_bounded_before_write_and_on_child_read(self):
        info=video();info["formats"][0]["url"]+="x"*MAX_FRESH_BYTES
        with self.assertRaises(ValueError):encode_component(info,"133")
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/"oversized.json";path.write_bytes(b"x"*(MAX_FRESH_BYTES+1))
            with self.assertRaisesRegex(ValueError,"FRESH_METADATA_LIMIT"):read_component(path,"133")


class ControlledApiTests(unittest.TestCase):
    def invoke(self, info, action=None, target="source-0.media"):
        import yt_dlp
        from yt_dlp.globals import plugin_dirs
        calls=[];original=yt_dlp.YoutubeDL
        class RecordingYoutubeDL(original):
            def __init__(self, options):
                if plugin_dirs.value != []:raise AssertionError("plugin discovery enabled")
                super().__init__(options)
                if self.params.get("remote_components") or self.params.get("cookiefile") or self.params.get("cookiesfrombrowser"):
                    raise AssertionError("credentials or remote components enabled")
            def process_info(self, value):
                calls.append(value["format_id"])
                if action:action(self,value)
        with tempfile.TemporaryDirectory() as temporary:
            cwd=os.getcwd();os.chdir(temporary)
            try:
                metadata=Path(temporary)/(".fresh-component-"+"a"*32+".json");metadata.write_bytes(encode_component(info,"133"))
                args=["--ignore-config","--no-plugin-dirs","--no-remote-components","--no-cookies","--no-cookies-from-browser",
                      "--no-playlist","--no-warnings","--format","133","--output",str(Path(temporary)/target),
                      "--clapclip-info-json",str(metadata),"--clapclip-component","133"]
                with patch.object(yt_dlp,"YoutubeDL",RecordingYoutubeDL),redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                    download_fresh(args)
            finally:os.chdir(cwd)
        return calls

    def test_format_drift_old_reextract_fails_new_uses_exact_current_component(self):
        import yt_dlp
        from yt_dlp.globals import plugin_dirs
        from yt_dlp.utils import ExtractorError
        plugin_dirs.value=[]
        fresh=video();drifted=copy.deepcopy(fresh);drifted["formats"][0]["format_id"]="134"
        with yt_dlp.YoutubeDL({"format":"133","quiet":True,"no_warnings":True}) as old:
            with patch.object(old,"process_info",side_effect=AssertionError("unexpected media call")):
                with redirect_stderr(io.StringIO()),self.assertRaises(ExtractorError):old.process_ie_result(drifted,download=True)
        self.assertEqual(self.invoke(fresh),["133"])

    def test_any_implicit_reextract_is_disabled_including_failure_and_additional_urls(self):
        from yt_dlp.utils import DownloadError
        info=video();info["additional_urls"]=["https://evil.invalid"]
        self.assertEqual(self.invoke(info),["133"])
        def refresh(ydl,value):ydl.extract_info("https://evil.invalid")
        with self.assertRaisesRegex(DownloadError,"CLAPCLIP_REEXTRACT_DISABLED"):self.invoke(video(),refresh)
        def nonfatal(ydl,value):ydl._download_retcode=1
        with self.assertRaisesRegex(DownloadError,"CLAPCLIP_FRESH_DOWNLOAD_FAILED"):self.invoke(video(),nonfatal)

    def test_http403_and_reextract_exception_do_not_reach_page_extraction(self):
        from yt_dlp.utils import DownloadError, ReExtractInfo
        for error in (DownloadError("HTTP Error 403"),ReExtractInfo("expired",expected=True)):
            with self.subTest(error=type(error).__name__):
                def fail(ydl,value):raise error
                with self.assertRaises(Exception):self.invoke(video(),fail)


class ParentAttemptTests(unittest.TestCase):
    def runner(self):
        from mediaworker.downloader.worker import Config, ProcessRunner
        proxy=type("Proxy",(),{"port":12345,"url":"http://worker:local@127.0.0.1:12345","allow_media":lambda _:None})()
        return ProcessRunner(Config(),proxy)

    def test_timeout_cancel_or_start_failure_removes_private_component_json(self):
        from mediaworker.downloader.safety import ServiceError
        from mediaworker.downloader.worker import format_options
        for code in ("TIMEOUT","CANCELLED","SERVICE_UNAVAILABLE"):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as temporary:
                runner=self.runner();info=video();option=next(v for v in format_options(info).values() if not v["audio"])
                runner.inspect=Mock(return_value=info);seen=[]
                def fail(args,*_,**__):
                    path=Path(args[args.index("--clapclip-info-json")+1]);self.assertTrue(path.exists());seen.append(path)
                    error=ServiceError(code,"safe",422);error.phase="download_source";raise error
                runner.run=fail
                with self.assertRaises(ServiceError):runner.download({"cancel":threading.Event()},{"url":"https://example.invalid"},option,Path(temporary),lambda *_:None)
                runner.inspect.assert_called_once()
                self.assertTrue(seen)
                self.assertFalse(any(path.exists() for path in seen))
                self.assertFalse(list(Path(temporary).glob(".fresh-component-*")))

    def test_mp4_webm_and_mp3_reuse_one_fresh_inspect_and_cleanup_every_component(self):
        from mediaworker.downloader.worker import format_options
        for ext,audio in (("mp4",False),("webm",False),("mp3",True)):
            with self.subTest(ext=ext),tempfile.TemporaryDirectory() as temporary:
                runner=self.runner();info=video(ext="webm" if ext=="webm" else "mp4")
                info["formats"].append({"format_id":"140","url":"https://media.example.invalid/audio","protocol":"https",
                    "ext":"opus" if ext=="webm" else "m4a","vcodec":"none","acodec":"opus" if ext=="webm" else "aac"})
                option=next(v for v in format_options(info).values() if v["audio"]==audio)
                runner.inspect=Mock(return_value=info);seen=[];component_ids=[]
                def run(args,*_,**__):
                    if "--clapclip-info-json" in args:
                        self.assertNotIn("--",args)
                        path=Path(args[args.index("--clapclip-info-json")+1]);seen.append(path)
                        component=args[args.index("--clapclip-component")+1];component_ids.append(component)
                        self.assertEqual(json.loads(path.read_bytes())["formats"][0]["format_id"],component)
                        Path(args[args.index("--output")+1]).write_bytes(b"fixture");return ""
                    if args[0]==runner.config.ffmpeg:
                        Path(args[-1]).write_bytes(b"fixture");return ""
                    return json.dumps({"format":{"duration":4.9},"streams":[{"codec_type":"audio"}] if audio else [{"codec_type":"video","width":256,"height":144}]})
                runner.run=run
                result=runner.download({"cancel":threading.Event()},{"url":"https://example.invalid"},option,Path(temporary),lambda *_:None)
                self.assertEqual(result.suffix,"."+ext)
                runner.inspect.assert_called_once()
                self.assertEqual(component_ids,["140"] if audio else ["133","140"])
                self.assertTrue(all(not path.exists() for path in seen))
