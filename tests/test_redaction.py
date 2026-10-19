import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from mediaworker.client import WorkerClient
from mediaworker.config import Config
from mediaworker import model_registry
from mediaworker.redaction import RedactingFormatter, redact


class RedactionTests(unittest.TestCase):
    def test_urls_credentials_and_encoded_queries_are_removed(self):
        sensitive = [
            'https://bucket.example/private-photo.png?X-Amz-Signature=signature-secret',
            'https://user:password-secret@example.test/private#fragment-secret',
            'Bearer bearer-secret', 'Basic basic-secret',
            'WORKER_COGNITO_PASSWORD="password-secret with spaces"',
            "{'nodeToken': 'node-secret', 'lease_token': 'lease-secret'}",
            'X-Amz-Credential=credential-secret%2Faws4_request',
            'AWS_SECRET_ACCESS_KEY=aws-secret',
            'GITHUB_TOKEN=github-secret',
        ]
        for message in sensitive:
            with self.subTest(message=message):
                clean = redact(message)
                self.assertIn('[redacted', clean)
                self.assertNotIn('-secret', clean)
                self.assertNotIn('private-photo', clean)
                self.assertNotIn('fragment-secret', clean)
        self.assertEqual(redact('MEDIA_UNAVAILABLE'), 'MEDIA_UNAVAILABLE')
        self.assertEqual(redact('model checksum mismatch: model.onnx'),
                         'model checksum mismatch: model.onnx')

    def test_formatter_redacts_arguments_and_entire_chained_traceback(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(RedactingFormatter('%(levelname)s %(message)s'))
        logger = logging.Logger('redaction-test')
        logger.addHandler(handler)
        try:
            try:
                raise ValueError('https://example.test/private?X-Amz-Signature=query-secret')
            except ValueError as error:
                raise RuntimeError('WORKER_COGNITO_PASSWORD=password-secret') from error
        except RuntimeError:
            logger.exception('request failed %s', 'Bearer bearer-secret')
        rendered = output.getvalue()
        self.assertNotIn('-secret', rendered)
        self.assertNotIn('https://', rendered)
        self.assertIn('RuntimeError', rendered)
        self.assertIn('ValueError', rendered)

    def test_public_config_has_no_production_auth_or_repository_defaults(self):
        with patch.dict(os.environ, {}, clear=True), patch("mediaworker.config.Path.home", return_value=Path("C:/worker-test-home")):
            cfg = Config.from_env(load_env=False)
        self.assertEqual(cfg.cognito_user_pool_id, '')
        self.assertEqual(cfg.cognito_client_id, '')
        self.assertEqual(cfg.faceswap_model_repository, '')
        cfg.api_base_url = 'https://worker.example.test'
        cfg.cognito_username = 'worker@example.test'
        cfg.cognito_password = 'offline-password'
        with self.assertRaisesRegex(SystemExit, 'WORKER_COGNITO_USER_POOL_ID'):
            cfg.require_api()
        cfg.node_token = 'offline-token'
        cfg.require_api()  # Token authentication does not require Cognito IDs.
        cfg.github_token = 'github-secret'
        for secret in ['offline-token', 'offline-password', 'github-secret']:
            self.assertNotIn(secret, repr(cfg))

    def test_missing_model_repository_fails_before_any_network_request(self):
        with patch.dict(os.environ, {}, clear=True), patch("mediaworker.config.Path.home", return_value=Path("C:/worker-test-home")), tempfile.TemporaryDirectory() as directory:
            cfg = Config.from_env(load_env=False)
            cfg.faceswap_models_dir = Path(directory)
            with patch.object(model_registry, '_matches', return_value=False), \
                 patch.object(model_registry.httpx, 'Client') as client:
                with self.assertRaisesRegex(RuntimeError, 'FACESWAP_MODEL_REPOSITORY'):
                    model_registry.sync_faceswap_models(cfg)
                client.assert_not_called()
            with patch.object(model_registry, '_matches', return_value=True), \
                 patch.object(model_registry.httpx, 'Client') as client:
                model_registry.sync_faceswap_models(cfg)
                client.assert_not_called()  # Verified local models still work.


class PublicFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_payload_redacts_before_truncation_and_preserves_lease(self):
        with patch.dict(os.environ, {}, clear=True), patch("mediaworker.config.Path.home", return_value=Path("C:/worker-test-home")):
            cfg = Config.from_env(load_env=False)
        cfg.api_base_url = 'https://worker.example.test'
        cfg.node_token = 'offline-token'
        received = []

        def handle(request):
            received.append(json.loads(request.content))
            return httpx.Response(200, json={})

        client = WorkerClient(cfg)
        await client._http.aclose()
        client._http = httpx.AsyncClient(base_url=cfg.api_base_url,
                                        transport=httpx.MockTransport(handle))
        async with client:
            await client.fail('job', 'lease-must-remain',
                              'HTTP 403 https://bucket.test/user-photo?' +
                              'X-Amz-Signature=' + 's' * 700 + ' Bearer bearer-secret')
        self.assertEqual(received[0]['lease_token'], 'lease-must-remain')
        self.assertEqual(received[0]['error'],
                         'HTTP 403 [redacted-url] Bearer [redacted]')


if __name__ == '__main__':
    unittest.main()
