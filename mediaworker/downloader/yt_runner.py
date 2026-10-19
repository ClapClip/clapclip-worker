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
    yt_dlp.main()
