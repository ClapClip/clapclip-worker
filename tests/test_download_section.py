import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit
from unittest.mock import patch

from mediaworker.downloader import section_pipe
from mediaworker.downloader.fresh_metadata import encode_component
from mediaworker.downloader.safety import ServiceError
from mediaworker.downloader.worker import Config,ProcessRunner,format_options,stop_process


PRODUCER = r'''
import json,os,struct,sys,time
from pathlib import Path
flag=Path(sys.argv[sys.argv.index('--clapclip-stream-stop')+1])
metrics=Path(sys.argv[sys.argv.index('--clapclip-stream-metrics')+1])
nonce=sys.argv[sys.argv.index('--clapclip-stream-nonce')+1]
mode=sys.argv[1];count=0
prefix=struct.pack('>I4s',8,b'ftyp')+struct.pack('>I4s',8 if mode!='blocked_prefix' else 1000,b'moov')
sys.stdout.buffer.write(prefix);sys.stdout.buffer.flush();count+=len(prefix)
if mode=='blocked_prefix':time.sleep(60)
try:
    while not flag.exists():
        sys.stdout.buffer.write(b'x'*65536);sys.stdout.buffer.flush();count+=65536
finally:
    stats={'nonce':nonce,'read_bytes':count,'read_calls':count//65536,'source_bytes':999999999,'source_bytes_known':True,
           'unsupported_reason':None,'stopped':flag.exists(),'failed':mode in {'failed','spoof'},
           'mode':'native_pipe_faststart','fragments_selected':0,'fragments_total':0,'fragments_started':0,'tail_clamped':False}
    with metrics.open('x') as stream:json.dump(stats,stream)
    if mode=='spoof':os.write(2,b'clapclip_section_stats:{"read_bytes":1,"stopped":true,"failed":false}\n')
if mode in {'failed','spoof'}:sys.exit(1)
'''


class SectionCoordinationTests(unittest.TestCase):
    def exercise(self,mode='normal',consumer_block=False,cancel=False,timeout=False,stop_failure=False,start_failure=None):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        folder=Path(temporary.name);target=folder/'source-0.media'
        event=threading.Event();seen=[];calls=[]
        class Proxy:
            url='http://unused:unused@127.0.0.1:65534';port=65534
        consumer=("import time;time.sleep(60)" if consumer_block else
            "import sys;sys.stdin.buffer.read(32)")
        original=subprocess.Popen
        original_start=threading.Thread.start
        starts=[0]
        def start(thread):
            starts[0]+=1
            if starts[0]==start_failure:raise RuntimeError('synthetic thread resource failure')
            return original_start(thread)
        def launch(*a,**kw):
            result=original(*a,**kw);seen.append(result);return result
        def stopper(process):
            calls.append(process)
            if stop_failure and len(calls)==1:raise OSError('synthetic stop failure')
            stop_process(process)
        timer=None
        if cancel:
            timer=threading.Timer(.3,event.set);timer.start()
        try:
            with patch.object(section_pipe,'command',return_value=[sys.executable,'-B','-c',consumer]), \
                 patch.object(section_pipe.subprocess,'Popen',side_effect=launch), \
                 patch.object(threading.Thread,'start',new=start):
                return section_pipe.run([sys.executable,'-B','-c',PRODUCER,mode,'--output','-'],
                    'unused','',target,2,True,False,folder,event,time.monotonic()+(.3 if timeout else 8),Proxy(),stopper)
        finally:
            if timer:timer.cancel();timer.join()
            self.assertTrue(all(p.poll() is not None for p in seen))
            self.assertEqual(list(folder.glob('.section-stop-*')),[])
            self.assertEqual(list(folder.glob('.section-metrics-*')),[])
            if stop_failure:self.assertEqual(len(calls),2)

    def test_owned_python_is_actual_pid_and_retains_same_venv_without_inherited_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with patch.dict(os.environ,{'__PYVENV_LAUNCHER__':'untrusted-old-hint','PYTHONHOME':'untrusted-home','PYTHONPATH':'untrusted-path'}):
                env=section_pipe.environment(folder)
            self.assertNotIn('PYTHONHOME',env);self.assertNotIn('PYTHONPATH',env)
            self.assertNotIn('__PYVENV_LAUNCHER__',env)
            other,unchanged=section_pipe.owned_command(['explicit-other-python','-B'],env)
            self.assertEqual(other[0],'explicit-other-python');self.assertEqual(unchanged,env)
            code="import json,os,sys,yt_dlp;print(json.dumps({'pid':os.getpid(),'prefix':sys.prefix,'ytdlp':yt_dlp.__file__}))"
            args,env=section_pipe.owned_command([sys.executable,'-B','-c',code],env)
            process=subprocess.Popen(args,cwd=folder,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            output,_=process.communicate(timeout=5)
            self.assertEqual(process.returncode,0)
            result=json.loads(output)
            self.assertEqual(result['pid'],process.pid)
            self.assertEqual(Path(result['prefix']).resolve(),Path(sys.prefix).resolve())
            import yt_dlp
            self.assertEqual(Path(result['ytdlp']).resolve(),Path(yt_dlp.__file__).resolve())
            shutil.rmtree(folder)

    def test_metadata_clone_start_failure_cleans_owned_private_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);metadata=folder/'.fresh-component-test.json'
            metadata.write_text('{}')
            proxy=type('Proxy',(),{'url':'http://127.0.0.1:65534','port':65534})()
            with patch.object(section_pipe.shutil,'copyfile',side_effect=PermissionError('synthetic clone denied')):
                with self.assertRaises(section_pipe.SectionFailure):
                    section_pipe.run(['unused','--output','source-0.media','--clapclip-info-json',str(metadata)],
                        'unused','',folder/'source-0.media',2,True,False,folder,threading.Event(),time.monotonic()+2,proxy,stop_process)
            self.assertEqual(list(folder.glob('.section-*')),[])

    def test_success_only_after_intentional_stop_and_final_numeric_metrics(self):
        metrics=self.exercise()
        self.assertTrue(metrics['producer_stop_requested'])
        self.assertEqual((metrics['window_start'],metrics['window_end']),(0,2))

    def test_failed_true_cannot_be_overwritten_by_later_stop_true(self):
        with self.assertRaises(section_pipe.SectionFailure) as caught:self.exercise(mode='failed')
        self.assertEqual(caught.exception.reason,'SECTION_PRODUCER_FAILED')

    def test_untrusted_stderr_marker_cannot_override_dedicated_metrics(self):
        with self.assertRaises(section_pipe.SectionFailure) as caught:self.exercise(mode='spoof')
        self.assertEqual(caught.exception.reason,'SECTION_PRODUCER_FAILED')

    def test_blocked_prefix_and_blocked_consumer_obey_cancel(self):
        for mode,blocked in (('blocked_prefix',False),('normal',True)):
            with self.subTest(mode=mode),self.assertRaises(section_pipe.SectionFailure) as caught:
                self.exercise(mode=mode,consumer_block=blocked,cancel=True)
            self.assertEqual(caught.exception.reason,'CANCELLED')

    def test_prefix_read_obeys_same_deadline(self):
        with self.assertRaises(section_pipe.SectionFailure) as caught:self.exercise(mode='blocked_prefix',timeout=True)
        self.assertEqual(caught.exception.reason,'TIMEOUT')

    def test_first_stop_failure_still_terminates_second_child(self):
        self.assertTrue(self.exercise(stop_failure=True)['producer_stop_requested'])

    def test_thread_start_failure_reaps_every_started_owned_child(self):
        for index in (1,2,3):
            with self.subTest(index=index),self.assertRaises(section_pipe.SectionFailure) as caught:
                self.exercise(start_failure=index)
            self.assertEqual(caught.exception.reason,'SECTION_LOCAL_PROCESS_FAILED')

    def test_cleanup_retains_call_ownership_until_exact_child_and_thread_exit(self):
        released=threading.Event();finished=threading.Event()
        class Owned:
            def poll(self):return 0 if released.is_set() else None
            def kill(self):raise PermissionError('synthetic denied')
            def wait(self,timeout):
                if not released.wait(timeout):raise subprocess.TimeoutExpired('owned',timeout)
        writer=threading.Thread(target=lambda:released.wait());writer.start()
        def stopper(_):raise PermissionError('synthetic denied')
        coordinator=threading.Thread(target=lambda:(section_pipe.reap_owned([Owned()],[writer],stopper),finished.set()))
        coordinator.start()
        self.assertFalse(finished.wait(.2))
        self.assertTrue(coordinator.is_alive())
        released.set();coordinator.join(3);writer.join(1)
        self.assertTrue(finished.is_set())

    def test_worker_does_not_return_cancelled_attempt_while_owned_producer_cannot_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            work=Path(temporary);target=work/'source-0.media'
            metadata=work/('.fresh-component-'+'a'*32+'.json')
            info={'duration':12,'formats':[{'format_id':'v','url':'http://media.example/generated',
                'protocol':'http','ext':'mp4','width':640,'height':360,'vcodec':'avc1','acodec':'aac'}]}
            metadata.write_bytes(encode_component(info,'v'))
            permit=threading.Event();cancel=threading.Event();finished=threading.Event();errors=[];owned=[]
            original=subprocess.Popen
            class Wrapped:
                def __init__(self,p):self.process=p
                def __getattr__(self,name):return getattr(self.process,name)
                def kill(self):
                    if not permit.is_set():raise PermissionError('synthetic denied')
                    self.process.kill()
                def wait(self,timeout):
                    if not permit.wait(min(.1,timeout)):raise subprocess.TimeoutExpired('owned',timeout)
                    return self.process.wait(timeout=timeout)
            def launch(args,*a,**kw):
                result=Wrapped(original([sys.executable,'-B','-c',PRODUCER,'blocked_prefix',*args[2:]],*a,**kw))
                owned.append(result);return result
            class Proxy:
                port=65534;url='http://unused:unused@127.0.0.1:65534'
            runner=ProcessRunner(Config(),Proxy())
            args=[sys.executable,'unused','--output',str(target),'--clapclip-info-json',str(metadata),
                '--clapclip-component','v','--clapclip-section-limit','2','--clapclip-section-components','1',
                '--clapclip-section-deadline',str(time.monotonic()+10)]
            def execute():
                try:runner.run_section(args,work,cancel,None,8*1024**2)
                except ServiceError as error:errors.append(error.code)
                finally:finished.set()
            with patch.object(section_pipe.subprocess,'Popen',side_effect=launch), \
                 patch('mediaworker.downloader.worker.stop_process',side_effect=PermissionError('synthetic denied')):
                coordinator=threading.Thread(target=execute);coordinator.start()
                time.sleep(.2);cancel.set()
                self.assertFalse(finished.wait(.3))
                self.assertTrue(metadata.exists())
                permit.set();coordinator.join(5)
            self.assertTrue(finished.is_set());self.assertEqual(errors,['CANCELLED'])
            self.assertTrue(owned and all(p.poll() is not None for p in owned))

    def test_ffmpeg_only_has_local_pipe_and_explicit_mappings(self):
        for video,audio in ((True,True),(True,False),(False,True)):
            args=section_pipe.command('ffmpeg',Path('component.media'),300,video,audio)
            self.assertEqual(args[args.index('-protocol_whitelist')+1],'pipe')
            self.assertEqual(args[args.index('-f')+1],'mov')
            self.assertEqual(args[args.index('-enable_drefs')+1],'0')
            self.assertEqual(args[args.index('-use_absolute_path')+1],'0')
            self.assertEqual(args[args.index('-i')+1],'pipe:0')
            self.assertNotIn('-force_key_frames',args)
            self.assertEqual('0:v:0' in args,video);self.assertEqual('0:a:0' in args,audio)
        env=section_pipe.environment(Path('attempt'))
        self.assertFalse(any(k.lower().endswith('proxy') for k in env))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'ffmpeg/ffprobe required')
class NativePipeMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix='section-local-')
        cls.folder=Path(cls.temporary.name)
        cls.ffmpeg,cls.ffprobe=shutil.which('ffmpeg'),shutil.which('ffprobe')
        def generate(name,args):
            subprocess.run([cls.ffmpeg,'-nostdin','-v','error','-y',*args,str(cls.folder/name)],
                           capture_output=True,check=True,timeout=45)
        generate('mux.mp4',['-f','lavfi','-i','testsrc2=s=1280x720:r=30:d=12',
            '-f','lavfi','-i','sine=duration=12:sample_rate=48000','-c:v','libx264','-preset','veryfast',
            '-crf','12','-threads','2','-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart'])
        generate('video.mp4',['-i',str(cls.folder/'mux.mp4'),'-an','-c','copy','-movflags','+faststart'])
        generate('audio.m4a',['-i',str(cls.folder/'mux.mp4'),'-vn','-c','copy','-movflags','+faststart'])
        generate('tail.mp4',['-i',str(cls.folder/'mux.mp4'),'-c','copy'])

    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()

    def run_source(self,names,*,reported_audio=True,variant='normal'):
        data={name:(self.folder/name).read_bytes() for name in names}
        ranges=[];headers=[]
        dropped=[False]
        transient=[0]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def do_GET(self):
                name=urlsplit(self.path).path.rsplit('/',1)[-1]
                body=data.get(name)
                if body is None:self.send_error(404);return
                headers.append({k.lower():v for k,v in self.headers.items() if k.lower() in {'cookie','authorization','accept-encoding'}})
                if variant=='always_503' or variant=='recover_503' and transient[0]==0:
                    transient[0]+=1
                    self.send_response(503);self.send_header('Content-Length','0');self.send_header('Connection','close');self.end_headers();return
                match=re.fullmatch(r'bytes=(\d+)-(\d*)',self.headers.get('Range',''))
                start=int(match[1]) if match else 0
                end=min(int(match[2]) if match and match[2] else len(body)-1,len(body)-1)
                chunked=variant=='chunked_no_length'
                ignored=variant=='restart_range' and start>0
                if chunked or ignored:start,end=0,len(body)-1
                if start>=len(body):self.send_error(416);return
                ranges.append((name,start,end))
                self.send_response(206 if match and not ignored and not chunked else 200)
                self.send_header('Content-Type','video/mp4');self.send_header('Connection','close')
                if chunked:self.send_header('Transfer-Encoding','chunked')
                else:self.send_header('Content-Length',str(end-start+1))
                if match and not ignored and not chunked:self.send_header('Content-Range',f'bytes {start}-{end}/{len(body)}')
                self.end_headers()
                try:
                    if variant=='drop_once' and not dropped[0]:
                        dropped[0]=True;self.wfile.write(body[start:min(start+65536,end+1)])
                        self.wfile.flush();self.close_connection=True;return
                    for offset in range(start,end+1,65536):
                        data_chunk=body[offset:min(offset+65536,end+1)]
                        if chunked:self.wfile.write(f'{len(data_chunk):x}\r\n'.encode()+data_chunk+b'\r\n')
                        else:self.wfile.write(data_chunk)
                    if chunked:self.wfile.write(b'0\r\n\r\n')
                except OSError:pass
        class Server(ThreadingHTTPServer):
            daemon_threads=True
            def handle_error(self,*_):pass
        server=Server(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        class Proxy:
            port=server.server_port;url=f'http://127.0.0.1:{server.server_port}'
            def allow_media(self):pass
        try:
            info={'duration':12,'formats':[{'format_id':'v','url':'http://media.example/'+names[0],
                'protocol':'http','ext':'mp4','width':1280,'height':720,'vcodec':'avc1',
                'acodec':'aac' if len(names)==1 and reported_audio else 'none',
                'downloader_options':{'http_chunk_size':256*1024}}]}
            if len(names)==2:
                info['formats'].append({'format_id':'a','url':'http://media.example/'+names[1],
                    'protocol':'http','ext':'m4a','vcodec':'none','acodec':'aac',
                    'downloader_options':{'http_chunk_size':256*1024}})
            config=Config(max_duration=2,job_timeout=30,python=sys.executable,ffmpeg=self.ffmpeg,
                          ffprobe=self.ffprobe,node=shutil.which('node') or '')
            class Runner(ProcessRunner):
                inspect_calls=0
                def inspect(self,*_):self.inspect_calls+=1;return info
            runner=Runner(config,Proxy())
            with tempfile.TemporaryDirectory(prefix='section-attempt-') as temporary:
                work=Path(temporary)
                if variant=='always_503':
                    with self.assertRaises(ServiceError) as caught:
                        runner.download({'cancel':threading.Event()},{'url':'http://media.example/page'},
                            next(o for o in format_options(info).values() if not o['audio']),work,lambda *_:None)
                    self.assertEqual(caught.exception.code,'MEDIA_UNAVAILABLE')
                    self.assertEqual(runner.inspect_calls,1)
                    self.assertLessEqual(transient[0],2)
                    self.assertFalse(list(work.glob('.section-*')) or list(work.glob('.fresh-component-*')))
                    print('clapclip_local_section_proof:'+json.dumps({'variant':variant,'outcome':'unrecovered_failure',
                        'fresh_inspects':runner.inspect_calls,'original_retry_requests':transient[0],'native_fallback':False}))
                    return {'outcome':'failed_as_expected'}
                with self.assertLogs('mediaworker.downloader.worker',level='INFO') as logs:
                    target=runner.download({'cancel':threading.Event()},{'url':'http://media.example/page'},
                        next(o for o in format_options(info).values() if not o['audio']),work,lambda *_:None)
                probe=json.loads(subprocess.run([self.ffprobe,'-v','error','-show_entries',
                    'format=duration:stream=codec_type,duration,width,height','-of','json',str(target)],
                    capture_output=True,check=True).stdout)
                self.assertLessEqual(float(probe['format']['duration']),2)
                for stream in probe['streams']:
                    if 'duration' in stream:self.assertLessEqual(float(stream['duration']),2)
                subprocess.run([self.ffmpeg,'-nostdin','-v','error','-xerror','-protocol_whitelist','file,pipe',
                    '-i',str(target),'-map','0:v:0','-map','0:a:0?','-f','null','-'],capture_output=True,check=True,timeout=10)
                self.assertEqual(runner.inspect_calls,1)
                self.assertEqual(list(work.glob('.fresh-component-*')),[])
                self.assertEqual(list(work.glob('.section-*')),[])
                metrics=[json.loads(line.split('download section ',1)[1]) for line in logs.output if 'download section {' in line]
                self.assertTrue(all('cookie' not in row and 'authorization' not in row for row in headers))
                fallback=names[0]=='tail.mp4' or variant in {'restart_range','drop_once'}
                if not fallback:
                    self.assertEqual(len(metrics),len(names))
                    self.assertTrue(all(m['window_start']==0 and m['window_end']==2 and m['format_preserved'] for m in metrics))
                    # A tiny audio file can fit in native/OS read-ahead in full.
                    # Prove the actual combined body budget fell, not that each
                    # fetched byte maps exactly to the requested timeline.
                    self.assertLess(sum(m['upstream_read_bytes'] for m in metrics),sum(len(value) for value in data.values()))
                    if variant=='chunked_no_length':
                        self.assertTrue(all(m['source_bytes']==0 and not m['source_bytes_known'] for m in metrics))
                    else:
                        self.assertTrue(all(m['source_bytes']==len(data[names[m['component']]]) and m['source_bytes_known'] for m in metrics))
                        self.assertTrue(all(end-start+1<=256*1024 for _,start,end in ranges))
                    self.assertTrue(all(h.get('accept-encoding')=='identity' for h in headers))
                else:
                    self.assertTrue(any('mode=full_native' in line for line in logs.output))
                    if variant=='restart_range':self.assertTrue(any('SECTION_NATIVE_RESTART_REQUIRED' in line for line in logs.output))
                    if variant=='drop_once':self.assertTrue(any('SECTION_NATIVE_RECOVERY_REQUIRED' in line for line in logs.output))
                proof={'component_count':len(names),'source_duration':12,'window':[0,2],
                    'variant':variant,
                    'source_bytes':sum(len(v) for v in data.values()),'section_metrics':metrics,'full_decode':'pass',
                    'native_chunk_size':256*1024,'max_requested_chunk':max(end-start+1 for _,start,end in ranges),
                    'fresh_inspects':runner.inspect_calls,'temporary_json_and_stop_metrics_removed':True,
                    'audio_streams':sum(s['codec_type']=='audio' for s in probe['streams'])}
                print('clapclip_local_section_proof:'+json.dumps(proof,sort_keys=True))
                return proof
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)

    def test_real_native_chunks_reduce_muxed_source_bytes_and_decode(self):
        self.assertEqual(self.run_source(['mux.mp4'])['audio_streams'],1)

    def test_split_components_share_window_and_keep_format_identity(self):
        self.assertEqual(self.run_source(['video.mp4','audio.m4a'],reported_audio=False)['audio_streams'],1)

    def test_single_video_keeps_real_optional_audio_and_allows_real_silence(self):
        self.assertEqual(self.run_source(['mux.mp4'],reported_audio=False)['audio_streams'],1)
        self.assertEqual(self.run_source(['video.mp4'],reported_audio=False)['audio_streams'],0)

    def test_nonfaststart_closes_producer_then_explicitly_falls_back(self):
        self.run_source(['tail.mp4'])

    def test_chunked_source_without_content_length_remains_eligible(self):
        self.run_source(['mux.mp4'],variant='chunked_no_length')

    def test_later_ignored_range_cleans_pipe_then_native_file_recovers(self):
        self.run_source(['mux.mp4'],variant='restart_range')

    def test_opened_body_short_read_cleans_pipe_then_native_file_recovers(self):
        self.run_source(['mux.mp4'],variant='drop_once')

    def test_original_native_503_retry_recovers_without_latching_failed(self):
        self.run_source(['mux.mp4'],variant='recover_503')

    def test_unrecovered_native_503_still_fails(self):
        self.run_source(['mux.mp4'],variant='always_503')

    def test_real_mov_external_reference_positive_control_and_hardened_pipe_denial(self):
        body=(self.folder/'mux.mp4').read_bytes()
        # FFmpeg 7.1 parses external dref paths from QuickTime 'alis', not
        # arbitrary 'url ' entries. A metadata-only MOV must read this generated
        # external media file to produce any packet in the positive control.
        path=(self.folder/'mux.mp4').resolve()
        reference=path.as_posix()
        if path.drive:reference=reference[len(path.drive):]
        reference=reference.encode()
        alias=(b'\0'*10+b'\0'+b'\0'*27+b'\0'*12+b'\0'+b'\0'*63+b'\0'*16
               +struct.pack('>HH',1,1)+b'\0'*16+struct.pack('>HH',2,len(reference))+reference
               +(b'\0' if len(reference)%2 else b'')+struct.pack('>hH',-1,0))
        containers={b'moov',b'trak',b'mdia',b'minf',b'dinf'}
        replaced=[0]
        def rewrite(data):
            output=bytearray();offset=0
            while offset<len(data):
                size,kind=struct.unpack('>I4s',data[offset:offset+8])
                self.assertGreaterEqual(size,8)
                payload=data[offset+8:offset+size]
                if kind in containers:payload=rewrite(payload)
                if kind==b'dref':
                    count=struct.unpack('>I',payload[4:8])[0]
                    self.assertEqual(count,1)
                    entry=struct.pack('>I4s',12+len(alias),b'alis')+b'\0'*4+alias
                    payload=payload[:8]+entry;replaced[0]+=1
                if kind!=b'mdat':output.extend(struct.pack('>I4s',len(payload)+8,kind)+payload)
                offset+=size
            return bytes(output)
        malicious=rewrite(body)
        self.assertGreater(replaced[0],0)
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            control=section_pipe.command(self.ffmpeg,folder/'control.mp4',1,True,'optional')
            control[control.index('-protocol_whitelist')+1]='file,pipe'
            control[control.index('-enable_drefs')+1]='1'
            control[control.index('-use_absolute_path')+1]='1'
            result=subprocess.run(control,input=malicious,capture_output=True,cwd=self.folder,
                                  env=section_pipe.environment(folder),timeout=10)
            self.assertEqual(result.returncode,0,'external-reference positive control must produce real packets')
            subprocess.run([self.ffmpeg,'-nostdin','-v','error','-i',str(folder/'control.mp4'),'-frames:v','1',
                '-f','null','-'],capture_output=True,check=True,timeout=10)
            def packets(path):
                result=subprocess.run([self.ffprobe,'-v','error','-count_packets','-show_entries',
                    'stream=nb_read_packets','-of','json',str(path)],capture_output=True,timeout=10)
                if result.returncode:return 0
                return sum(int(stream.get('nb_read_packets',0)) for stream in json.loads(result.stdout).get('streams',[]))
            self.assertGreater(packets(folder/'control.mp4'),0)
            hardened=section_pipe.command(self.ffmpeg,folder/'blocked.mp4',1,True,'optional')
            result=subprocess.run(hardened,input=malicious,capture_output=True,cwd=self.folder,
                                  env=section_pipe.environment(folder),timeout=10)
            # ffmpeg may return zero for a header-only, zero-packet copy. The
            # strict downstream duration/probe gate rejects it; returncode alone
            # is deliberately not the evidence for blocking the external read.
            self.assertEqual(packets(folder/'blocked.mp4'),0)
            # Independently prove the dref switch too, even if a future caller
            # accidentally broadens the input protocol list back to file,pipe.
            hardened[hardened.index('-protocol_whitelist')+1]='file,pipe'
            subprocess.run(hardened,input=malicious,capture_output=True,cwd=self.folder,
                           env=section_pipe.environment(folder),timeout=10)
            self.assertEqual(packets(folder/'blocked.mp4'),0)
            print('clapclip_local_dref_proof:'+json.dumps({'positive_control_external_track_decoded':True,'hardened_external_packets':0}))

    def test_real_playlist_positive_control_is_rejected_as_mov_pipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);playlist=folder/'generated.ffconcat'
            playlist.write_text("ffconcat version 1.0\nfile '"+(self.folder/'mux.mp4').as_posix()+"'\n",encoding='utf-8')
            control=subprocess.run([self.ffmpeg,'-nostdin','-v','error','-f','concat','-safe','0','-i',str(playlist),
                '-t','0.1','-f','null','-'],capture_output=True,env=section_pipe.environment(folder),timeout=10)
            self.assertEqual(control.returncode,0)
            hardened=subprocess.run(section_pipe.command(self.ffmpeg,folder/'blocked.mp4',1,True,'optional'),
                input=playlist.read_bytes(),capture_output=True,env=section_pipe.environment(folder),timeout=10)
            self.assertNotEqual(hardened.returncode,0)
            print('clapclip_local_playlist_proof:'+json.dumps({'positive_control_parsed':True,'forced_mov_rejected':True}))


if __name__=='__main__':unittest.main()
