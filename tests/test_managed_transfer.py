"""Anonymous signed-storage transport with generated six-byte assets only."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from mediaworker.managed.api import Asset, Bootstrap, SetupAPI
from mediaworker.managed.credentials import Login
from mediaworker.managed.common import ManagedError


class ModelTransferTests(unittest.TestCase):
    def api(self, storage, authorization=None, *, storage_url="https://fixture.s3.us-east-1.amazonaws.com/model?synthetic=opaque"):
        self.api_calls, self.storage_calls = [], []
        def authorized(request):
            self.api_calls.append(request)
            self.assertEqual(request.headers["Authorization"], "Bearer synthetic-id-token")
            return (authorization(request) if authorization else httpx.Response(302, headers={
                "Location": storage_url,
                "Set-Cookie": "private=must-never-reach-storage"}))
        def anonymous(request):
            self.storage_calls.append(request)
            self.assertNotIn("Authorization", request.headers)
            self.assertNotIn("Cookie", request.headers)
            return storage(request)
        return SetupAPI(Bootstrap("https://offline.test", "us-east-1_fixture", "fixture", "us-east-1", "/api/worker/models"),
            Login("fixture", "synthetic-password"), token="synthetic-id-token",
            transport=httpx.MockTransport(authorized), storage_transport=httpx.MockTransport(anonymous))

    def asset(self):
        return Asset("synthetic", ("cutout",), "cutout", "synthetic.onnx", 6,
                     hashlib.sha256(b"abcdef").hexdigest(), "/api/worker/models/synthetic")

    def test_expired_url_reauthorizes_and_resumes_exact_range_without_auth_leak(self):
        attempts = 0
        def storage(request):
            nonlocal attempts
            attempts += 1
            self.assertEqual(request.headers["Range"], "bytes=2-")
            return httpx.Response(403) if attempts == 1 else httpx.Response(206,
                headers={"Content-Length": "4", "Content-Range": "bytes 2-5/6"}, content=b"cdef")
        api = self.api(storage)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.onnx"
            path.with_suffix(".onnx.part").write_bytes(b"ab")
            api.download(self.asset(), path)
            self.assertEqual(path.read_bytes(), b"abcdef")
        self.assertEqual(len(self.api_calls), 2)
        self.assertEqual(len(self.storage_calls), 2)

    def test_hash_mismatch_does_not_publish_model_or_keep_corrupt_partial(self):
        api = self.api(lambda request: httpx.Response(200, content=b"broken"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.onnx"
            with self.assertRaisesRegex(ManagedError, "MODEL_INTEGRITY_CHECK_FAILED"):
                api.download(self.asset(), path)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_suffix(".onnx.part").exists())

    def test_wrong_content_range_is_not_appended(self):
        api = self.api(lambda request: httpx.Response(206, content=b"cdef",
            headers={"Content-Length": "4", "Content-Range": "bytes 1-4/6"}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.onnx"
            partial = path.with_suffix(".onnx.part")
            partial.write_bytes(b"ab")
            with self.assertRaises(ManagedError):
                api.download(self.asset(), path)
            self.assertEqual(partial.read_bytes(), b"ab")

    def test_unreviewed_redirect_is_rejected_before_anonymous_request(self):
        api = self.api(lambda request: self.fail("unreviewed storage accessed"),
            authorization=lambda request: httpx.Response(302, headers={"Location": "https://unreviewed.test/model"}))
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ManagedError):
            api.download(self.asset(), Path(directory) / "synthetic.onnx")
        self.assertEqual(self.storage_calls, [])

    def test_github_release_cdn_uses_anonymous_client_and_provider_opaque_signature(self):
        url = "https://release-assets.githubusercontent.com/github-production-release-asset/1/abcdef-0123?sp=r&sv=fixture&sig=synthetic"
        api = self.api(lambda request: httpx.Response(200, content=b"abcdef"), storage_url=url)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.onnx"
            api.download(self.asset(), path)
            self.assertEqual(path.read_bytes(), b"abcdef")
        self.assertEqual(len(self.storage_calls), 1)
        self.assertEqual(str(self.storage_calls[0].url), url)
        self.assertEqual(self.api_calls[0].url.host, "offline.test")

    def test_github_expired_link_reauthorizes_exact_resume_and_rejects_wrong_hash_or_range(self):
        url = "https://release-assets.githubusercontent.com/github-production-release-asset/1/abcdef-0123?sig=synthetic"
        for failure in (None, "hash", "range"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                attempts = 0
                def storage(request):
                    nonlocal attempts
                    attempts += 1
                    self.assertEqual(request.headers["Range"], "bytes=2-")
                    if attempts == 1:
                        return httpx.Response(403)
                    return httpx.Response(206, content=b"xxxx" if failure == "hash" else b"cdef",
                        headers={"Content-Length": "4", "Content-Range": "bytes 1-4/6" if failure == "range" else "bytes 2-5/6"})
                api = self.api(storage, storage_url=url)
                path = Path(directory) / "synthetic.onnx"
                path.with_suffix(".onnx.part").write_bytes(b"ab")
                if failure:
                    with self.assertRaises(ManagedError):
                        api.download(self.asset(), path)
                    self.assertFalse(path.exists())
                else:
                    api.download(self.asset(), path)
                    self.assertEqual(path.read_bytes(), b"abcdef")
                self.assertEqual(len(self.api_calls), 2)

    def test_github_only_exact_reviewed_host_https_443_and_no_userinfo(self):
        for url in (
            "http://release-assets.githubusercontent.com/model",
            "https://release-assets.githubusercontent.com:444/model",
            "https://@release-assets.githubusercontent.com/model",
            "https://user:secret@release-assets.githubusercontent.com/model",
            "https://release-assets.githubusercontent.com.evil.test/model",
            "https://sub.release-assets.githubusercontent.com/model",
            "https://objects.githubusercontent.com/model",
            "https://api.github.com/repos/fixture/assets/1",
            "https://github.com/fixture/releases/download/v1/model",
            "https://release-assets.githubusercontent.com/model#fragment",
            "https://release-assets.githubusercontent.com/model",
            "https://release-assets.githubusercontent.com/github-production-release-asset/not-numeric/abcdef",
            "https://release-assets.githubusercontent.com/github-production-release-asset/1/not-hex",
            "https://release-assets.githubusercontent.com/github-production-release-asset/1/abcdef/extra",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ManagedError, "UNEXPECTED_MODEL_STORAGE_ORIGIN"):
                SetupAPI._storage_url(url)

    def test_github_storage_second_redirect_is_not_followed(self):
        api = self.api(lambda request: httpx.Response(302, headers={"Location": "https://unreviewed.test/model"}),
                       storage_url="https://release-assets.githubusercontent.com/github-production-release-asset/1/abcdef-0123?sig=synthetic")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ManagedError):
            api.download(self.asset(), Path(directory) / "synthetic.onnx")
        self.assertEqual(len(self.storage_calls), 1)

    def test_wrong_discovery_origin_stops_without_request(self):
        transport = httpx.MockTransport(lambda request: self.fail("invalid discovery origin accessed"))
        with self.assertRaises(ManagedError):
            SetupAPI.discover("https://user:synthetic-password@offline.test", transport=transport)


if __name__ == "__main__":
    unittest.main()
