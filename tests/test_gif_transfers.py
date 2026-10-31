"""Anonymous per-attempt transfer pooling, concurrency and cleanup contracts."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from mediaworker import lifecycle, storage
from mediaworker.config import Config
from mediaworker.engine.base import RunResult
from mediaworker.models import ClaimedJob

JOB = 'a86eeb49-a664-4249-bfb7-20c6001be743'
LEASE = 'd8c5b4a4-feb2-490a-a98d-f3965c91a47a'


class GifTransferTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cfg = Config.from_env(load_env=False)
        self.cfg.work_dir = Path(temporary.name)
        self.cfg.max_image_bytes = 16
        self.job = ClaimedJob(id=JOB,lease_token=LEASE,attempt=1,task_type='faceswap',
            params={'mode':'gif','targetMedia':'gif'},
            inputs={'target':'https://input-a.test/target','source':'https://input-b.test/source'},
            result={'put_url':'https://output.test/result','key':'result.gif'})
        self.api = SimpleNamespace(heartbeat=AsyncMock(),complete=AsyncMock(),fail=AsyncMock())
        def run(ctx):
            output = ctx.workdir/'result.gif'
            output.write_bytes(b'generated-gif')
            return RunResult(output,4,3,'image/gif')
        self.engine = SimpleNamespace(run=Mock(side_effect=run),result_byte_limit=lambda _p,default:default,
                                     failure_report=lambda e,_p:SimpleNamespace(message=str(e),include_traceback=False,phase='input',diagnostic_code='INPUT'))

    async def test_two_inputs_overlap_and_authenticated_queue_client_is_not_reused(self):
        started, both = [], asyncio.Event()
        requests, order = [], []
        async def respond(request):
            requests.append(request)
            self.assertNotIn('Authorization',request.headers)
            self.assertNotIn('Cookie',request.headers)
            if request.method == 'GET':
                started.append(request.url.host)
                if len(started)==2:
                    both.set()
                await asyncio.wait_for(both.wait(),1)
                self.assertEqual(request.extensions['timeout']['read'],60)
                return httpx.Response(200,content=b'input',headers={'Set-Cookie':'source=private; Domain=input-a.test'})
            order.append('upload')
            self.assertEqual(request.headers['Content-Type'],'image/gif')
            self.assertEqual(request.extensions['timeout']['read'],120)
            self.assertEqual(await request.aread(),b'generated-gif')
            return httpx.Response(200)
        async def heartbeat(*_):
            order.append('lease')
        async def complete(*_):
            order.append('complete')
        self.api.heartbeat.side_effect=heartbeat
        self.api.complete.side_effect=complete
        transfer=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(storage,'transfer_client',return_value=transfer) as factory:
            await lifecycle._process_job(self.cfg,self.api,{'faceswap':self.engine},self.job)
        self.assertEqual(len(requests),3)
        self.assertEqual(order,['lease','upload','complete'])
        self.assertTrue(transfer.is_closed)
        factory.assert_called_once()
        self.api.fail.assert_not_awaited()
        self.assertFalse((self.cfg.work_dir/JOB).exists())

    async def test_input_failure_cancels_sibling_before_cleanup(self):
        reading, closed=asyncio.Event(),asyncio.Event()
        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                reading.set()
                yield b'first'
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        async def respond(request):
            if request.url.path=='/source':
                return httpx.Response(200,stream=WaitingStream())
            await reading.wait()
            return httpx.Response(200,content=b'x',headers={'Content-Length':'17'})
        transfer=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(storage,'transfer_client',return_value=transfer):
            await lifecycle._process_job(self.cfg,self.api,{'faceswap':self.engine},self.job)
        self.assertTrue(closed.is_set())
        self.assertTrue(transfer.is_closed)
        self.engine.run.assert_not_called()
        self.api.complete.assert_not_awaited()
        self.assertIn('byte limit',self.api.fail.await_args.args[2])
        self.assertFalse((self.cfg.work_dir/JOB).exists())

    async def test_cancellation_waits_for_both_input_requests(self):
        entered, cancelled=[],[]
        both=asyncio.Event()
        async def respond(request):
            entered.append(request.url.path)
            if len(entered)==2:
                both.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(request.url.path)
        transfer=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(storage,'transfer_client',return_value=transfer):
            task=asyncio.create_task(lifecycle._process_job(self.cfg,self.api,{'faceswap':self.engine},self.job))
            await asyncio.wait_for(both.wait(),1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertCountEqual(cancelled,['/target','/source'])
        self.assertTrue(transfer.is_closed)
        self.assertFalse((self.cfg.work_dir/JOB).exists())
        self.engine.run.assert_not_called()
        self.api.complete.assert_not_awaited()

    async def test_each_attempt_has_a_fresh_anonymous_cookie_jar(self):
        clients=[]
        async def respond(request):
            if request.method=='GET':
                self.assertNotIn('Cookie',request.headers)
                return httpx.Response(200,content=b'input',headers={'Set-Cookie':'previous=private; Domain=input-a.test'})
            return httpx.Response(200)
        def factory():
            client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
            clients.append(client)
            return client
        with patch.object(storage,'transfer_client',side_effect=factory):
            for _ in range(2):
                await lifecycle._process_job(self.cfg,self.api,{'faceswap':self.engine},self.job)
        self.assertEqual(len(clients),2)
        self.assertTrue(all(c.is_closed for c in clients))
        self.assertEqual(self.api.complete.await_count,2)

    async def test_pooled_download_still_enforces_streaming_byte_limit(self):
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'12345678'
                yield b'123456789'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r:httpx.Response(200,stream=Stream()))) as client:
            with self.assertRaisesRegex(ValueError,'byte limit'):
                await storage.download('https://input.test/data',self.cfg.work_dir/'bounded',16,client=client)
            self.assertFalse(client.is_closed,'the attempt owns the supplied pool')

    async def test_observation_fault_after_complete_cannot_report_failure(self):
        async def complete(*_):
            (self.cfg.work_dir/JOB/LEASE/'result.gif').unlink()
        self.api.complete.side_effect=complete
        transfer=httpx.AsyncClient(transport=httpx.MockTransport(lambda _r:httpx.Response(200,content=b'input')))
        with patch.object(storage,'transfer_client',return_value=transfer), \
                patch.object(lifecycle.log,'info',side_effect=OSError('offline log unavailable')):
            await lifecycle._process_job(self.cfg,self.api,{'faceswap':self.engine},self.job)
        self.api.complete.assert_awaited_once()
        self.api.fail.assert_not_awaited()
        self.assertTrue(transfer.is_closed)
