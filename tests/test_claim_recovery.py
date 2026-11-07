from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from mediaworker import scheduling as runner
from mediaworker.client import WorkerClient
from mediaworker.config import Config


class ClaimRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = Config.from_env(load_env=False)
        runtime = tempfile.TemporaryDirectory()
        self.addCleanup(runtime.cleanup)
        self.cfg.runtime_dir = Path(runtime.name)
        self.cfg.api_base_url = "https://offline.test"
        self.cfg.node_token = "offline-token"
        self.cfg.cognito_username = ""
        self.cfg.cognito_password = ""
        self.cfg.poll_interval_ms = 2000

    async def test_ambiguous_transport_failure_is_never_replayed(self):
        for error_type in (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError):
            with self.subTest(error=error_type.__name__):
                calls = 0

                def handler(request):
                    nonlocal calls
                    calls += 1
                    raise error_type("", request=request)

                async with WorkerClient(self.cfg) as client:
                    await client._http.aclose()
                    client._http = httpx.AsyncClient(
                        base_url=self.cfg.api_base_url,
                        transport=httpx.MockTransport(handler),
                    )
                    with self.assertRaises(error_type):
                        await client.claim(["faceswap"], ["hyperswap_256_b"])
                self.assertEqual(calls, 1)

    async def test_later_poll_recovers_without_replaying_failed_claim(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.RemoteProtocolError("", request=request)
            return httpx.Response(200, json={"job": None})

        async with WorkerClient(self.cfg) as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(
                base_url=self.cfg.api_base_url,
                transport=httpx.MockTransport(handler),
            )
            with self.assertRaises(httpx.RemoteProtocolError):
                await client.claim(["faceswap"], ["hyperswap_256_b"])
            self.assertEqual(calls, 1)
            self.assertIsNone(await client.claim(["faceswap"], ["hyperswap_256_b"]))
        self.assertEqual(calls, 2)

    async def test_observation_failure_never_discards_an_accepted_claim(self):
        payload = {"job": {"id": "a86eeb49-a664-4249-bfb7-20c6001be743",
                   "lease_token": "d8c5b4a4-feb2-490a-a98d-f3965c91a47a", "attempt": 1,
                   "task_type": "cutout", "inputs": {},
                   "result": {"put_url": "https://offline.test/put", "key": "synthetic/result.png"}}}
        async with WorkerClient(self.cfg) as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url=self.cfg.api_base_url,
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
            def broken_observer(_):
                raise OSError("synthetic-secret-that-must-not-be-logged")
            client.poll_observer = broken_observer
            with self.assertLogs("mediaworker.client", level="WARNING") as logs:
                claimed = await client.claim(["cutout"], ["synthetic"])
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, payload["job"]["id"])
            self.assertNotIn("synthetic-secret", "\n".join(logs.output))

    async def test_drain_finishes_accepted_job_without_another_claim(self):
        shutdown = asyncio.Event()
        entered, finished = asyncio.Event(), asyncio.Event()
        sentinel = object()
        client = SimpleNamespace(claim=AsyncMock(return_value=sentinel))
        async def process(*_):
            entered.set()
            await finished.wait()
        registry = {"cutout": SimpleNamespace(available_models=["synthetic"])}
        with patch.object(runner, "_process_job", side_effect=process):
            task = asyncio.create_task(runner._worker_loop(0, self.cfg, client, registry, shutdown))
            await entered.wait()
            shutdown.set()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            finished.set()
            await asyncio.wait_for(task, 1)
        client.claim.assert_awaited_once()

    def test_backoff_is_exponential_bounded_and_jittered(self):
        with patch.object(runner.random, "uniform", return_value=1.0):
            self.assertEqual(
                [runner._claim_retry_delay(2.0, failures) for failures in range(1, 8)],
                [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0],
            )
            self.assertEqual(runner._claim_retry_delay(2.0, 10000), 30.0)
        with patch.object(runner.random, "uniform", return_value=0.8):
            self.assertEqual(runner._claim_retry_delay(2.0, 1), 1.6)
            self.assertEqual(runner._claim_retry_delay(2.0, 10), 24.0)

    async def test_worker_logs_recovery_and_resets_backoff(self):
        shutdown = asyncio.Event()
        outcomes = [httpx.ReadTimeout(""), httpx.ReadTimeout(""), None,
                    httpx.RemoteProtocolError("secret-token"), None]
        calls = 0

        async def claim(*_):
            nonlocal calls
            result = outcomes[calls]
            calls += 1
            if calls == len(outcomes):
                shutdown.set()
            if isinstance(result, Exception):
                raise result
            return result

        client = SimpleNamespace(claim=AsyncMock(side_effect=claim))
        registry = {"faceswap": SimpleNamespace(available_models=["hyperswap_256_b"])}
        with (
            patch.object(runner.random, "uniform", return_value=1.0),
            patch.object(runner, "_sleep_or_stop", new_callable=AsyncMock) as sleep,
            self.assertLogs("mediaworker.scheduling", level="INFO") as logs,
        ):
            await runner._worker_loop(0, self.cfg, client, registry, shutdown)
        self.assertEqual([call.args[1] for call in sleep.await_args_list], [2.0, 4.0, 2.0, 2.0, 2.0])
        self.assertEqual(calls, 5)
        output = "\n".join(logs.output)
        self.assertIn("ReadTimeout", output)
        self.assertIn("RemoteProtocolError", output)
        self.assertIn("claim connection recovered", output)
        self.assertIn("endpoint=offline.test; failures=2", output)
        self.assertNotIn("secret-token", output)
        client.claim.assert_awaited_with(["faceswap"], ["hyperswap_256_b"])

    async def test_shutdown_interrupts_outage_backoff(self):
        shutdown = asyncio.Event()
        sleeping = asyncio.create_task(runner._sleep_or_stop(shutdown, 30.0))
        await asyncio.sleep(0)
        shutdown.set()
        await asyncio.wait_for(sleeping, timeout=0.5)

    async def test_cancelled_worker_does_not_claim_again(self):
        shutdown = asyncio.Event()
        client = SimpleNamespace(claim=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await runner._worker_loop(0, self.cfg, client, {}, shutdown)
        client.claim.assert_awaited_once()
        slots = []
        try:
            for _ in range(3):
                slot = runner.acquire_worker_slot(self.cfg.runtime_dir)
                self.assertIsNotNone(slot)
                slots.append(slot)
        finally:
            for slot in slots:
                slot.release()

    def test_http_failure_summary_has_status_without_credentials(self):
        request = httpx.Request("POST", "https://user:secret@offline.test/claim?token=secret")
        response = httpx.Response(503, request=request)
        error = httpx.HTTPStatusError("sensitive response secret", request=request, response=response)
        self.assertEqual(runner._claim_error_summary(error), "HTTPStatusError HTTP 503")
        self.assertEqual(runner._claim_error_summary(httpx.ReadTimeout("")), "ReadTimeout")


if __name__ == "__main__":
    unittest.main()
