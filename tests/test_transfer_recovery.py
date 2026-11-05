"""Offline transfer faults: retry budgets, partial bodies and cancellation."""
import asyncio
import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from mediaworker import storage


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks, self.error, self.closed = chunks, error, False
    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error
    async def aclose(self):
        self.closed = True


class TransferRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = Path(tmp.name)/"input"

    async def test_interrupted_body_restarts_at_zero_and_closes_both_streams(self):
        self.dest.write_bytes(b"previous-long-file")
        streams = [Stream([b"partial"], httpx.RemoteProtocolError("offline")), Stream([b"ok"])]
        calls = []
        def respond(request):
            calls.append(request)
            self.assertNotIn("Range", request.headers)
            return httpx.Response(200, stream=streams[len(calls)-1])
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(storage.asyncio, "sleep", new_callable=AsyncMock):
                await storage.download("https://offline.test/input", self.dest, client=client)
            self.assertFalse(client.is_closed)
        self.assertEqual(self.dest.read_bytes(), b"ok")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(s.closed for s in streams))

    async def test_silent_short_body_uses_three_attempt_budget_and_removes_partial(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, headers={"Content-Length":"8"}, stream=Stream([b"short"]))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(storage.asyncio, "sleep", new_callable=AsyncMock), self.assertRaises(httpx.RemoteProtocolError):
                await storage.download("https://offline.test/input", self.dest, client=client)
        self.assertEqual(len(calls), 3)
        self.assertFalse(self.dest.exists())

    async def test_encoded_content_length_counts_wire_bytes_but_limit_counts_decoded(self):
        data = gzip.compress(b"decoded"*10)
        def respond(request):
            return httpx.Response(200, headers={"Content-Encoding":"gzip", "Content-Length":str(len(data))}, stream=Stream([data]))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await storage.download("https://offline.test/input", self.dest, 70, client=client)
            self.assertEqual(self.dest.read_bytes(), b"decoded"*10)
            with self.assertRaisesRegex(ValueError, "byte limit"):
                await storage.download("https://offline.test/input", self.dest, 60, client=client)
        self.assertFalse(self.dest.exists())

    async def test_http_denial_stream_limit_and_invalid_length_are_not_retried(self):
        for status, headers, chunks, error in ((403, {}, [b"denied"], httpx.HTTPStatusError),
                (200, {}, [b"123", b"456"], ValueError),
                (200, {"Content-Length":"999"}, [], ValueError),
                (200, {"Content-Length":"invalid"}, [], ValueError)):
            with self.subTest(status=status, headers=headers):
                calls = []
                def respond(request):
                    calls.append(request)
                    return httpx.Response(status, headers=headers, stream=Stream(chunks))
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    with self.assertRaises(error):
                        await storage.download("https://offline.test/input", self.dest, 5, client=client)
                self.assertEqual(len(calls), 1)
                self.assertFalse(self.dest.exists())

    async def test_cancel_during_backoff_does_not_start_another_request(self):
        sleeping = asyncio.Event()
        calls = []
        async def sleep(_):
            sleeping.set()
            await asyncio.Event().wait()
        def respond(request):
            calls.append(request)
            return httpx.Response(200, stream=Stream([b"partial"], httpx.ReadError("offline")))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(storage.asyncio, "sleep", side_effect=sleep):
                task = asyncio.create_task(storage.download("https://offline.test/input", self.dest, client=client))
                await asyncio.wait_for(sleeping.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.dest.exists())

    async def test_put_protocol_disconnect_reopens_body_and_obeys_budget(self):
        self.dest.write_bytes(b"full result")
        for recover in (True, False):
            bodies = []
            async def respond(request):
                bodies.append(await request.aread())
                if not recover or len(bodies) < 2:
                    raise httpx.RemoteProtocolError("offline")
                return httpx.Response(200)
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with patch.object(storage.asyncio, "sleep", new_callable=AsyncMock):
                    if recover:
                        await storage.upload("https://offline.test/output", self.dest, client=client)
                    else:
                        with self.assertRaises(httpx.RemoteProtocolError):
                            await storage.upload("https://offline.test/output", self.dest, client=client)
            self.assertEqual(bodies, [b"full result"]*(2 if recover else 3))
