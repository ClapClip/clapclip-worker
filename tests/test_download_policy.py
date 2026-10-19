"""Offline current policy, media/container and queue/upload acceptance tests."""
import asyncio
import importlib.util
from dataclasses import replace
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from mediaworker import lifecycle, storage
from mediaworker.client import LeaseLost, WorkerClient
from mediaworker.config import Config as NodeConfig
from mediaworker.downloader.egress import EgressProxy, ProxyHandler
from mediaworker.downloader.policy import DOWNLOAD_LIMITS, resolve_job_config
from mediaworker.downloader.safety import ServiceError
from mediaworker.downloader.worker import Config, ProcessRunner, format_options
from mediaworker.engine.base import Engine, JobContext, RunResult
from mediaworker.engine.download_engine import DownloadEngine
from mediaworker.models import ClaimedJob

OBSOLETE_LIMITS = dict(DOWNLOAD_LIMITS, version=1, maxBytes=50*1024**2, maxHeight=480, maxWidth=854, maxDuration=180)
URL = 'https://www.youtube.com/watch?v=offline'
VIDEO = {'format_id': 'video', 'ext': 'mp4', 'protocol': 'https', 'width': 1920,
         'height': 1080, 'vcodec': 'h264', 'acodec': 'none', 'filesize': 80 * 1024**2}
AUDIO = {'format_id': 'audio', 'ext': 'm4a', 'protocol': 'https', 'vcodec': 'none', 'acodec': 'aac'}


def current_config(**overrides):
    return replace(resolve_job_config(dict(DOWNLOAD_LIMITS), Config()), **overrides)


class PolicyTests(unittest.TestCase):
    def test_runtime_current_requires_all_output_encoders(self):
        from mediaworker.downloader import runtime
        def probe(args, **kwargs):
            if args[-1] == '--version':
                return Mock(stdout='v24.6.0')
            if args[-1] == '-encoders':
                return Mock(stdout='\n'.join(' V..... '+c for c in ['libx264', 'libvpx-vp9', 'aac', 'libopus', 'libmp3lame']))
            return Mock(stdout='ffmpeg test')
        with patch.object(runtime.sys, 'version_info', (3, 14)), \
             patch.object(runtime.metadata, 'version', return_value='test'), \
             patch.object(runtime.shutil, 'which', side_effect=lambda v: v), \
             patch.object(runtime.subprocess, 'run', side_effect=probe) as run:
            cfg = Config(node='node')
            runtime.check_runtime(cfg)
            run.side_effect = lambda *a, **kw: Mock(stdout='v24.6.0' if a[0][-1] == '--version' else 'ffmpeg test')
            with self.assertRaisesRegex(RuntimeError, 'requires ffmpeg encoders'):
                runtime.check_runtime(cfg)

    @unittest.skipUnless(importlib.util.find_spec('yt_dlp'), 'separate download environment required')
    def test_http_metadata_input_remains_bounded_during_download_phase(self):
        import io
        from yt_dlp import YoutubeDL
        from yt_dlp.downloader.http import HttpFD
        from yt_dlp.networking.common import Response
        from yt_dlp.networking.exceptions import HTTPError, TransportError
        from mediaworker.downloader.yt_runner import install_response_limits
        def response(*_):
            return Response(io.BytesIO(b'x'*1024), URL, {})
        def media(instance, *args, **kwargs):
            return YoutubeDL.urlopen(None, URL).read()
        # The patch contexts restore yt-dlp originals after guard installation.
        with patch.object(YoutubeDL, 'urlopen', response), patch.object(HttpFD, 'real_download', media):
            install_response_limits(100)
            with self.assertRaises(TransportError):
                YoutubeDL.urlopen(None, URL).read()
            partial = YoutubeDL.urlopen(None, URL)
            self.assertEqual(len(partial.read(99)), 99)
            with self.assertRaises(TransportError):
                partial.read(2)
            self.assertEqual(len(HttpFD.real_download(None)), 1024)
            # Leaving the disk-media downloader must restore metadata limits.
            with self.assertRaises(TransportError):
                YoutubeDL.urlopen(None, URL).read()
        def denied(*_):
            raise HTTPError(Response(io.BytesIO(b'x'*1024), URL, {}, status=403))
        with patch.object(YoutubeDL, 'urlopen', denied), patch.object(HttpFD, 'real_download', media):
            install_response_limits(100)
            with self.assertRaises(HTTPError) as failure:
                YoutubeDL.urlopen(None, URL)
            self.assertEqual(failure.exception.status, 403)
            with self.assertRaises(TransportError):
                failure.exception.response.read()
            self.assertTrue(failure.exception.response.closed)

    @unittest.skipUnless(importlib.util.find_spec('yt_dlp'), 'separate download environment required')
    def test_metadata_guard_counts_decoded_gzip_and_restores_after_media_exception(self):
        import gzip
        import io
        import requests
        from urllib3.response import HTTPResponse
        from yt_dlp import YoutubeDL
        from yt_dlp.downloader.http import HttpFD
        from yt_dlp.networking._requests import RequestsResponseAdapter
        from yt_dlp.networking.exceptions import TransportError
        from mediaworker.downloader.yt_runner import install_response_limits
        def compressed(*_):
            raw = requests.Response()
            raw.status_code, raw.url = 200, URL
            raw.headers['Content-Encoding'] = 'gzip'
            raw.raw = HTTPResponse(body=io.BytesIO(gzip.compress(b'x'*1024)),
                                   headers=raw.headers, preload_content=False)
            return RequestsResponseAdapter(raw)
        def interrupted(*_):
            self.assertEqual(len(YoutubeDL.urlopen(None, URL).read()), 1024)
            raise RuntimeError('offline media interruption')
        with patch.object(YoutubeDL, 'urlopen', compressed), patch.object(HttpFD, 'real_download', interrupted):
            install_response_limits(100)
            handle = YoutubeDL.urlopen(None, URL)
            self.assertEqual(len(handle.read(60)), 60)
            with self.assertRaises(TransportError):
                handle.read(60)
            self.assertTrue(handle.closed)
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                HttpFD.real_download(None)
            with self.assertRaises(TransportError):
                YoutubeDL.urlopen(None, URL).read()

    def test_current_bounds_are_explicit_and_legacy_env_is_ignored(self):
        import os
        with patch.dict(os.environ, {"DOWNLOAD_MAX_BYTES":"not-an-integer", "DOWNLOAD_MAX_HEIGHT":"1",
                                     "DOWNLOAD_MAX_WIDTH":"99999", "DOWNLOAD_MAX_DURATION":"180"}):
            local = Config.from_env()
        self.assertFalse(hasattr(local, "max_bytes"))
        self.assertEqual((local.max_height, local.max_width, local.max_duration), (720, 1280, 300))
        effective = resolve_job_config(dict(DOWNLOAD_LIMITS), local)
        self.assertIsNot(effective, local)
        for field, value in (("maxDuration",301),("maxWidth",1281),("maxHeight",721)):
            with self.subTest(field=field), self.assertRaises(ServiceError):
                resolve_job_config(dict(DOWNLOAD_LIMITS, **{field:value}), local)
        narrow = resolve_job_config(dict(DOWNLOAD_LIMITS,maxWidth=426,maxHeight=240,maxDuration=20),local)
        self.assertEqual((narrow.max_width,narrow.max_height,narrow.max_duration),(426,240,20))
        self.assertEqual(local.max_duration,300)
        with self.assertRaises(ServiceError):
            resolve_job_config(dict(OBSOLETE_LIMITS),local)

    def test_current_invalid_policies_fail_closed(self):
        cases = [None, {}, dict(DOWNLOAD_LIMITS, version=3), dict(DOWNLOAD_LIMITS, version=True),
                 dict(DOWNLOAD_LIMITS, maxBytes=0), dict(DOWNLOAD_LIMITS, maxBytes=2**40),
                 dict(DOWNLOAD_LIMITS, maxDuration=0), dict(DOWNLOAD_LIMITS, maxHeight=True),
                 dict(DOWNLOAD_LIMITS, maxWidth=1280.0), dict(DOWNLOAD_LIMITS, maxDuration=2**53),
                 dict(DOWNLOAD_LIMITS, version=2.0), dict(DOWNLOAD_LIMITS, requireKnownHeight=1),
                 dict(DOWNLOAD_LIMITS, requireDuration=False), dict(DOWNLOAD_LIMITS, extra=1),
                 {k: v for k, v in DOWNLOAD_LIMITS.items() if k != 'version'}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ServiceError):
                resolve_job_config(value, Config())

    def test_high_source_can_scale_audio_stays_available_and_no_size_labels(self):
        info = {'formats': [dict(VIDEO), dict(AUDIO)]}
        values = list(format_options(info).values())
        video = next(option for option in values if not option['audio'])
        self.assertEqual((video['public']['width'], video['public']['height']), (1280, 720))
        self.assertEqual(video['component_video'], [True, False])
        self.assertTrue(any(option['audio'] for option in values))
        self.assertNotIn('filesize', video['public'])
        audio = list(format_options({'formats': [dict(AUDIO)]}).values())
        self.assertEqual(len(audio), 1)
        self.assertTrue(audio[0]['audio'])
        with_native = format_options({'formats': [dict(VIDEO), dict(VIDEO, format_id='native', width=1280, height=720)]},
                                     1280, 720)
        self.assertEqual(next(iter(with_native.values()))['components'], ['native'])
        # A legal 1px task bound cannot encode 4:2:0 video; do not advertise a
        # larger/upscaled result, and do not reject its independent audio track.
        tiny = list(format_options(info,1,1).values())
        self.assertTrue(tiny)
        self.assertTrue(all(option['audio'] for option in tiny))

    def test_info_is_bounded_and_long_source_is_accepted_only_in_current(self):
        engine = DownloadEngine(NodeConfig.from_env(load_env=False))
        metadata = {'duration': 800, 'formats': [dict(VIDEO), dict(AUDIO)]}
        with tempfile.TemporaryDirectory() as temporary, \
             patch('mediaworker.engine.download_engine.EgressProxy'), \
             patch('mediaworker.engine.download_engine.ProcessRunner') as factory:
            factory.return_value.inspect.return_value = metadata
            ctx = JobContext('test', 'download', {'url': URL, 'operation': 'info',
                'limits': dict(DOWNLOAD_LIMITS)}, {}, Path(temporary))
            result = engine.run(ctx)
            self.assertEqual(json.loads(result.output_path.read_text())['duration'], 800)
            self.assertEqual((result.width, result.height, result.duration), (1, 1, None))
            self.assertEqual(engine.result_byte_limit(ctx.params, 32 * 1024**2), 512 * 1024)
            self.assertIsNone(engine.result_byte_limit(dict(ctx.params, operation='download'), 32 * 1024**2))
            ctx.params['limits'] = dict(OBSOLETE_LIMITS)
            with self.assertRaisesRegex(RuntimeError, 'INVALID_LIMITS'):
                engine.run(ctx)

    def test_current_metadata_process_output_is_still_bounded(self):
        import sys
        proxy = Mock(port=12345, url='http://worker:offline@127.0.0.1:12345')
        with tempfile.TemporaryDirectory() as temporary:
            runner = ProcessRunner(current_config(), proxy)
            with self.assertRaises(ServiceError) as failure:
                runner.run([sys.executable, '-c', "print('x'*(9*1024**2))"], Path(temporary),
                           threading.Event(), 10, failure_phase='info')
            self.assertEqual(failure.exception.code, 'MEDIA_UNAVAILABLE')

    def test_current_child_marks_policy_without_leaking_credentials(self):
        import sys
        proxy = Mock(port=12345, url='http://worker:offline@127.0.0.1:12345')
        with tempfile.TemporaryDirectory() as temporary, patch.dict('os.environ', {'AWS_SECRET_ACCESS_KEY': 'offline-secret'}):
            runner = ProcessRunner(current_config(), proxy)
            raw = runner.run([sys.executable, '-c',
                "import os; print(os.environ['CLAPCLIP_DOWNLOAD_POLICY_VERSION']); print(os.environ.get('AWS_SECRET_ACCESS_KEY'))"],
                Path(temporary), threading.Event(), 5)
            self.assertEqual(raw.splitlines(), ['2', 'None'])

    def test_current_child_does_not_inherit_an_obsolete_marker(self):
        import sys
        proxy = Mock(port=12345, url='http://worker:offline@127.0.0.1:12345')
        with tempfile.TemporaryDirectory() as temporary, patch.dict('os.environ', {'CLAPCLIP_DOWNLOAD_POLICY_VERSION': '1'}):
            runner = ProcessRunner(Config(), proxy)
            raw = runner.run([sys.executable, '-c', "import os; print(os.environ['CLAPCLIP_DOWNLOAD_POLICY_VERSION'])"],
                             Path(temporary), threading.Event(), 5)
            self.assertEqual(raw.strip(), '2')

    def test_script_entry_rejects_missing_or_obsolete_marker(self):
        import os
        import sys
        from mediaworker.downloader import yt_runner
        script = r'''
import io, os, runpy, socket, subprocess, sys, types
yt = types.ModuleType('yt_dlp')
class Response(io.BytesIO): pass
class Download:
 def urlopen(self, *args, **kwargs): return Response(b'x'*(9*1024**2))
class Http:
 def real_download(self, *args, **kwargs): return Download().urlopen().read()
class TransportError(Exception): pass
class HTTPError(Exception): pass
yt.YoutubeDL = Download
http = types.ModuleType('yt_dlp.downloader.http'); http.HttpFD = Http
errors = types.ModuleType('yt_dlp.networking.exceptions')
errors.TransportError, errors.HTTPError = TransportError, HTTPError
sys.modules.update({'yt_dlp': yt, 'yt_dlp.downloader.http': http,
                    'yt_dlp.networking.exceptions': errors})
def main():
 try:
  Download().urlopen().read(); print('legacy-metadata')
 except TransportError: print('bounded-metadata')
 assert len(Http().real_download()) == 9*1024**2
 for action in (lambda: socket.socket().connect(('8.8.8.8',443)),
                lambda: subprocess.Popen(['unapproved-tool'])):
  try: action()
  except OSError: print('blocked')
  else: raise AssertionError('network/process guard removed')
yt.main=main
runpy.run_path(sys.argv[1], run_name='__main__')
'''
        for marker in (None, '1', '2'):
            env = {key: value for key, value in os.environ.items() if key.upper() in
                   {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PYTHONUTF8'}}
            env.update(CLAPCLIP_PROXY_PORT='65534', CLAPCLIP_NODE='')
            if marker is not None:
                env['CLAPCLIP_DOWNLOAD_POLICY_VERSION'] = marker
            result = subprocess.run([sys.executable, '-c', script, str(Path(yt_runner.__file__).resolve())],
                                    capture_output=True, text=True, env=env, timeout=10)
            if marker != '2':
                self.assertNotEqual(result.returncode,0)
                self.assertIn('Current download policy marker required',result.stderr)
            else:
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stdout.splitlines(),['bounded-metadata','blocked','blocked'])

    def test_current_info_json_result_has_a_fixed_limit(self):
        engine = DownloadEngine(NodeConfig.from_env(load_env=False))
        with tempfile.TemporaryDirectory() as temporary, \
             patch('mediaworker.engine.download_engine.EgressProxy'), \
             patch('mediaworker.engine.download_engine.ProcessRunner') as runner, \
             patch('mediaworker.engine.download_engine.format_options', return_value={'f': {'public': 'x'*(513*1024)}}):
            runner.return_value.inspect.return_value = {'duration': 301}
            ctx = JobContext('test', 'download', {'url': URL, 'operation': 'info',
                'limits': dict(DOWNLOAD_LIMITS)}, {}, Path(temporary))
            with self.assertRaisesRegex(RuntimeError, 'MEDIA_UNAVAILABLE'):
                engine.run(ctx)

    def test_proxy_inspection_starts_bounded_then_media_is_unbounded(self):
        proxy = EgressProxy(1)
        try:
            self.assertIsInstance(proxy.server.tunnel_bytes, int)
            proxy.allow_media()
            self.assertIsNone(proxy.server.tunnel_bytes)
        finally:
            proxy.close()

    def test_unbounded_relay_transfers_more_than_200mib(self):
        # Exercise the real relay loop with a finite generated socket stream.
        remote, local = Mock(), Mock()
        chunks = iter([b'x' * 65536] * 3400 + [b''])
        remote.recv.side_effect = lambda _: next(chunks)
        handler = object.__new__(ProxyHandler)
        handler.connection = local
        handler.server = Mock(tunnel_seconds=10, tunnel_bytes=None)
        with patch('mediaworker.downloader.egress.select.select', return_value=([remote], [], [])):
            handler._relay(remote)
        self.assertEqual(local.sendall.call_count, 3400)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe required')
class LocalMediaCurrentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='clapclip-current-fixtures-')
        cls.root = Path(cls.temporary.name)
        def generate(name, args):
            subprocess.run([shutil.which('ffmpeg'), '-nostdin', '-v', 'error', '-y', *args,
                            str(cls.root / name)], check=True, capture_output=True, timeout=45)
        generate('short.mp4', ['-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=10:d=1',
                               '-f', 'lavfi', '-i', 'sine=duration=1', '-c:v', 'libx264', '-c:a', 'aac'])
        # 301 actual seconds at 1fps: cheap to generate but a real long container.
        generate('long.mp4', ['-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=1:d=301',
                              '-f', 'lavfi', '-i', 'sine=duration=301', '-c:v', 'libx264', '-c:a', 'aac'])
        generate('high.mp4', ['-f', 'lavfi', '-i', 'testsrc2=s=1920x1080:r=1:d=1', '-an', '-c:v', 'libx264'])
        generate('audio.m4a', ['-f', 'lavfi', '-i', 'sine=duration=301', '-c:a', 'aac'])
        generate('rotated.mp4', ['-display_rotation', '90', '-i', str(cls.root/'high.mp4'), '-c', 'copy'])
        generate('sar.mp4', ['-f', 'lavfi', '-i', 'color=s=320x240:r=1:d=1', '-vf', 'setsar=2', '-c:v', 'libx264'])
        shutil.copyfile(cls.root/'short.mp4', cls.root/'large.mp4')
        cls.pad(cls.root/'large.mp4')

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @staticmethod
    def pad(path):
        # A real valid MP4 free atom makes the complete media container >200MiB.
        # This tests file limits, not a claim about compressed payload bitrate.
        with path.open('ab') as output:
            size = 205 * 1024**2
            output.write(struct.pack('>I4s', size, b'free'))
            for _ in range(205):
                output.write(b'\0' * (1024**2 - (8 if _ == 0 else 0)))

    def run_media(self, source, *, audio=False, ext='mp4', padded_output=False, config=None, split=False):
        fixtures, test = self.root, self
        cfg = config or current_config()
        class Proxy:
            port, url = 12345, 'http://worker:local@127.0.0.1:12345'
            def allow_media(self):
                pass
        class FixtureRunner(ProcessRunner):
            selectors = []
            def run(self, args, directory, cancel, timeout, on_line=None, output_limit=8*1024**2, failure_phase='process'):
                if '--output' in args:
                    self.selectors.append(args[args.index('--format') + 1])
                    selected = args[args.index('--format')+1]
                    shutil.copyfile(fixtures/('audio.m4a' if split and selected.startswith('audio[') else source),
                                    Path(args[args.index('--output') + 1]))
                    return ''
                value = super().run(args, directory, cancel, timeout, on_line, output_limit, failure_phase)
                if padded_output and Path(args[0]).name.startswith('ffmpeg'):
                    test.pad(Path(args[-1]))
                return value
        runner = FixtureRunner(cfg, Proxy())
        info = {'duration': 301 if 'long' in source or audio or split else 1, 'formats': [
            dict(VIDEO, ext=ext, acodec='none' if split else ('aac' if not audio else 'none')),
            dict(AUDIO)]}
        if audio:
            info['formats'] = [dict(AUDIO)]
        runner.inspect = lambda *_: info
        selected = next(o for o in format_options(info, cfg.max_width, cfg.max_height).values() if o['audio'] == audio)
        result = runner.download({'cancel': threading.Event()}, {'url': URL}, selected,
                                 self.workdir, lambda *_: None)
        probe = subprocess.run([cfg.ffprobe, '-v', 'error', '-show_entries',
            'format=duration:stream=codec_type,width,height:stream_side_data=rotation', '-of', 'json', str(result)],
            check=True, capture_output=True)
        actual = json.loads(probe.stdout)
        self.assertGreater(float(actual['format']['duration']), 0)
        self.assertLessEqual(float(actual['format']['duration']), cfg.max_duration)
        self.assertEqual(runner.output_details[2], float(actual['format']['duration']))
        return runner, result, actual

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='clapclip-current-output-')
        self.addCleanup(temporary.cleanup)
        self.workdir = Path(temporary.name)

    def test_short_mp4_and_long_mp4_actual_cut(self):
        for name in ('short.mp4', 'long.mp4'):
            with self.subTest(name=name):
                runner, _, data = self.run_media(name)
                self.assertEqual(runner.output_details[:2], (160, 90))
                if name.startswith('long'):
                    self.assertGreater(float(data['format']['duration']), 299)

    def test_current_task_can_require_tighter_bounds_for_video_and_audio(self):
        config = resolve_job_config(dict(DOWNLOAD_LIMITS,maxWidth=426,maxHeight=240,maxDuration=20), Config())
        for name, audio in (('high.mp4',False),('long.mp4',False),('audio.m4a',True)):
            with self.subTest(source=name):
                runner, _, _ = self.run_media(name,audio=audio,config=config)
                if name.startswith('high'):
                    self.assertEqual(runner.output_details[:2],(426,240))
                self.assertLessEqual(runner.output_details[2],20)

    def test_long_mp3_actual_cut_without_video_dimensions(self):
        runner, _, data = self.run_media('audio.m4a', audio=True)
        self.assertEqual(runner.output_details[:2], (0, 0))
        self.assertEqual([s['codec_type'] for s in data['streams']], ['audio'])
        self.assertNotIn('height<=', runner.selectors[0])
        self.assertNotIn('--max-filesize', runner.base_args())

    def test_high_portrait_rotation_and_anamorphic_scale(self):
        expected = {'high.mp4': (1280, 720), 'rotated.mp4': (406, 720), 'sar.mp4': (640, 240)}
        for name, shape in expected.items():
            with self.subTest(name=name):
                runner, _, _ = self.run_media(name)
                self.assertEqual(runner.output_details[:2], shape)

    def test_webm_transcodes_and_split_audio_is_kept(self):
        runner, _, data = self.run_media('long.mp4', ext='webm')
        self.assertEqual(runner.output_details[:2], (160, 90))
        self.assertTrue(any(s['codec_type'] == 'audio' for s in data['streams']))
        runner, _, data = self.run_media('high.mp4', split=True)
        self.assertEqual(runner.output_details[:2], (1280, 720))
        self.assertTrue(any(s['codec_type'] == 'audio' for s in data['streams']))
        self.assertNotIn('height<=', runner.selectors[-1])

    def test_real_containers_larger_than_200mib_are_accepted_for_source_and_result(self):
        self.assertGreater((self.root/'large.mp4').stat().st_size, 200*1024**2)
        runner, result, _ = self.run_media('large.mp4', padded_output=True)
        self.assertGreater(result.stat().st_size, 200*1024**2)
        self.assertEqual(runner.output_details[:2], (160, 90))


class QueueAndUploadCurrentTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_upload_still_rejects_mutating_or_empty_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/'generated.mp4'
            output.touch()
            with self.assertRaises(ValueError):
                await storage.upload('https://offline.test/put', output, max_bytes=None)
            output.write_bytes(b'x'*300000)
            class Mutating(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    first = True
                    async for chunk in request.stream:
                        if first:
                            with output.open('ab') as file:
                                file.write(b'changed')
                            first = False
                    return httpx.Response(200)
            real = httpx.AsyncClient
            with patch('mediaworker.storage.httpx.AsyncClient', side_effect=lambda **kwargs: real(transport=Mutating(), **kwargs)):
                with self.assertRaisesRegex(ValueError, 'changed during upload'):
                    await storage.upload('https://offline.test/put', output, max_bytes=None)

    async def test_current_lease_loss_and_cancel_prevent_upload_and_cleanup_after_writer(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), tempfile.TemporaryDirectory() as temporary:
                cfg = NodeConfig.from_env(load_env=False); cfg.work_dir = Path(temporary)
                started = threading.Event(); finished = threading.Event()
                class Fixture(Engine):
                    def run(self, ctx):
                        path = ctx.workdir/'result.mp4'; path.write_bytes(b'begin')
                        started.set()
                        if cancelled:
                            if not ctx.cancel.wait(5):
                                raise TimeoutError('offline cancellation not delivered')
                            path.write_bytes(b'end')  # must precede cleanup
                        finished.set()
                        return RunResult(path, 640, 360, 'video/mp4', duration=1)
                    def result_byte_limit(self, params, default):
                        return None
                job = ClaimedJob(id='a86eeb49-a664-4249-bfb7-20c6001be743',
                    lease_token='d8c5b4a4-feb2-490a-a98d-f3965c91a47a', attempt=1, task_type='download',
                    params={'operation': 'download', 'limits': dict(DOWNLOAD_LIMITS)}, inputs={},
                    result={'put_url': 'https://offline.test/put', 'key': 'key'})
                client = Mock(heartbeat=AsyncMock(side_effect=LeaseLost('stale')),
                              complete=AsyncMock(), fail=AsyncMock())
                with patch.object(storage, 'upload', new_callable=AsyncMock) as upload:
                    task = asyncio.create_task(lifecycle._process_job(cfg, client, {'download': Fixture()}, job))
                    if cancelled:
                        self.assertTrue(await asyncio.to_thread(started.wait, 3))
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        await task
                self.assertTrue(finished.is_set())
                upload.assert_not_awaited(); client.complete.assert_not_awaited()
                self.assertFalse((cfg.work_dir/job.id).exists())

    async def test_negotiation_and_only_download_complete_gains_duration(self):
        cfg = NodeConfig.from_env(load_env=False)
        cfg.api_base_url, cfg.node_token = 'https://offline.test', 'offline'
        bodies = []
        def respond(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={})
        async with WorkerClient(cfg) as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url=cfg.api_base_url, transport=httpx.MockTransport(respond))
            await client.claim(['download'], [])
            self.assertEqual(bodies[-1]['downloadPolicyVersions'], [2])
            await client.claim(['faceswap'], [])
            self.assertNotIn('downloadPolicyVersions', bodies[-1])
            client._job_routes['download'] = cfg.download_api_prefix
            await client.complete('download', 'lease', 'key', 0, 0, duration=299.94)
            self.assertEqual((bodies[-1]['w'], bodies[-1]['h'], bodies[-1]['duration']), (0, 0, 299.94))
            await client.complete('ai', 'lease', 'key', 4, 3, duration=1)
            self.assertNotIn('duration', bodies[-1])

    async def test_large_upload_streams_without_fixed_limit_and_ai_still_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'generated.mp4'
            with path.open('wb') as output:
                for _ in range(205):
                    output.write(b'x'*1024**2)
            received = [0, 0]
            class Transport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    self.asserted_length = int(request.headers['Content-Length'])
                    async for chunk in request.stream:
                        received[0] += len(chunk)
                        received[1] = max(received[1], len(chunk))
                    return httpx.Response(200)
            real = httpx.AsyncClient
            with patch('mediaworker.storage.httpx.AsyncClient', side_effect=lambda **kwargs: real(transport=Transport(), **kwargs)):
                await storage.upload('https://offline.test/upload', path, 'video/mp4', None)
                with self.assertRaises(ValueError):
                    await storage.upload('https://offline.test/upload', path)
            self.assertEqual(received[0], path.stat().st_size)
            self.assertLessEqual(received[1], 256*1024)

    async def test_fenced_lifecycle_accepts_none_only_for_valid_current_media(self):
        class Fixture(Engine):
            def run(self, ctx):
                path = ctx.workdir/'result.mp4'; path.write_bytes(b'offline')
                return RunResult(path, 640, 360, 'video/mp4', duration=1.0)
            def result_byte_limit(self, params, default):
                return None
        for task, operation, limits, expected in [
            ('download', 'download', DOWNLOAD_LIMITS, True),
            ('download', 'info', DOWNLOAD_LIMITS, False),
            ('download', 'download', OBSOLETE_LIMITS, False),
            ('faceswap', 'download', DOWNLOAD_LIMITS, False)]:
            with self.subTest(task=task, operation=operation, version=limits['version']), tempfile.TemporaryDirectory() as temporary:
                cfg = NodeConfig.from_env(load_env=False); cfg.work_dir = Path(temporary)
                job = ClaimedJob(id='a86eeb49-a664-4249-bfb7-20c6001be743', lease_token='d8c5b4a4-feb2-490a-a98d-f3965c91a47a', attempt=1, task_type=task,
                    params={'operation': operation, 'limits': dict(limits)}, inputs={},
                    result={'put_url': 'https://offline.test/put', 'key': 'key'})
                client = Mock(heartbeat=AsyncMock(), complete=AsyncMock(), fail=AsyncMock())
                with patch.object(storage, 'upload', new_callable=AsyncMock) as upload:
                    await lifecycle._process_job(cfg, client, {task: Fixture()}, job)
                self.assertEqual(upload.await_count, int(expected))
                self.assertEqual(client.complete.await_count, int(expected))
                self.assertEqual(client.fail.await_count, int(not expected))
                if expected:
                    self.assertEqual(client.complete.await_args.kwargs, {'duration': 1.0})
                else:
                    self.assertEqual(client.fail.await_args.args[2], 'INVALID_LIMITS')
                self.assertFalse((cfg.work_dir/'a86eeb49-a664-4249-bfb7-20c6001be743').exists())
