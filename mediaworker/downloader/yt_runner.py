"""Isolated yt-dlp entry point: only proxy sockets and the official JS runtime.

This runs in its own process with a minimal environment, no user configuration,
no plugins, and no shell. The service invokes ffmpeg separately on local files.
"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import re
import json

SECTION_STATE = None

def download_fresh(arguments):
    """Use one already resolved component; never re-extract an expiring page."""
    import yt_dlp
    from yt_dlp.globals import plugin_dirs
    from yt_dlp.utils import DownloadError
    if __package__:
        from .fresh_metadata import read_component
    else:
        from fresh_metadata import read_component

    args = list(arguments)
    stop_path = None
    metrics_path = nonce = None
    window = None
    if "--clapclip-stream-stop" in args:
        if args.count("--clapclip-stream-stop") != 1:
            raise ValueError("INVALID_FRESH_METADATA")
        index = args.index("--clapclip-stream-stop")
        if index + 1 >= len(args):raise ValueError("INVALID_FRESH_METADATA")
        stop_path = Path(args[index+1])
        del args[index:index+2]
        if (stop_path.exists() or stop_path.is_symlink()
                or stop_path.resolve().parent != Path.cwd().resolve()
                or not re.fullmatch(r"\.section-stop-[0-9a-f]{32}\.flag",stop_path.name)):
            raise ValueError("INVALID_FRESH_METADATA")
        if args.count("--clapclip-stream-window") != 1:raise ValueError("INVALID_FRESH_METADATA")
        index=args.index("--clapclip-stream-window")
        window=int(args[index+1]);del args[index:index+2]
        if not 1<=window<=300:raise ValueError("INVALID_FRESH_METADATA")
        values={}
        for flag in ("--clapclip-stream-metrics","--clapclip-stream-nonce"):
            if args.count(flag)!=1:raise ValueError("INVALID_FRESH_METADATA")
            index=args.index(flag)
            if index+1>=len(args):raise ValueError("INVALID_FRESH_METADATA")
            values[flag]=args[index+1];del args[index:index+2]
        metrics_path=Path(values["--clapclip-stream-metrics"]);nonce=values["--clapclip-stream-nonce"]
        if (metrics_path.exists() or metrics_path.is_symlink() or metrics_path.resolve().parent!=Path.cwd().resolve()
                or not re.fullmatch(r"\.section-metrics-[0-9a-f]{32}\.json",metrics_path.name)
                or not re.fullmatch(r"[0-9a-f]{32}",nonce)):
            raise ValueError("INVALID_FRESH_METADATA")
    values = {}
    for flag in ("--clapclip-info-json", "--clapclip-component"):
        if args.count(flag) != 1:
            raise ValueError("INVALID_FRESH_METADATA")
        index = args.index(flag)
        if index + 1 >= len(args):
            raise ValueError("INVALID_FRESH_METADATA")
        values[flag] = args[index + 1]
        del args[index:index + 2]
    path = Path(values["--clapclip-info-json"])
    if (path.is_symlink() or path.resolve().parent != Path.cwd().resolve()
            or not re.fullmatch(r"\.fresh-component-[0-9a-f]{32}\.json", path.name)):
        raise ValueError("INVALID_FRESH_METADATA")
    info = read_component(path, values["--clapclip-component"])
    parsed = yt_dlp.parse_options(args)
    if parsed.urls:
        raise ValueError("INVALID_FRESH_METADATA")
    options = parsed.ydl_opts
    template = options.get("outtmpl", {}).get("default")
    if ((stop_path is None and (not isinstance(template, str) or Path(template).resolve().parent != Path.cwd().resolve()
            or Path(template).name not in {"source-0.media", "source-1.media"}))
            or (stop_path is not None and template != "-")):
        raise ValueError("INVALID_FRESH_METADATA")
    # CLI _real_main normally initializes this global before YoutubeDL does.
    # The controlled API entry must disable plugins explicitly as well.
    plugin_dirs.value = []
    options.update({"remote_components": set(), "cookiefile": None,
                    "cookiesfrombrowser": None, "outtmpl": {"default": template},
                    "noplaylist": True})

    class FreshYoutubeDL(yt_dlp.YoutubeDL):
        def extract_info(self, *args, **kwargs):
            raise DownloadError("CLAPCLIP_REEXTRACT_DISABLED")

    global SECTION_STATE
    if stop_path is not None:
        SECTION_STATE = {"path":stop_path,"read_bytes":0,"read_calls":0,"source_bytes":0,
                         "stopped":False,"failed":False,"unsupported_reason":None,"http_retry_pending":False,"fragment_retry_pending":False,"mode":"native_pipe_faststart",
                         "fragments_selected":0,"fragments_total":0,"fragments_started":0,"tail_clamped":False,"dash_spans":{}}
        options.update(quiet=True,noprogress=True,logtostderr=True,forceprint={},print_to_file={},progress_hooks=[],
                       concurrent_fragment_downloads=1,skip_unavailable_fragments=False)
        # Controlled constant, not an imported extractor credential/header.
        info["formats"][0].setdefault("http_headers",{})["Accept-Encoding"]="identity"
    try:
        with FreshYoutubeDL(options) as ydl:
            if stop_path is not None:
                if __package__:
                    from .native_prefix import prepare, UnsupportedPrefix
                else:
                    from native_prefix import prepare, UnsupportedPrefix
                try:
                    SECTION_STATE.update(prepare(info,ydl,window))
                    if SECTION_STATE["mode"]=="native_dash_byte_pipe":
                        from urllib.parse import parse_qsl,urlsplit
                        for fragment in info["formats"][0]["fragments"]:
                            span=next(v for k,v in parse_qsl(urlsplit(fragment["url"]).query) if k=="range")
                            start,end=map(int,span.split("-"))
                            SECTION_STATE["dash_spans"][fragment["url"]]=(start,end)
                except (UnsupportedPrefix,UnicodeError):
                    SECTION_STATE["unsupported_reason"]="SECTION_NATIVE_FRAGMENT_UNSUPPORTED"
                    raise DownloadError("CLAPCLIP_SECTION_FRAGMENT_UNSUPPORTED") from None
            ydl.process_ie_result(info, download=True)
            if ydl._download_retcode:
                raise DownloadError("CLAPCLIP_FRESH_DOWNLOAD_FAILED")
    except (DownloadError,OSError):
        if (SECTION_STATE is not None and not SECTION_STATE["stopped"]
                and SECTION_STATE["unsupported_reason"] is None):
            SECTION_STATE["failed"] = True
        raise
    finally:
        if SECTION_STATE is not None:
            stats={key:SECTION_STATE[key] for key in ("read_bytes","read_calls","source_bytes","stopped","failed","unsupported_reason",
                      "mode","fragments_selected","fragments_total","fragments_started","tail_clamped")}
            stats["failed"] = stats["failed"] or SECTION_STATE["http_retry_pending"] or SECTION_STATE["fragment_retry_pending"]
            stats["source_bytes_known"]=SECTION_STATE["source_bytes"]>0
            stats["nonce"]=nonce
            payload=json.dumps(stats,separators=(",",":"),allow_nan=False).encode()
            if len(payload)>2048:raise ValueError("INVALID_SECTION_METRICS")
            descriptor=os.open(metrics_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,"wb") as stream:stream.write(payload)



def install_response_limits(max_metadata_bytes=8 * 1024 * 1024, *, fragment_prefix=False):
    """Bound decoded HTTP metadata even during a media re-extraction.

    HttpFD only streams selected media/fragments to disk. All other requests
    (extractor pages/JSON, HLS manifests/keys and JS metadata) stay bounded.
    Classification is by the downloader call, never untrusted Content-Type.
    """
    from yt_dlp import YoutubeDL
    from yt_dlp.downloader.http import HttpFD
    from yt_dlp.networking.exceptions import HTTPError, TransportError

    context = threading.local()
    original_open = YoutubeDL.urlopen
    original_download = HttpFD.real_download
    if fragment_prefix:
        from yt_dlp.downloader.fragment import FragmentFD
        original_fragments = FragmentFD.download_and_append_fragments
        original_fragment = FragmentFD._download_fragment

    def limit(response):
        original_read = response.read
        received = 0

        def read(amount=None):
            nonlocal received
            available = max_metadata_bytes - received + 1
            amount = available if amount is None or amount < 0 else min(amount, available)
            data = original_read(amount)
            received += len(data)
            if received > max_metadata_bytes:
                response.close()
                raise TransportError("Metadata response exceeds byte limit")
            return data

        response.read = read
        return response

    def urlopen(self, *args, **kwargs):
        section = SECTION_STATE if getattr(context,"media",False) else None
        if section is not None and section["unsupported_reason"] is not None:
            raise TransportError("CLAPCLIP_SECTION_NATIVE_RESTART_REQUIRED")
        if section is not None and section["path"].exists():
            section["stopped"]=True
            raise TransportError("CLAPCLIP_SECTION_STOPPED")
        try:
            response = original_open(self, *args, **kwargs)
        except HTTPError as error:
            if section is not None:
                if 500 <= error.status < 600:
                    # HttpFD owns its existing bounded retry policy. A later
                    # positive response clears this; an unserved retry cannot
                    # be disguised by a racing intentional consumer stop.
                    section["http_retry_pending"]=True
                else:section["failed"]=True
            # Extractors may parse JSON/HTML error bodies as metadata too.
            limit(error.response)
            raise
        if section is None:
            return response if getattr(context, "media", False) else limit(response)
        if getattr(response,"status",None) in {200,206}:section["http_retry_pending"]=False
        if response.headers.get("Content-Encoding","identity").lower() not in {"identity",""}:
            response.close();section["unsupported_reason"]="SECTION_NATIVE_ENCODING_UNSUPPORTED"
            raise TransportError("CLAPCLIP_SECTION_ENCODING_REJECTED")
        original_read=response.read
        content_range=re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)",response.headers.get("Content-Range",""))
        request=args[0] if args else kwargs.get("req")
        requested=re.fullmatch(r"bytes=(\d+)-(\d*)",getattr(request,"headers",{}).get("Range",""))
        requested_start=int(requested[1]) if requested else 0
        if context.transfer["start"] is None:context.transfer["start"]=requested_start
        status=getattr(response,"status",None)
        if status not in {200,206}:
            response.close();section["failed"]=True
            raise TransportError("CLAPCLIP_SECTION_RESPONSE_REJECTED")
        continuous=(requested_start==context.transfer["start"]+context.transfer["received"] and
                    ((status==206 and content_range and int(content_range[1])==requested_start
                      and int(content_range[2])>=requested_start)
                     or (status==200 and context.transfer["received"]==0 and context.transfer["start"]==0)))
        if not continuous and not context.transfer["fragment"]:
            response.close();section["unsupported_reason"]="SECTION_NATIVE_RESTART_REQUIRED"
            raise TransportError("CLAPCLIP_SECTION_NATIVE_RESTART_REQUIRED")
        span=context.transfer.get("dash_span")
        if span is not None and (status==206 or response.headers.get("Content-Range")):
            expected_start=span[0]+requested_start
            expected_end=min(span[1],span[0]+int(requested[2])) if requested and requested[2] else span[1]
            if (not content_range or int(content_range[1])!=expected_start
                    or int(content_range[2])!=expected_end or content_range[3]=="*"
                    or int(content_range[3])!=section["source_bytes"]):
                response.close();section["failed"]=True
                raise TransportError("CLAPCLIP_SECTION_FRAGMENT_RANGE_REJECTED")
        length=response.headers.get("Content-Length")
        if length is not None and (not re.fullmatch(r"\d{1,20}",length)):
            response.close();section["failed"]=True
            raise TransportError("CLAPCLIP_SECTION_LENGTH_REJECTED")
        if content_range and (int(content_range[2])<int(content_range[1])
                or (length is not None and int(length)!=int(content_range[2])-int(content_range[1])+1)):
            response.close();section["failed"]=True
            raise TransportError("CLAPCLIP_SECTION_LENGTH_REJECTED")
        total=(int(content_range[3]) if content_range and content_range[3]!="*"
               else int(length) if status==200 and length is not None else 0)
        if total and context.transfer["total"] not in {0,total} and not context.transfer["fragment"]:
            response.close();section["failed"]=True
            raise TransportError("CLAPCLIP_SECTION_LENGTH_REJECTED")
        if total:context.transfer["total"]=total
        if section["mode"]=="native_pipe_faststart" and total:section["source_bytes"]=total
        expected=int(length) if length is not None else None
        received=0
        def read(amount=None):
            nonlocal received
            if section["path"].exists():
                section["stopped"]=True
                response.close()
                raise TransportError("CLAPCLIP_SECTION_STOPPED")
            try:data=original_read(amount)
            except (TransportError,OSError):
                if context.transfer["fragment"]:section["fragment_retry_pending"]=True
                else:section["unsupported_reason"]="SECTION_NATIVE_RECOVERY_REQUIRED"
                raise
            section["read_calls"]+=1;section["read_bytes"]+=len(data);received+=len(data)
            context.transfer["received"]+=len(data)
            if expected is not None and received>expected:
                section["failed"]=True
                raise TransportError("CLAPCLIP_SECTION_LENGTH_REJECTED")
            if not data and expected is not None and received<expected:
                if context.transfer["fragment"]:section["fragment_retry_pending"]=True
                else:
                    section["unsupported_reason"]="SECTION_NATIVE_RECOVERY_REQUIRED"
                    raise TransportError("CLAPCLIP_SECTION_NATIVE_RECOVERY_REQUIRED")
            return data
        response.read=read
        return response

    def real_download(self, *args, **kwargs):
        previous = getattr(context, "media", False)
        previous_transfer=getattr(context,"transfer",None)
        context.media = True
        context.transfer={"start":None,"received":0,"total":0,
                          "fragment":SECTION_STATE is not None and SECTION_STATE["mode"]!="native_pipe_faststart","dash_span":None}
        if SECTION_STATE is not None and SECTION_STATE["mode"]=="native_dash_byte_pipe":
            row=args[1] if len(args)>1 else kwargs.get("info_dict",{})
            context.transfer["dash_span"]=SECTION_STATE["dash_spans"].get(row.get("url"))
            if context.transfer["dash_span"] is None:
                SECTION_STATE["failed"]=True
                raise TransportError("CLAPCLIP_SECTION_FRAGMENT_IDENTITY_REJECTED")
        if SECTION_STATE is not None and SECTION_STATE["mode"]!="native_pipe_faststart":
            SECTION_STATE["fragments_started"]+=1
        try:
            return original_download(self, *args, **kwargs)
        finally:
            context.media = previous
            context.transfer=previous_transfer

    def fragment(self,ctx,url,info_dict,headers=None,request_data=None):
        result=original_fragment(self,ctx,url,info_dict,headers,request_data)
        state=SECTION_STATE
        if result and state is not None and state["mode"]=="native_dash_byte_pipe":
            span=state["dash_spans"].get(url)
            expected=span[1]-span[0]+1 if span is not None else None
            path=ctx.get("fragment_filename_sanitized")
            if expected is None or not path or Path(path).stat().st_size!=expected:
                state["failed"]=True
                raise TransportError("CLAPCLIP_SECTION_FRAGMENT_LENGTH_REJECTED")
        if result and state is not None:state["fragment_retry_pending"]=False
        return result

    def fragments(self,ctx,items,info_dict,**kwargs):
        def controlled():
            for item in items:
                state=SECTION_STATE
                if state is not None:
                    if state["path"].exists():
                        state["stopped"]=True
                        break
                    if state["unsupported_reason"] is not None or state["failed"]:break
                yield item
        return original_fragments(self,ctx,controlled(),info_dict,**kwargs)

    if fragment_prefix:
        FragmentFD._download_fragment=fragment
        FragmentFD.download_and_append_fragments=fragments
    YoutubeDL.urlopen = urlopen
    HttpFD.real_download = real_download


def install_guards(port, node_binary):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_popen = subprocess.Popen

    def permitted(address):
        return isinstance(address, tuple) and address[0] == "127.0.0.1" and address[1] == port

    def connect(connection, address):
        if not permitted(address):
            raise OSError("Direct network connections are disabled")
        return original_connect(connection, address)

    def connect_ex(connection, address):
        if not permitted(address):
            raise OSError("Direct network connections are disabled")
        return original_connect_ex(connection, address)

    class GuardedPopen(original_popen):
        def __init__(self, args, *positional, **kwargs):
            if (not isinstance(args, (list, tuple)) or not args or kwargs.get("shell")
                    or not node_binary or Path(args[0]).resolve() != Path(node_binary).resolve()):
                raise OSError("External downloaders and post-processors are disabled")
            super().__init__(args, *positional, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    subprocess.Popen = GuardedPopen
    # Optional native HTTP clients bypass Python socket hooks. Never load them.
    sys.modules["curl_cffi"] = None


if __name__ == "__main__":
    install_guards(int(os.environ["CLAPCLIP_PROXY_PORT"]), os.environ.get("CLAPCLIP_NODE", ""))
    import yt_dlp
    # Only the current controlled ProcessRunner may execute this entry point.
    if os.environ.get("CLAPCLIP_DOWNLOAD_POLICY_VERSION") != "2":
        raise SystemExit("Current download policy marker required")
    install_response_limits(fragment_prefix="--clapclip-stream-stop" in sys.argv[1:])
    if "--clapclip-info-json" in sys.argv[1:]:
        try:
            download_fresh(sys.argv[1:])
        except ValueError:
            raise SystemExit("CLAPCLIP_FRESH_METADATA_INVALID") from None
    else:
        yt_dlp.main()
