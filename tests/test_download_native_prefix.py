import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from mediaworker.downloader.native_prefix import UnsupportedPrefix, byte_dashy, hls_prefix
from mediaworker.downloader.worker import Config, ProcessRunner, format_options
from mediaworker.downloader.safety import ServiceError


class PrefixPlanTests(unittest.TestCase):
    def test_hls_keeps_original_key_sequence_map_ranges_and_boundary(self):
        text = ('#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:7\n'
                '#EXT-X-MAP:URI="init"\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n'
                '#EXTINF:1.5,\n#EXT-X-BYTERANGE:10@0\nsegment\n'
                '#EXTINF:1.5,\n#EXT-X-BYTERANGE:10\nsegment\n'
                '#EXTINF:1.5,\nsegment3\n#EXT-X-ENDLIST\n')
        prefix, selected, total = hls_prefix(text, 2)
        self.assertEqual((selected, total), (2, 3))
        self.assertIn('#EXT-X-MEDIA-SEQUENCE:7', prefix)
        self.assertIn('#EXT-X-KEY:METHOD=AES-128,URI="key"', prefix)
        self.assertIn('#EXT-X-MAP:URI="init"', prefix)
        self.assertIn('#EXT-X-BYTERANGE:10\nsegment', prefix)
        self.assertNotIn('segment3', prefix)
        decorated=text.replace('#EXT-X-TARGETDURATION:4', '#EXT-X-TARGETDURATION:4\n#EXT-X-ALLOW-CACHE:YES\n#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00Z\n#EXT-X-DISCONTINUITY-SEQUENCE:4')
        self.assertEqual(hls_prefix(decorated,2)[1:],(selected,total))
        self.assertEqual(hls_prefix('#EXTM3U\n#EXTINF:1E999999999,\nsegment\n#EXT-X-ENDLIST\n',2)[1],1)

    def test_hls_unknown_live_master_gap_ad_discontinuity_and_bad_duration_fallback(self):
        base = '#EXTM3U\n#EXTINF:3,\nsegment\n#EXT-X-ENDLIST\n'
        for text in (base.replace('#EXT-X-ENDLIST', ''), base.replace('#EXTINF:3,', '#EXT-X-STREAM-INF:BANDWIDTH=2'),
                     *(base.replace('#EXTINF:3,', tag+'\n#EXTINF:3,') for tag in
                       ('#EXT-X-DISCONTINUITY', '#EXT-X-GAP', '#EXT-X-PART:DURATION=1', '#ANVATO-SEGMENT-INFO:type=ad')),
                     base.replace('3,', 'NaN,'), base.replace('#EXTINF:3,\n', '')):
            with self.subTest(text=text), self.assertRaises(UnsupportedPrefix):hls_prefix(text, 2)

    def test_byte_dashy_exact_identity_contiguous_eof_and_official_one_past_tail(self):
        from yt_dlp.utils import update_url_query
        base='https://media.example/object?signature=opaque'
        row={'url':base,'filesize':100,'fragments':[
            {'url':update_url_query(base,{'range':'0-49'})},
            {'url':update_url_query(base,{'range':'50-100'})}]}
        fragments, clamped=byte_dashy(row)
        self.assertTrue(clamped)
        self.assertEqual(fragments[-1]['url'],update_url_query(base,{'range':'50-99'}))
        for patch in ({'filesize':None}, {'filesize':100.0}, {'filesize_approx':100,'filesize':None},
                      {'fragments':[row['fragments'][1]]},
                      {'fragments':[{'url':update_url_query(base,{'range':'0-99'})},{'url':update_url_query(base,{'range':'100-100'})}]},
                      {'fragments':[row['fragments'][0],row['fragments'][0]]},
                      {'fragments':[row['fragments'][0],{'url':base+'&range=50-99&range=50-99'}]},
                      {'fragments':[row['fragments'][0],{'url':'https://other.example/object?range=50-99'}]},
                      {'fragments':[row['fragments'][0],{'url':update_url_query(base,{'range':'51-99'})}]}):
            with self.subTest(patch=patch),self.assertRaises(UnsupportedPrefix):byte_dashy({**row,**patch})


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'ffmpeg/ffprobe required')
class NativeFragmentProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix='native-prefix-generated-')
        cls.folder=Path(cls.temporary.name)
        cls.ffmpeg,cls.ffprobe=shutil.which('ffmpeg'),shutil.which('ffprobe')
        subprocess.run([cls.ffmpeg,'-nostdin','-v','error','-y','-f','lavfi','-i',
            'testsrc2=s=640x360:r=30:d=12','-f','lavfi','-i','sine=duration=12:sample_rate=48000',
            '-c:v','libx264','-preset','veryfast','-crf','12','-threads','2','-g','30','-pix_fmt','yuv420p',
            '-c:a','aac','-movflags','+faststart',str(cls.folder/'source.mp4')],
            capture_output=True,check=True,timeout=30)
        subprocess.run([cls.ffmpeg,'-nostdin','-v','error','-y','-i',str(cls.folder/'source.mp4'),
            '-c','copy','-hls_time','1','-hls_list_size','0','-hls_playlist_type','vod',
            '-hls_segment_filename',str(cls.folder/'part%d.ts'),str(cls.folder/'media.m3u8')],
            capture_output=True,check=True,timeout=15)

        subprocess.run([cls.ffmpeg,'-nostdin','-v','error','-y','-i',str(cls.folder/'source.mp4'),
            '-c','copy','-hls_time','1','-hls_list_size','0','-hls_playlist_type','vod','-hls_segment_type','fmp4',
            '-hls_segment_filename',str(cls.folder/'fragment%d.m4s'),str(cls.folder/'fragmented.m3u8')],
            capture_output=True,check=True,timeout=15,cwd=cls.folder)
        subprocess.run([cls.ffmpeg,'-nostdin','-v','error','-y','-i',str(cls.folder/'source.mp4'),
            '-vn','-c','copy','-f','segment','-segment_time','1','-segment_format','adts',str(cls.folder/'audio%d.aac')],
            capture_output=True,check=True,timeout=15)

    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()

    def proof(self, protocol, *, missing=False, byterange=False, fmp4=False, raw_audio=False, recovery=False, response_range=None):
        source=(self.folder/'source.mp4').read_bytes()
        bodies={p.name:p.read_bytes() for p in self.folder.iterdir()}
        if fmp4:bodies['media.m3u8']=bodies['fragmented.m3u8']
        if raw_audio:
            lines=['#EXTM3U','#EXT-X-TARGETDURATION:2','#EXT-X-PLAYLIST-TYPE:VOD']
            for index in range(12):lines += ['#EXTINF:1,',f'audio{index}.aac']
            bodies['media.m3u8']='\n'.join(lines+['#EXT-X-ENDLIST','']).encode()
        if byterange:
            # Same object, nonzero offsets, implicit subsequent range semantics.
            parts=[bodies[f'part{i}.ts'] for i in range(12)]
            offset=len(parts[0]);joined=b''.join(parts)
            lines=['#EXTM3U','#EXT-X-TARGETDURATION:1','#EXT-X-MEDIA-SEQUENCE:0','#EXT-X-PLAYLIST-TYPE:VOD']
            position=0
            for part in parts:
                lines+=['#EXTINF:1,',f'#EXT-X-BYTERANGE:{len(part)}@{position}','joined.ts'];position+=len(part)
            lines+=['#EXT-X-ENDLIST','']
            bodies['media.m3u8']='\n'.join(lines).encode();bodies['joined.ts']=joined
        requests=[]
        dropped=[False]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def do_GET(self):
                parts=urlsplit(self.path);name=parts.path.rsplit('/',1)[-1]
                if missing and name=='part1.ts':self.send_error(404);return
                body=bodies.get(name)
                if body is None:
                    requests.append(("missing:"+re.split(r"[/\\]",name)[-1],0,0));self.send_error(404);return
                span=parse_qs(parts.query).get('range',[None])[0]
                header=self.headers.get('Range')
                match=re.fullmatch(r'bytes=(\d+)-(\d*)',header or '')
                start,end=(map(int,span.split('-')) if span else
                           (int(match[1]),int(match[2]) if match[2] else len(body)-1) if match else (0,len(body)-1))
                if recovery and match and int(match[1])>0:start,end=0,len(body)-1;match=None
                end=min(end,len(body)-1)
                requests.append((name,start,end))
                self.send_response(206 if match or span and response_range else 200)
                self.send_header('Content-Length',str(end-start+1));self.send_header('Connection','close')
                if match or span and response_range:
                    cr_start=start+1 if response_range=='wrong_start' else start
                    cr_end=end+1 if response_range=='wrong_start' else end
                    cr_total=len(body)+1 if response_range=='wrong_total' else len(body)
                    self.send_header('Content-Range',f'bytes {cr_start}-{cr_end}/{cr_total}')
                self.end_headers()
                try:
                    if recovery and name=='part0.ts' and not dropped[0]:
                        dropped[0]=True;self.wfile.write(body[start:start+32768]);self.wfile.flush();self.close_connection=True;return
                    self.wfile.write(body[start:end+1])
                except OSError:pass
        class Server(ThreadingHTTPServer):
            daemon_threads=True
            def handle_error(self,*_):pass
        server=Server(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        class Proxy:
            port=server.server_port;url=f'http://127.0.0.1:{server.server_port}'
            def allow_media(self):pass
        base='http://media.example/'+('source.mp4' if protocol=='http_dash_segments' else 'media.m3u8')
        row={'format_id':'v','url':base,'protocol':protocol,'ext':'mp4','width':640,'height':360,
             'vcodec':'none' if raw_audio else 'avc1','acodec':'aac'}
        if protocol=='http_dash_segments':
            from yt_dlp.utils import update_url_query
            row.update(filesize=len(source),fragments=[{'url':update_url_query(base,{'range':f'{start}-{min(start+65535,len(source))}'})}
                for start in range(0,len(source),65536)])
        info={'duration':12,'formats':[row]}
        class Runner(ProcessRunner):
            inspections=0
            def inspect(self,*_):self.inspections+=1;return info
            def run_section(self,*args,**kwargs):
                try:return super().run_section(*args,**kwargs)
                except ServiceError as error:
                    if missing or response_range in {"wrong_start","wrong_total"}:raise
                    raise AssertionError('generated source phase='+str(getattr(error,'phase',None))+' reason='+str(getattr(error,'diagnostic_code',None))+' requests='+repr(requests)) from None
        runner=Runner(Config(max_duration=2,job_timeout=30,python=sys.executable,
                            ffmpeg=self.ffmpeg,ffprobe=self.ffprobe,node=shutil.which('node') or ''),Proxy())
        try:
            with tempfile.TemporaryDirectory(prefix='native-prefix-attempt-') as temporary:
                work=Path(temporary)
                if missing or response_range in {"wrong_start","wrong_total"}:
                    with self.assertRaises(ServiceError):
                        runner.download({'cancel':threading.Event()},{'url':'http://media.example/page'},
                            next(o for o in format_options(info).values() if o['audio']==raw_audio),work,lambda *_:None)
                    self.assertFalse(list(work.glob('.section-*')))
                    return
                with self.assertLogs('mediaworker.downloader',level='INFO') as logs:
                    result=runner.download({'cancel':threading.Event()},{'url':'http://media.example/page'},
                        next(o for o in format_options(info).values() if o['audio']==raw_audio),work,lambda *_:None)
                metrics=[json.loads(line.split('download section ',1)[1]) for line in logs.output if 'download section {' in line]
                if raw_audio:
                    self.assertEqual(metrics,[])
                    self.assertTrue(any('mode=full_native' in line for line in logs.output))
                else:self.assertEqual(len(metrics),1,'must hit native prefix, not full-native fallback')
                if not raw_audio:self.assertEqual(metrics[0]['section_mode'],'native_dash_byte_pipe' if protocol=='http_dash_segments' else 'native_hls_prefix')
                if not raw_audio:self.assertTrue(metrics[0]['format_preserved'])
                probe=json.loads(subprocess.run([self.ffprobe,'-v','error','-show_entries',
                    'format=duration:stream=codec_type,duration','-of','json',str(result)],capture_output=True,check=True).stdout)
                self.assertLessEqual(float(probe['format']['duration']),2)
                self.assertEqual({s['codec_type'] for s in probe['streams']},{'audio'} if raw_audio else {'video','audio'})
                subprocess.run([self.ffmpeg,'-nostdin','-v','error','-xerror','-i',str(result),'-f','null','-'],
                    capture_output=True,check=True,timeout=10)
                self.assertEqual(runner.inspections,1)
                self.assertFalse(list(work.glob('.section-*')) or list(work.glob('.fresh-component-*')))
                if protocol=='m3u8_native' and not raw_audio:
                    self.assertFalse(any(name=='part2.ts' for name,_,_ in requests))
                    self.assertFalse(metrics[0]['source_bytes_known'])
                if recovery:self.assertTrue(dropped[0])
                print('clapclip_native_prefix_proof:'+json.dumps({'protocol':protocol,'byterange':byterange,
                    'fresh_inspects':runner.inspections,'duration':float(probe['format']['duration']),
                    'full_decode':True,'temporary_cleaned':True,'metrics':metrics[0] if metrics else None}))
        finally:
            server.shutdown();server.server_close();thread.join(2)

    def test_hls_ts_prefix_actual_native_and_full_decode(self):self.proof('m3u8_native')
    def test_hls_nonzero_byterange_prefix_actual_native_and_full_decode(self):self.proof('m3u8_native',byterange=True)
    def test_selected_hls_missing_fragment_does_not_succeed_by_skip(self):self.proof('m3u8_native',missing=True)
    def test_exact_byte_dashy_native_prefix_and_full_decode(self):self.proof('http_dash_segments')
    def test_byte_dashy_query_206_exact_range_positive(self):self.proof('http_dash_segments',response_range='correct')
    def test_byte_dashy_same_length_wrong_range_or_total_fails_closed(self):
        for kind in ('wrong_start','wrong_total'):
            with self.subTest(kind=kind):self.proof('http_dash_segments',response_range=kind)
    def test_hls_fmp4_map_prefix_actual_native_and_full_decode(self):self.proof('m3u8_native',fmp4=True)
    def test_hls_no_map_raw_aac_explicit_native_fallback(self):self.proof('m3u8_native',raw_audio=True)
    def test_fragment_body_retry_and_ignored_resume_range_preserve_native_file_recovery(self):self.proof('m3u8_native',recovery=True)


class DownloadTimingsTests(unittest.TestCase):
    def test_fixed_stage_numeric_only_and_logging_failure_is_nonfatal(self):
        import time
        from unittest.mock import patch
        from mediaworker.downloader import timings
        with self.assertLogs(timings.log,level='INFO') as captured:
            timings.record('upload',time.monotonic(),input_bytes=123)
            timings.record('https://invalid.example/private',time.monotonic())
            timings.record('upload',time.monotonic(),protocol='private')
        self.assertEqual(len(captured.output),1)
        self.assertIn('stage=upload',captured.output[0]);self.assertIn('input_bytes=123',captured.output[0])
        with patch.object(timings.log,'info',side_effect=OSError('synthetic logging failure')):
            timings.record('complete',time.monotonic())


class DownloadTimingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self,stat_failure):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from contextlib import ExitStack
        from mediaworker import lifecycle, storage
        from mediaworker.config import Config as WorkerConfig
        from mediaworker.engine.base import Engine, RunResult
        from mediaworker.models import ClaimedJob
        from mediaworker.downloader import timings
        class Fixture(Engine):
            task_type='download'
            def run(self,ctx):
                path=ctx.workdir/'result.json';path.write_bytes(b'{}')
                return RunResult(path,1,1,'application/json')
        uploaded=[False];failed_stats=[0]
        async def upload(*_):uploaded[0]=True
        original_stat=Path.stat
        def stat(path,*args,**kwargs):
            if uploaded[0] and path.name=='result.json':
                failed_stats[0]+=1;raise OSError('synthetic timing stat after PUT')
            return original_stat(path,*args,**kwargs)
        with tempfile.TemporaryDirectory() as temporary:
            cfg=WorkerConfig.from_env(load_env=False);cfg.work_dir=Path(temporary)
            job=ClaimedJob(id='00000000-0000-4000-8000-000000000001',lease_token='00000000-0000-4000-8000-000000000002',attempt=1,
                task_type='download',params={'operation':'info'},
                result={'put_url':'https://offline.invalid/put','key':'result'})
            client=SimpleNamespace(heartbeat=AsyncMock(),complete=AsyncMock(),fail=AsyncMock())
            with ExitStack() as stack:
                stack.enter_context(patch.object(storage,'upload',side_effect=upload))
                if stat_failure:stack.enter_context(patch.object(Path,'stat',new=stat))
                else:stack.enter_context(patch.object(timings.log,'info',side_effect=OSError('synthetic logger unavailable')))
                await lifecycle._process_job(cfg,client,{'download':Fixture()},job)
            client.complete.assert_awaited_once();client.fail.assert_not_awaited()
            self.assertTrue(uploaded[0])
            if stat_failure:self.assertGreater(failed_stats[0],0)
            self.assertFalse((cfg.work_dir/job.id).exists())

    async def test_logging_failures_cannot_fail_after_accepted_download_complete(self):await self.exercise(False)
    async def test_timing_stat_failure_after_successful_put_still_completes(self):await self.exercise(True)


class FragmentEofLatchTests(unittest.TestCase):
    def test_plain_eof_shortread_cannot_be_hidden_by_later_intentional_stop(self):
        import contextlib
        import os
        import uuid
        from unittest.mock import patch
        from types import SimpleNamespace
        from yt_dlp import YoutubeDL
        from yt_dlp.downloader.http import HttpFD
        from yt_dlp.downloader.fragment import FragmentFD
        from yt_dlp.networking import Request
        from yt_dlp.utils import DownloadError
        from mediaworker.downloader import yt_runner
        from mediaworker.downloader.fresh_metadata import encode_component
        response=SimpleNamespace(status=200,headers={'Content-Length':'10'},close=lambda:None)
        pieces=iter([b'abc',b''])
        response.read=lambda *_:next(pieces)
        def original_download(*_):
            opened=YoutubeDL.urlopen(object(),Request('https://media.example/source'))
            self.assertEqual(opened.read(10),b'abc');self.assertEqual(opened.read(10),b'')
            self.assertTrue(yt_runner.SECTION_STATE['fragment_retry_pending'])
            yt_runner.SECTION_STATE['path'].touch();yt_runner.SECTION_STATE['stopped']=True
            raise DownloadError('synthetic stop after unrecovered EOF')
        def process(*_,**__):
            yt_runner.SECTION_STATE['mode']='native_hls_prefix'
            return HttpFD.real_download(object(),'unused',{})
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary).resolve();nonce=uuid.uuid4().hex
            metadata=folder/('.fresh-component-'+nonce+'.json')
            metadata.write_bytes(encode_component({'duration':12,'formats':[{'format_id':'v',
                'url':'https://media.example/source','protocol':'https','ext':'mp4','vcodec':'avc1','acodec':'aac'}]},'v'))
            stop=folder/('.section-stop-'+nonce+'.flag');metrics=folder/('.section-metrics-'+nonce+'.json')
            previous=Path.cwd()
            try:
                os.chdir(folder)
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(YoutubeDL,'urlopen',lambda *_a,**_k:response))
                    stack.enter_context(patch.object(HttpFD,'real_download',original_download))
                    stack.enter_context(patch.object(FragmentFD,'_download_fragment',FragmentFD._download_fragment))
                    stack.enter_context(patch.object(FragmentFD,'download_and_append_fragments',FragmentFD.download_and_append_fragments))
                    stack.enter_context(patch.object(YoutubeDL,'process_ie_result',process))
                    stack.enter_context(patch.object(yt_runner,'SECTION_STATE',None))
                    yt_runner.install_response_limits(fragment_prefix=True)
                    with self.assertRaises(DownloadError):yt_runner.download_fresh([
                        '--ignore-config','--no-plugin-dirs','--no-remote-components','--no-simulate','--output','-',
                        '--clapclip-info-json',str(metadata),'--clapclip-component','v',
                        '--clapclip-stream-stop',str(stop),'--clapclip-stream-metrics',str(metrics),
                        '--clapclip-stream-nonce',nonce,'--clapclip-stream-window','2'])
                stats=json.loads(metrics.read_bytes())
                self.assertTrue(stats['failed']);self.assertTrue(stats['stopped'])
            finally:os.chdir(previous)
