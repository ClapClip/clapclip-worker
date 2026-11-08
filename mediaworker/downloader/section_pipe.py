"""Bounded native-HTTP producer -> local MOV pipe -> first-window component.

No new network client or FFmpeg network protocol. The existing isolated yt-dlp
producer retains its small HTTP chunks, proxy, redirects and socket guards.
"""
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import struct
import subprocess
import sys
import threading
import time
import uuid

CHUNK = 64 * 1024
MAX_PREFIX = 8 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
log=logging.getLogger(__name__)


class SectionFailure(Exception):
    def __init__(self, reason, *, fallback=False):
        self.reason, self.fallback = reason, fallback
        super().__init__(reason)


def reap_owned(processes,threads,stopper):
    """Never return the execution future/slot while an owned writer survives."""
    processes=[p for p in processes if p is not None]
    for process in processes:
        try:stopper(process)
        except Exception:
            # A failed tree helper must not skip the other child's cleanup.
            # Popen.kill targets our retained native process handle on Windows.
            try:
                if process.poll() is None:process.kill()
                process.wait(timeout=2)
            except (OSError,subprocess.SubprocessError):pass
    for thread in threads:thread.join(timeout=2)
    announced=False
    while True:
        alive=[]
        for process in processes:
            try:
                if process.poll() is None:alive.append(process)
            except OSError:alive.append(process)
        pending=[thread for thread in threads if thread.is_alive()]
        if not alive and not pending:return
        if not announced:
            log.warning("download section cleanup awaiting owned resources children=%d threads=%d",len(alive),len(pending))
            announced=True
        # This is cleanup ownership, not a fresh processing budget. Keeping the
        # synchronous attempt alive preserves lifecycle's slot and workdir.
        for process in alive:
            try:process.kill()
            except OSError:pass
            try:process.wait(timeout=.1)
            except (OSError,subprocess.SubprocessError):pass
        for thread in pending:thread.join(timeout=.1)
        if not pending:time.sleep(.05)


def environment(directory, proxy=None, port=None, node=None):
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {k:v for k,v in os.environ.items() if k.upper() in keep}
    env.update(HOME=str(directory), USERPROFILE=str(directory), TEMP=str(directory), TMP=str(directory),
               TMPDIR=str(directory), PYTHONUTF8="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    if proxy is not None:
        env.update(CLAPCLIP_DOWNLOAD_POLICY_VERSION="2", CLAPCLIP_PROXY_PORT=str(port), CLAPCLIP_NODE=node,
                   HTTP_PROXY=proxy, HTTPS_PROXY=proxy, ALL_PROXY=proxy, NO_PROXY="")
    return env


def owned_command(args, env):
    """On Windows retain the actual Python handle, not its venv redirector.

Only our exact interpreter can use our trusted base and venv hint. The hint is
constructed after environment filtering; no inherited Python overrides enter.
"""
    args, env = list(args), dict(env)
    if os.name == "nt" and args and Path(args[0]).resolve() == Path(sys.executable).resolve():
        args[0] = sys._base_executable
        env["__PYVENV_LAUNCHER__"] = sys.executable
    return args, env


def command(ffmpeg, target, window, video, audio, container="mov"):
    args=[ffmpeg,"-nostdin","-hide_banner","-v","error","-y",
          "-protocol_whitelist","pipe","-format_whitelist",container,"-f",container]
    if container=="mov":args += ["-enable_drefs","0","-use_absolute_path","0"]
    args += ["-i","pipe:0"]
    if video:args += ["-map","0:v:0"]
    if audio:args += ["-map","0:a:0?" if audio=="optional" else "0:a:0"]
    return args+["-map_metadata","0","-map_chapters","-1","-c","copy","-t",str(window),"-f","mp4",str(target)]


def run(producer_args, ffmpeg, node, target, window, video, audio, directory, cancel, deadline, proxy, stopper):
    producer = consumer = None
    threads=[]
    producer_directory=Path(tempfile.mkdtemp(prefix=".section-native-",dir=directory))
    stop_path=producer_directory/(".section-stop-"+uuid.uuid4().hex+".flag")
    metrics_path=producer_directory/(".section-metrics-"+uuid.uuid4().hex+".json")
    nonce=secrets.token_hex(16)
    ready, start_consumer, drain, done = (threading.Event() for _ in range(4))
    state={"failure":None,"prefix_bytes":0,"pipe_bytes":0,"output_total":0,"stats":None,"container":"mov"}
    tails={"producer":bytearray(),"consumer":bytearray()}
    guard=threading.Lock()

    def budget():
        if cancel.is_set():raise SectionFailure("CANCELLED")
        if time.monotonic() >= deadline:raise SectionFailure("TIMEOUT")

    def load_metrics():
        if not metrics_path.is_file() or metrics_path.is_symlink() or metrics_path.stat().st_size>2048:
            raise SectionFailure("SECTION_METRICS_UNCONFIRMED")
        try:stats=json.loads(metrics_path.read_bytes())
        except (OSError,ValueError):raise SectionFailure("SECTION_METRICS_UNCONFIRMED") from None
        if (not isinstance(stats,dict) or type(stats.get("read_bytes")) is not int
                or type(stats.get("read_calls")) is not int or type(stats.get("source_bytes")) is not int
                or min(stats["read_bytes"],stats["read_calls"],stats["source_bytes"])<0
                or not isinstance(stats.get("stopped"),bool) or not isinstance(stats.get("failed"),bool)
                or not isinstance(stats.get("source_bytes_known"),bool)
                or stats["source_bytes_known"]!=(stats["source_bytes"]>0)
                or stats.get("unsupported_reason") not in {None,"SECTION_NATIVE_RESTART_REQUIRED","SECTION_NATIVE_RECOVERY_REQUIRED","SECTION_NATIVE_ENCODING_UNSUPPORTED","SECTION_NATIVE_FRAGMENT_UNSUPPORTED"}
                or stats.get("mode") not in {"native_pipe_faststart","native_hls_prefix","native_dash_byte_pipe"}
                or any(type(stats.get(key)) is not int or stats[key]<0 for key in
                       ("fragments_selected","fragments_total","fragments_started"))
                or stats.get("fragments_selected",0)>stats.get("fragments_total",0)
                or not isinstance(stats.get("tail_clamped"),bool)
                or stats.get("nonce")!=nonce):
            raise SectionFailure("SECTION_METRICS_UNCONFIRMED")
        return stats

    def producer_failure(default="SECTION_PRODUCER_FAILED"):
        # Metrics are written before producer exit. Wait only within the same
        # original budget if EOF/consumer exit raced that final controlled write.
        while producer.poll() is None:
            budget();time.sleep(.01)
        try:stats=load_metrics()
        except SectionFailure:return SectionFailure(default)
        if not stats["failed"] and stats["unsupported_reason"]:
            return SectionFailure(stats["unsupported_reason"],fallback=True)
        return SectionFailure(default)

    def check():
        budget()
        if (state["failure"] is not None and state["failure"].reason=="SECTION_PRODUCER_SHORT_READ"
                and producer is not None):
            raise producer_failure("SECTION_PRODUCER_SHORT_READ")
        if state["failure"]:raise state["failure"]

    def capture(process, name):
        try:
            while True:
                data=process.stderr.read1(CHUNK)
                if not data:break
                with guard:
                    state["output_total"]+=len(data)
                    if state["output_total"]>MAX_OUTPUT:
                        state["failure"]=SectionFailure("PROCESS_OUTPUT_LIMIT")
                tails[name].extend(data)
                if len(tails[name])>32768:del tails[name][:-32768]
        finally:process.stderr.close()

    def pump():
        try:
            prefix=bytearray()
            def take(count):
                if count<0 or len(prefix)+count>MAX_PREFIX:
                    raise SectionFailure("SECTION_PREFIX_UNSUPPORTED",fallback=True)
                data=bytearray()
                while len(data)<count:
                    check()
                    part=producer.stdout.read1(min(CHUNK,count-len(data)))
                    if not part:raise SectionFailure("SECTION_PRODUCER_SHORT_READ")
                    data.extend(part)
                prefix.extend(data)
                return data
            first=take(8)
            # Classify bytes, not HLS ext/MAP presence. Three actual TS packet
            # sync points suffice to select only the fixed native MPEG-TS demuxer.
            if first[0]==0x47:
                take(188*3-len(prefix))
                if not all(prefix[offset]==0x47 for offset in (0,188,376)):
                    raise SectionFailure("SECTION_PREFIX_UNSUPPORTED",fallback=True)
                state["container"]="mpegts"
            while state["container"]=="mov":
                size,kind=struct.unpack(">I4s",first);header=8
                if size==1:size=struct.unpack(">Q",take(8))[0];header=16
                if size<header or size>MAX_PREFIX-len(prefix)+header:
                    raise SectionFailure("SECTION_PREFIX_UNSUPPORTED",fallback=True)
                if kind not in {b"ftyp",b"free",b"skip",b"wide",b"pdin",b"moov"}:
                    raise SectionFailure("SECTION_NOT_FASTSTART",fallback=True)
                take(size-header)
                if kind==b"moov":break
                first=take(8)
            state["prefix_bytes"]=len(prefix)
            ready.set()
            while not start_consumer.wait(.05):check()
            for offset in range(0,len(prefix),CHUNK):
                check();consumer.stdin.write(prefix[offset:offset+CHUNK])
                state["pipe_bytes"]+=min(CHUNK,len(prefix)-offset)
            prefix.clear()
            while True:
                check()
                data=producer.stdout.read1(CHUNK)
                if not data:break
                if not drain.is_set():
                    try:
                        consumer.stdin.write(data)
                        state["pipe_bytes"]+=len(data)
                    except (BrokenPipeError,OSError):
                        # Do not drain the producer ahead of the coordinator's
                        # confirmed-success stop marker.
                        while not drain.wait(.05):check()
        except SectionFailure as error:
            state["failure"]=error
        except (OSError,ValueError):
            if not drain.is_set():state["failure"]=SectionFailure("SECTION_PIPE_FAILED")
        finally:
            ready.set();done.set()
            if consumer is not None and consumer.stdin is not None:
                try:consumer.stdin.close()
                except OSError:pass

    def launch(args, env, cwd=directory, **streams):
        args, env = owned_command(args, env)
        return subprocess.Popen(args,cwd=cwd,env=env,shell=False,
            start_new_session=os.name!="nt",creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0,**streams)

    def start_thread(fn,*args):
        thread=threading.Thread(target=fn,args=args,daemon=True);thread.start();threads.append(thread)

    successful=False
    try:
        producer_args=list(producer_args)
        if "--clapclip-info-json" in producer_args:
            index=producer_args.index("--clapclip-info-json")+1
            original_metadata=Path(producer_args[index])
            cloned_metadata=producer_directory/original_metadata.name
            shutil.copyfile(original_metadata,cloned_metadata)
            producer_args[index]=str(cloned_metadata)
        producer_args[producer_args.index("--output")+1]="-"
        producer_args += ["--quiet","--no-progress","--clapclip-stream-stop",str(stop_path),
                          "--clapclip-stream-metrics",str(metrics_path),"--clapclip-stream-nonce",nonce,
                          "--clapclip-stream-window",str(window)]
        check()
        producer=launch(producer_args,environment(producer_directory,proxy.url,proxy.port,node),cwd=producer_directory,
                        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        start_thread(capture,producer,"producer");start_thread(pump)
        while not ready.wait(.05):check()
        check()
        if producer.poll() not in {None,0}:raise producer_failure()
        consumer=launch(command(ffmpeg,target,window,video,audio,state["container"]),environment(directory),
                        stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        start_thread(capture,consumer,"consumer");start_consumer.set()
        while consumer.poll() is None:
            check()
            if producer.poll() not in {None,0}:raise producer_failure()
            time.sleep(.05)
        check()
        if consumer.returncode:
            if done.is_set():raise producer_failure("SECTION_CONSUMER_FAILED")
            raise SectionFailure("SECTION_CONSUMER_FAILED")
        # A failure that occurred before our intentional stop is never accepted
        # just because a truncated source still made a valid short container.
        if producer.poll() not in {None,0}:raise producer_failure()
        stop_path.touch(exist_ok=False)
        drain.set()
        while producer.poll() is None:
            check();time.sleep(.05)
        for thread in threads:thread.join(timeout=2)
        check()
        stats=load_metrics()
        if stats["failed"]:raise SectionFailure("SECTION_PRODUCER_FAILED")
        if stats["unsupported_reason"]:raise SectionFailure(stats["unsupported_reason"],fallback=True)
        if producer.returncode and not stats["stopped"]:
            raise SectionFailure("SECTION_PRODUCER_FAILED")
        if any(thread.is_alive() for thread in threads):raise SectionFailure("SECTION_THREAD_UNCONFIRMED")
        successful=True
        return {"section_mode":stats.get("mode","native_pipe_faststart"),"container":state["container"],
                "fragments_started":stats.get("fragments_started",0),
                "fragments_selected":stats.get("fragments_selected",0),"fragments_total":stats.get("fragments_total",0),
                "tail_clamped":stats.get("tail_clamped",False),"window_start":0,"window_end":window,
                "prefix_bytes":state["prefix_bytes"],"pipe_bytes":state["pipe_bytes"],
                "upstream_read_bytes":stats["read_bytes"],"upstream_read_calls":stats["read_calls"],
                "source_bytes":stats["source_bytes"],"source_bytes_known":stats["source_bytes_known"],
                "source_bytes_origin":"fresh_filesize" if stats["mode"]=="native_dash_byte_pipe" else
                    "http_response" if stats["source_bytes_known"] else "unknown",
                "producer_stop_requested":stats["stopped"]}
    except (OSError,ValueError,TypeError,RuntimeError):
        raise SectionFailure("SECTION_LOCAL_PROCESS_FAILED") from None
    finally:
        # Kill direct children before closing their pipe objects, which wakes a
        # blocked read/write. Never wait on a file-object lock before termination.
        drain.set();start_consumer.set()
        primary=sys.exc_info()[1]
        reap_owned((consumer,producer),threads,stopper)
        cleanup_failed=False
        if producer is not None and producer.stdout is not None:producer.stdout.close()
        for path in ([stop_path,metrics_path] if successful else [stop_path,metrics_path,target]):
            try:path.unlink(missing_ok=True)
            except OSError:cleanup_failed=True
        try:shutil.rmtree(producer_directory)
        except OSError:cleanup_failed=True
        if cleanup_failed:
            if not (isinstance(primary,SectionFailure) and primary.reason in {"CANCELLED","TIMEOUT"}):
                raise SectionFailure("SECTION_CLEANUP_FAILED") from None
