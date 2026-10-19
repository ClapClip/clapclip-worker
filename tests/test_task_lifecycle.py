"""Offline protocol checks shared by image, metadata and media engines."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from mediaworker import lifecycle, scheduling, storage
from mediaworker.client import WorkerClient
from mediaworker.config import Config
from mediaworker.engine.base import Engine, FailureReport, RunResult
from mediaworker.engine.download_engine import DownloadEngine
from mediaworker.models import ClaimedJob
from mediaworker.queues import QueueRoutes, lease_heartbeat_interval

JOB_ID = "a86eeb49-a664-4249-bfb7-20c6001be743"
LEASE = "d8c5b4a4-feb2-490a-a98d-f3965c91a47a"


class FixtureEngine(Engine):
    def __init__(self, task_type, content_type):
        self.task_type = task_type
        self.content_type = content_type

    def run(self, ctx):
        output = ctx.workdir / "result"
        output.write_bytes(b"result")
        return RunResult(output, 4, 3, self.content_type)

    def result_byte_limit(self, params, default):
        return 123


class LifecycleContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cfg = Config.from_env(load_env=False)
        self.cfg.work_dir = Path(temporary.name) / "work"
        self.cfg.runtime_dir = Path(temporary.name) / "runtime"
        self.cfg.api_base_url = "https://offline.test"
        self.cfg.node_token = "offline"

    async def test_all_engine_results_use_the_same_fenced_lifecycle(self):
        cases = [("faceswap", {}, "image/jpeg"), ("cutout", {}, "image/png"),
                 ("enhance", {}, "image/png"),
                 ("download", {"operation": "info"}, "application/json"),
                 ("download", {"operation": "download"}, "video/mp4")]
        for task_type, params, content_type in cases:
            with self.subTest(task=task_type, params=params):
                calls = []
                claimed = ClaimedJob(id=JOB_ID, lease_token=LEASE, attempt=2,
                    task_type=task_type, params=params, inputs={},
                    result={"put_url": "https://offline.test/put", "key": "attempt/result"})

                def respond(request):
                    calls.append((request.url.path, json.loads(request.content)))
                    body = {"job": claimed.model_dump()} if request.url.path.endswith('/claim') else {}
                    return httpx.Response(200, json=body)

                async with WorkerClient(self.cfg) as client:
                    await client._http.aclose()
                    client._http = httpx.AsyncClient(base_url=self.cfg.api_base_url,
                        transport=httpx.MockTransport(respond))
                    job = await client.claim([task_type], [])
                    with patch.object(storage, "upload", new_callable=AsyncMock) as upload:
                        await lifecycle._process_job(self.cfg, client,
                            {task_type: FixtureEngine(task_type, content_type)}, job)
                    self.assertEqual(upload.await_args.args[2:], (content_type, 123))
                    self.assertFalse(upload.await_args.args[1].exists())
                prefix = '/api/worker/download' if task_type == 'download' else '/api/worker'
                self.assertEqual([path for path, _ in calls],
                    [f'{prefix}/claim', f'{prefix}/heartbeat', f'{prefix}/complete'])
                self.assertEqual(calls[-1][1]['lease_token'], LEASE)
                self.assertEqual(calls[-1][1]['result_key'], 'attempt/result')
                self.assertEqual((calls[-1][1]['w'], calls[-1][1]['h']), (4, 3))
                self.assertFalse((self.cfg.work_dir / JOB_ID).exists())

    async def test_failure_contract_does_not_depend_on_the_task_name(self):
        class RedactingEngine(FixtureEngine):
            def run(self, ctx):
                raise RuntimeError('private-url-with-secret')

            def failure_report(self, error, params):
                return FailureReport('SOURCE_FAILURE', False, 'process', 'SOURCE_FAILURE')

        claimed = ClaimedJob(id=JOB_ID, lease_token=LEASE, attempt=1, task_type='custom',
            result={'put_url': 'https://offline.test/put', 'key': 'attempt/result'})
        client = type('Client', (), {'heartbeat': AsyncMock(), 'complete': AsyncMock(), 'fail': AsyncMock()})()
        with self.assertLogs('mediaworker.lifecycle', level='ERROR') as logs:
            await lifecycle._process_job(self.cfg, client,
                {'custom': RedactingEngine('custom', 'application/json')}, claimed)
        self.assertNotIn('private-url', '\n'.join(logs.output))
        client.fail.assert_awaited_once_with(JOB_ID, LEASE, 'SOURCE_FAILURE')
        client.complete.assert_not_awaited()
        self.assertFalse((self.cfg.work_dir / JOB_ID).exists())

    async def test_missing_download_engine_keeps_the_public_error_contract(self):
        claimed = ClaimedJob(id=JOB_ID, lease_token=LEASE, attempt=1, task_type='download',
            result={'put_url': 'https://offline.test/put', 'key': 'attempt/result'})
        client = type('Client', (), {'heartbeat': AsyncMock(), 'complete': AsyncMock(), 'fail': AsyncMock()})()
        with self.assertLogs('mediaworker.lifecycle', level='ERROR') as logs:
            await lifecycle._process_job(self.cfg, client, {}, claimed)
        client.fail.assert_awaited_once_with(JOB_ID, LEASE, 'MEDIA_UNAVAILABLE')
        self.assertNotIn('Traceback', '\n'.join(logs.output))
        self.assertFalse((self.cfg.work_dir / JOB_ID).exists())

    async def test_slot_is_released_after_an_unexpected_attempt_error(self):
        claimed = ClaimedJob(id=JOB_ID, lease_token=LEASE, attempt=1, task_type='faceswap',
            result={'put_url': 'https://offline.test/put', 'key': 'attempt/result'})
        shutdown = asyncio.Event()
        client = type('Client', (), {'claim': AsyncMock(return_value=claimed)})()

        async def fail_attempt(*args):
            shutdown.set()
            raise RuntimeError('offline fixture')

        with patch.object(scheduling, '_process_job', side_effect=fail_attempt), \
             self.assertLogs('mediaworker.scheduling', level='ERROR'):
            await scheduling._worker_loop(0, self.cfg, client,
                {'faceswap': FixtureEngine('faceswap', 'image/jpeg')}, shutdown)
        held = []
        try:
            for _ in range(3):
                slot = scheduling.acquire_worker_slot(self.cfg.runtime_dir)
                self.assertIsNotNone(slot)
                held.append(slot)
            self.assertIsNone(scheduling.acquire_worker_slot(self.cfg.runtime_dir))
        finally:
            for slot in held:
                slot.release()

    def test_download_group_is_one_even_with_three_configured_workers(self):
        self.cfg.claim_capabilities = ['download']
        self.cfg.concurrency = 3
        self.assertEqual(scheduling.runtime_capability_groups(self.cfg, ['download']), [['download']])
        self.cfg.claim_capabilities = ['faceswap', 'download']
        with self.assertRaises(SystemExit):
            scheduling.runtime_capability_groups(self.cfg, ['faceswap', 'download'])

    def test_download_failure_policy_keeps_public_and_diagnostic_codes_separate(self):
        engine = DownloadEngine(self.cfg)
        error = RuntimeError('MEDIA_UNAVAILABLE')
        error.phase = 'info'
        error.diagnostic_code = 'HTTP_FORBIDDEN'
        failure = engine.failure_report(error, {'operation': 'info'})
        self.assertEqual(failure, FailureReport('MEDIA_UNAVAILABLE', False, 'info', 'HTTP_FORBIDDEN'))
        error = RuntimeError('https://private.test/signed?token=secret')
        failure = engine.failure_report(error, {'operation': 'https://secret'})
        self.assertEqual(failure, FailureReport('MEDIA_UNAVAILABLE', False, 'process', 'UNKNOWN_FAILURE'))

    def test_custom_download_prefix_and_heartbeat_cadence_are_preserved(self):
        routes = QueueRoutes('/api/worker/media')
        self.assertEqual(routes.claim_path(['download']), '/api/worker/media/claim')
        self.assertEqual(routes.claim_path(['cutout', 'enhance']), '/api/worker/claim')
        job = ClaimedJob(id=JOB_ID, lease_token=LEASE, attempt=1, task_type='download',
            result={'put_url': 'https://offline.test/put', 'key': 'attempt/result'})
        routes.bind(job)
        self.assertEqual(routes.job_path(JOB_ID, 'complete'), '/api/worker/media/complete')
        self.assertEqual(lease_heartbeat_interval('download', 120), 3)
        self.assertEqual(lease_heartbeat_interval('faceswap', 120), 30)
        self.assertEqual(lease_heartbeat_interval('cutout', 3), 1)
        with self.assertRaises(ValueError):
            QueueRoutes('/api/worker/../download')
