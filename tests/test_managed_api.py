"""Offline wire boundaries: malformed responses never escape or echo secrets."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import unittest

import httpx

from mediaworker.managed.api import Asset, Bootstrap, SetupAPI
from mediaworker.managed.common import ManagedError
from mediaworker.managed.credentials import Login


class ManagedAPIWireTests(unittest.TestCase):
    def api(self, value):
        requests = []

        def respond(request):
            requests.append(request)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/api/worker/preflight")
            return httpx.Response(200, json=value)

        api = SetupAPI(
            Bootstrap("https://offline.test", "us-east-1_fixture", "fixture", "us-east-1", "/api/worker/models"),
            Login("fixture-user", "fixture-password"),
            transport=httpx.MockTransport(respond), token="fixture-id-token",
        )
        return api, requests

    def test_non_object_preflight_stops_with_reviewed_error_without_output(self):
        for value in (None, [], ["fixture-password"], "fixture-password", 1, True):
            with self.subTest(value_type=type(value).__name__):
                api, requests = self.api(value)
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    with self.assertRaises(ManagedError) as caught:
                        api.preflight("fixture-node", ["cutout"])
                self.assertEqual(str(caught.exception), "INVALID_PREFLIGHT_RESPONSE")
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(len(requests), 1)

    def test_existing_owned_node_can_pass_when_enrollment_disabled(self):
        api, _ = self.api({"version": 1, "enrollmentEnabled": False,
                           "nodeRegistrationAllowed": True, "capabilities": ["cutout"]})
        self.assertEqual(api.preflight("fixture-node", ["cutout", "download"]), ["cutout"])

    def asset(self):
        catalogue = json.loads(Path(__file__).parents[1].joinpath(
            "mediaworker/managed/model-catalog.json").read_text(encoding="utf-8"))["assets"]
        filename, known = next(iter(catalogue.items()))
        value = {**known, "filename": filename, "assetId": "fixture-model",
                 "capabilities": ["faceswap"], "source": "fixture-only",
                 "downloadPath": "/api/worker/models/fixture-model"}
        return value, catalogue

    def test_reviewed_positive_integer_model_size_is_accepted(self):
        value, catalogue = self.asset()
        asset = Asset.validate(value, ["faceswap"], catalogue)
        self.assertEqual(asset.size, value["bytes"])
        self.assertIs(type(asset.size), int)

    def test_model_size_rejects_same_value_float_and_other_non_integers(self):
        value, catalogue = self.asset()
        for size in (float(value["bytes"]), True, False, str(value["bytes"]), None, [], {}):
            with self.subTest(size_type=type(size).__name__):
                with self.assertRaises(ManagedError) as caught:
                    Asset.validate({**value, "bytes": size}, ["faceswap"], catalogue)
                self.assertEqual(str(caught.exception), "MODEL_NOT_IN_REVIEWED_CATALOGUE")

    def test_model_size_requires_positive_integer_even_for_invalid_catalogue(self):
        value, catalogue = self.asset()
        for size in (0, -1):
            invalid_catalogue = {value["filename"]: {**catalogue[value["filename"]], "bytes": size}}
            with self.subTest(size=size), self.assertRaises(ManagedError):
                Asset.validate({**value, "bytes": size}, ["faceswap"], invalid_catalogue)


if __name__ == "__main__":
    unittest.main()
