"""Authenticated loopback HTTP/CONNECT proxy with DNS pinning and bounded sockets."""
import base64
from collections import Counter
from contextlib import contextmanager, suppress
import hmac
import json
import logging
import select
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .safety import EGRESS_REASON_STAGES, ServiceError, cloudflare_addresses, connect_public

log = logging.getLogger(__name__)


class ProxyDiagnostics:
    """One runner invocation's observations, never a terminal failure cause."""
    def __init__(self, phase, sequence):
        self.phase, self.sequence = phase, sequence
        self._counts = Counter()
        self._lock = threading.Lock()

    def record(self, reason):
        if not isinstance(reason, str) or reason not in EGRESS_REASON_STAGES:
            return
        with self._lock:
            self._counts[reason] = min(1000, self._counts[reason] + 1)
            count = self._counts[reason]
        if count != 1:
            return
        with suppress(Exception):
            log.info("download egress phase=%s run=%d stage=%s reason=%s count=%d",
                     self.phase, self.sequence, EGRESS_REASON_STAGES[reason], reason, count)

    def snapshot(self):
        with self._lock:
            return tuple({"stage": EGRESS_REASON_STAGES[reason], "reason": reason, "count": count}
                         for reason, count in sorted(self._counts.items()))

    def finish(self, outcome):
        with suppress(Exception):
            log.info("download egress_scope phase=%s run=%d outcome=%s observed_rejections=%s count_ceiling=1000",
                     self.phase, self.sequence, outcome, json.dumps(self.snapshot(), separators=(",", ":")))


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, max_connections=64):
        self.slots = threading.BoundedSemaphore(max_connections)
        self.diagnostics_lock = threading.Lock()
        self.diagnostics_sequence = 0
        self.diagnostics_current = None
        self.request_diagnostics = {}
        self.request_context = threading.local()
        super().__init__(address, handler)

    def diagnostic_receipt(self):
        with self.diagnostics_lock:
            return self.diagnostics_current

    def request_receipt(self):
        return getattr(self.request_context, "receipt", None)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        with self.diagnostics_lock:
            self.request_diagnostics[id(request)] = self.diagnostics_current
        try:
            super().process_request(request, client_address)
        except Exception:
            with self.diagnostics_lock:
                self.request_diagnostics.pop(id(request), None)
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        with self.diagnostics_lock:
            self.request_context.receipt = self.request_diagnostics.pop(id(request), None)
        try:
            super().process_request_thread(request, client_address)
        finally:
            del self.request_context.receipt
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # URL-bearing exceptions must not reach logs.


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    timeout = 20

    def log_message(self, *_):
        pass

    def _authorized(self):
        actual = self.headers.get("Proxy-Authorization", "")
        return hmac.compare_digest(actual.encode("utf-8"), self.server.authorization.encode("utf-8"))

    def _reject_observation(self, receipt, error, fallback):
        if receipt is None:
            return
        with suppress(Exception):
            reason = getattr(error, "egress_reason", fallback)
            receipt.record(reason if isinstance(reason, str) and reason in EGRESS_REASON_STAGES else fallback)

    def _relay(self, remote):
        total = 0
        byte_limit = self.server.tunnel_bytes
        deadline = time.monotonic() + self.server.tunnel_seconds
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.connection, remote], [], [], 20)
            if not readable:
                return
            for source in readable:
                data = source.recv(64 * 1024)
                if not data:
                    return
                total += len(data)
                if byte_limit is not None and total > byte_limit:
                    return
                (remote if source is self.connection else self.connection).sendall(data)

    def do_CONNECT(self):
        receipt = self.server.request_receipt()
        if not self._authorized():
            self._reject_observation(receipt, None, "AUTH_FAILED")
            self.send_error(407)
            return
        remote = None
        reason = "CONNECT_TARGET_INVALID"
        try:
            parsed = urlsplit("https://" + self.path)
            if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError()
            host, port = parsed.hostname or "", parsed.port or 443
            reason = "CONNECT_FAILED"
            remote = self.server.dial(host, port)
            self.send_response(200, "Connection established")
            self.end_headers()
            self.wfile.flush()
            self._relay(remote)
        except (OSError, ValueError, ServiceError) as error:
            if remote is None:
                self._reject_observation(receipt, error, reason)
                self.send_error(403, "Destination unavailable")
        finally:
            if remote:
                remote.close()
            self.close_connection = True

    def do_GET(self):
        receipt = self.server.request_receipt()
        if not self._authorized():
            self._reject_observation(receipt, None, "AUTH_FAILED")
            self.send_error(407)
            return
        remote = None
        reason = "HTTP_TARGET_INVALID"
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme != "http" or parsed.username or parsed.password or parsed.fragment:
                raise ValueError()
            reason = "REQUEST_BODY_INVALID"
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                raise ValueError()
            reason = "HTTP_TARGET_INVALID"
            host, port = parsed.hostname or "", parsed.port or 80
            reason = "CONNECT_FAILED"
            remote = self.server.dial(host, port)
            path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
            lines = [f"{self.command} {path} HTTP/1.0", f"Host: {parsed.netloc}", "Connection: close"]
            excluded = {"host", "connection", "proxy-authorization", "proxy-connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer"}
            excluded.update(item.strip().lower() for item in self.headers.get("Connection", "").split(","))
            for key, value in self.headers.items():
                if key.lower() not in excluded and "\r" not in value and "\n" not in value:
                    lines.append(f"{key}: {value}")
            remote.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            if length:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError()
                remote.sendall(body)
            total = 0
            byte_limit = self.server.tunnel_bytes
            deadline = time.monotonic() + self.server.tunnel_seconds
            while time.monotonic() < deadline:
                data = remote.recv(64 * 1024)
                if not data:
                    break
                total += len(data)
                if byte_limit is not None and total > byte_limit:
                    break
                self.wfile.write(data)
        except (OSError, ValueError, ServiceError) as error:
            if remote is None:
                self._reject_observation(receipt, error, reason)
                self.send_error(403, "Destination unavailable")
        finally:
            if remote:
                remote.close()
            self.close_connection = True

    do_POST = do_GET
    do_HEAD = do_GET


class EgressProxy:
    def __init__(self, timeout, public_dns=False):
        from .policy import EXTRACTOR_OUTPUT_BYTES
        self.server = BoundedServer(("127.0.0.1", 0), ProxyHandler)
        token = secrets.token_hex(24)
        self.server.authorization = "Basic " + base64.b64encode(f"worker:{token}".encode()).decode()
        # Start in bounded inspection mode. Only media transfer explicitly
        # removes the byte cap; public-IP pinning, time and socket limits stay.
        self.server.tunnel_bytes = EXTRACTOR_OUTPUT_BYTES + 16 * 1024 * 1024
        self.server.tunnel_seconds = timeout
        self.server.dial = (lambda host, port: connect_public(host, port, cloudflare_addresses)) if public_dns else connect_public
        self.port = self.server.server_port
        self.url = f"http://worker:{token}@127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @contextmanager
    def diagnostics(self, phase):
        if phase not in {"info", "download_source"}:
            raise ValueError("INVALID_EGRESS_DIAGNOSTIC_PHASE")
        with self.server.diagnostics_lock:
            previous = self.server.diagnostics_current
            self.server.diagnostics_sequence += 1
            receipt = ProxyDiagnostics(phase, self.server.diagnostics_sequence)
            self.server.diagnostics_current = receipt
        try:
            yield receipt
        except BaseException:
            receipt.finish("raised")
            raise
        else:
            receipt.finish("returned")
        finally:
            with self.server.diagnostics_lock:
                if self.server.diagnostics_current is receipt:
                    self.server.diagnostics_current = previous

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def allow_media(self):
        self.server.tunnel_bytes = None
