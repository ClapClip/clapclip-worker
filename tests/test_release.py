import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('release_check', Path(__file__).resolve().parents[1] / 'scripts/check-release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_deployment_material_is_not_public_source(self):
        for path in ('build/runtime/.env', 'mediaworker/.env.local', 'models/model.onnx',
                     '.ssh/id_ed25519', 'node-token.json', 'work/user.png', 'logs/run.log'):
            with self.subTest(path=path):
                self.assertTrue(release.private_path(path))
        for path in ('.env.example', '.env.local.example', 'mediaworker/models.py',
                     'mediaworker/web/app.js', 'LICENSE'):
            self.assertFalse(release.private_path(path))

    def test_tokens_are_detected_without_being_printed(self):
        token = ('ghp_' + 'x' * 36).encode()
        findings = release.scan('source.py', token)
        self.assertTrue(findings)
        self.assertNotIn(token.decode(), '\n'.join(findings))
        self.assertEqual(release.scan('source.py', b'GITHUB_TOKEN='), [])

    def test_archive_rejects_private_files_and_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('project/.env', 'synthetic password')
                archive.writestr('../escaped.txt', 'synthetic')
                archive.writestr('project/README.md', 'public')
            findings = release.check_archive(path)
        self.assertEqual(len(findings), 2)

    def test_archive_allows_sanitized_examples_and_license(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('project/.env.example', 'GITHUB_TOKEN=')
                archive.writestr('project/LICENSE', 'MIT License')
            self.assertEqual(release.check_archive(path), [])
