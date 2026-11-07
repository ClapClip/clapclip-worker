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
    if (not isinstance(template, str) or Path(template).resolve().parent != Path.cwd().resolve()
            or Path(template).name not in {"source-0.media", "source-1.media"}):
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

    with FreshYoutubeDL(options) as ydl:
        ydl.process_ie_result(info, download=True)
        if ydl._download_retcode:
            raise DownloadError("CLAPCLIP_FRESH_DOWNLOAD_FAILED")



def install_response_limits(max_metadata_bytes=8 * 1024 * 1024):
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
        try:
            response = original_open(self, *args, **kwargs)
        except HTTPError as error:
            # Extractors may parse JSON/HTML error bodies as metadata too.
            limit(error.response)
            raise
        return response if getattr(context, "media", False) else limit(response)

    def real_download(self, *args, **kwargs):
        previous = getattr(context, "media", False)
        context.media = True
        try:
            return original_download(self, *args, **kwargs)
        finally:
            context.media = previous

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
    install_response_limits()
    if "--clapclip-info-json" in sys.argv[1:]:
        try:
            download_fresh(sys.argv[1:])
        except ValueError:
            raise SystemExit("CLAPCLIP_FRESH_METADATA_INVALID") from None
    else:
        yt_dlp.main()
