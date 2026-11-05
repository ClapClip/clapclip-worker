from __future__ import annotations

from pathlib import Path
import asyncio
from contextlib import asynccontextmanager

import httpx

# The node exchanges bytes with S3 exclusively through presigned URLs handed to
# it by the API, so it never holds AWS credentials.

_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0)
_UPLOAD_TIMEOUT = httpx.Timeout(120.0)
_RETRYABLE_TRANSFER = (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)


def transfer_client() -> httpx.AsyncClient:
    """Anonymous, attempt-local pool; never reuse the authenticated queue client."""
    return httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT,
                             limits=httpx.Limits(max_connections=2, max_keepalive_connections=2,
                                                keepalive_expiry=120))


@asynccontextmanager
async def _client_or_owned(client, timeout):
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=timeout) as owned:
            yield owned


async def download(url: str, dest: Path, max_bytes: int = 6 * 1024 * 1024,
                   *, client: httpx.AsyncClient | None = None) -> Path:
    """Stream a presigned GET URL to a local file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    options = {"timeout": _DOWNLOAD_TIMEOUT} if client is not None else {}
    async with _client_or_owned(client, _DOWNLOAD_TIMEOUT) as http:
        for attempt in range(3):
            complete = False
            try:
                async with http.stream("GET", url, **options) as resp:
                    resp.raise_for_status()
                    expected = int(resp.headers.get("Content-Length", "0"))
                    if expected > max_bytes:
                        raise ValueError("download exceeds byte limit")
                    received = 0
                    # Each retry starts at byte zero, including an existing dest.
                    with dest.open("wb") as fh:
                        async for chunk in resp.aiter_bytes():
                            received += len(chunk)
                            if received > max_bytes:
                                raise ValueError("download exceeds byte limit")
                            fh.write(chunk)
                    # Content-Length counts encoded wire bytes, not decoded data.
                    wire_bytes = (resp.num_bytes_downloaded if resp.headers.get("Content-Encoding", "identity") != "identity"
                                  else received)
                    if expected > 0 and wire_bytes != expected:
                        raise httpx.RemoteProtocolError("incomplete input body")
                complete = True
                return dest
            except _RETRYABLE_TRANSFER:
                if attempt == 2:
                    raise
            finally:
                if not complete:
                    dest.unlink(missing_ok=True)
            await asyncio.sleep(0.5 * 2 ** attempt)


async def upload(put_url: str, src: Path, content_type: str = "application/octet-stream", max_bytes: int | None = 32 * 1024 * 1024,
                 *, client: httpx.AsyncClient | None = None) -> None:
    """PUT a local file to a presigned S3 URL."""
    if src.stat().st_size <= 0 or (max_bytes is not None and src.stat().st_size > max_bytes):
        raise ValueError("result exceeds byte limit")
    size = src.stat().st_size
    async def chunks():
        # httpx accepts an async byte stream. Never hold a whole video in RAM.
        with src.open("rb") as source:
            sent = 0
            while True:
                chunk = source.read(256 * 1024)
                if not chunk:
                    break
                sent += len(chunk)
                if (max_bytes is not None and sent > max_bytes) or sent > size:
                    raise ValueError("result changed during upload")
                yield chunk
            if sent != size:
                raise ValueError("result changed during upload")
    options = {"timeout": _UPLOAD_TIMEOUT} if client is not None else {}
    async with _client_or_owned(client, _UPLOAD_TIMEOUT) as http:
        for attempt in range(3):
            try:
                resp = await http.put(
                    put_url, content=chunks(), headers={"Content-Type": content_type, "Content-Length": str(size)}, **options
                )
                resp.raise_for_status()
                return
            except _RETRYABLE_TRANSFER:
                if attempt == 2:
                    raise
                # An attempt-specific S3 PUT is idempotent even after a lost
                # response. Never retry queue claims or database transactions.
                await asyncio.sleep(0.5 * 2 ** attempt)
