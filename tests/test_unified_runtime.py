import asyncio
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mediaworker import __main__, runner, scheduling, supervisor
from mediaworker.config import Config


class UnifiedRuntimeTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch('mediaworker.config.Path.home', return_value=Path('C:/worker-test-home')):
            self.cfg = Config.from_env(load_env=False)
        self.cfg.api_base_url = 'https://offline.test'
        self.cfg.capabilities = ['faceswap', 'cutout', 'enhance', 'download']
        self.cfg.claim_capabilities = ['faceswap', 'cutout', 'enhance']
        self.cfg.concurrency = 3

    def test_retired_commands_fail_before_loading_credentials_or_engines(self):
        for command in (['download-worker'], ['download-server'], ['selftest']):
            with self.subTest(command=command), \
                 patch('sys.argv', ['mediaworker', *command]), \
                 patch.object(__main__.Config, 'from_env') as configure, \
                 patch('sys.stderr'):
                with self.assertRaises(SystemExit) as error:
                    __main__.main()
                self.assertEqual(error.exception.code, 2)
                configure.assert_not_called()

    def test_local_command_does_not_load_private_configuration(self):
        with patch('sys.argv', ['mediaworker', 'local', '--port', '8766']), \
             patch.object(__main__.Config, 'from_env') as configure, \
             patch('mediaworker.local_web.run') as local:
            __main__.main()
        configure.assert_not_called()
        self.assertEqual(local.call_args.args[0].port, 8766)

    def test_unified_cli_clears_old_ai_only_filter_and_requires_all_engines(self):
        run = AsyncMock()
        with patch('sys.argv', ['mediaworker', 'run', '--all-capabilities']), \
             patch.object(__main__.Config, 'from_env', return_value=self.cfg), \
             patch.object(__main__, '_configure_logging'), \
             patch.object(runner, 'run_forever', run):
            __main__.main()
        self.assertIsNone(self.cfg.claim_capabilities)
        run.assert_awaited_once_with(self.cfg, require_all_engines=True)

    def test_missing_engine_fails_before_node_authentication_or_claim(self):
        self.cfg.claim_capabilities = None
        with patch.object(Config, 'require_api'), \
             patch('mediaworker.model_registry.sync_models', return_value=self.cfg.capabilities), \
             patch.object(runner, 'build_registry', return_value={'faceswap': Mock(), 'cutout': Mock(), 'enhance': Mock()}), \
             patch.object(runner, 'bootstrap_node_token', new_callable=AsyncMock) as authenticate, \
             patch('mediaworker.engine.registry._mock_enabled', return_value=False):
            with self.assertRaisesRegex(SystemExit, 'required engines unavailable: download'):
                asyncio.run(runner.run_forever(self.cfg, require_all_engines=True))
        authenticate.assert_not_awaited()

    def test_mixed_pollers_execute_at_most_three_jobs_and_one_download(self):
        self.cfg.claim_capabilities = None
        groups = scheduling.runtime_capability_groups(self.cfg, self.cfg.capabilities)

        async def exercise():
            shutdown = asyncio.Event()
            active = peak = active_download = peak_download = completed = 0
            issued = {'image': 0, 'download': 0}

            async def claim(capabilities, models):
                kind = 'download' if capabilities == ['download'] else 'image'
                limit = 1 if kind == 'download' else 3
                if issued[kind] >= limit:
                    return None
                issued[kind] += 1
                return SimpleNamespace(kind=kind)

            async def process(cfg, client, registry, job):
                nonlocal active, peak, active_download, peak_download, completed
                active += 1
                active_download += job.kind == 'download'
                peak = max(peak, active)
                peak_download = max(peak_download, active_download)
                await asyncio.sleep(.08)
                active -= 1
                active_download -= job.kind == 'download'
                completed += 1
                if completed == 4:
                    shutdown.set()

            client = SimpleNamespace(claim=claim)
            registry = {name: SimpleNamespace(available_models=[]) for name in self.cfg.capabilities}
            with tempfile.TemporaryDirectory() as temporary:
                self.cfg.runtime_dir = Path(temporary)
                with patch.object(scheduling, '_process_job', side_effect=process):
                    workers = [asyncio.create_task(scheduling._worker_loop(
                        index, self.cfg, client, registry, shutdown, group))
                        for index, group in enumerate(groups)]
                    try:
                        await asyncio.wait_for(asyncio.gather(*workers), 3)
                    finally:
                        shutdown.set()
                        await asyncio.gather(*workers, return_exceptions=True)
            self.assertEqual((completed, peak, peak_download), (4, 3, 1))

        asyncio.run(exercise())

    def test_supervisor_forwards_all_capabilities_to_one_child(self):
        stop = threading.Event()
        child = Mock(pid=10)
        child.poll.return_value = None
        with patch.object(supervisor, 'snapshot', return_value=[]), \
             patch.object(supervisor.subprocess, 'Popen', return_value=child) as start, \
             patch.object(stop, 'wait', side_effect=lambda _: stop.set() or True):
            supervisor.supervise('run', Path('python.exe'), Path('shell.exe'), Path('.'), stop,
                                 all_capabilities=True)
        self.assertEqual(start.call_args.args[0],
                         ['python.exe', '-m', 'mediaworker', 'run', '--all-capabilities'])
        self.assertEqual(start.call_count, 1)

    def test_dedicated_download_mode_cannot_enable_all_capabilities(self):
        with self.assertRaisesRegex(ValueError, 'only unified run mode'):
            supervisor.supervise('download-worker', Path('python.exe'), Path('shell.exe'),
                                 Path('.'), threading.Event(), all_capabilities=True)


if __name__ == '__main__':
    unittest.main()
