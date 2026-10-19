from __future__ import annotations

from pathlib import Path
import asyncio

import httpx

# The node exchanges bytes with S3 exclusively through presigned URLs handed to
# it by the API, so it never holds AWS credentials.

_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0)
_UPLOAD_TIMEOUT = httpx.Timeout(120.0)


async def download(url: str, dest: Path, max_bytes: int = 6 * 1024 * 1024) -> Path:
    """Stream a presigned GET URL to a local file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            if int(resp.headers.get("Content-Length", "0")) > max_bytes:
                raise ValueError("download exceeds byte limit")
            received = 0
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise ValueError("download exceeds byte limit")
                    fh.write(chunk)
    return dest


async def upload(put_url: str, src: Path, content_type: str = "application/octet-stream", max_bytes: int | None = 32 * 1024 * 1024) -> None:
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
    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
        for attempt in range(3):
            try:
                resp = await client.put(
                    put_url, content=chunks(), headers={"Content-Type": content_type, "Content-Length": str(size)}
                )
                resp.raise_for_status()
                return
            except (httpx.NetworkError, httpx.TimeoutException):
                if attempt == 2:
                    raise
                # An attempt-specific S3 PUT is idempotent even after a lost
                # response. Never retry queue claims or database transactions.
                await asyncio.sleep(0.5 * 2 ** attempt)
