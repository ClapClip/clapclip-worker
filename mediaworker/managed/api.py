"""Verified discovery, read-only enrollment checks and authorized model bytes."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import httpx

from .common import ManagedError, inside, write_json
from .credentials import Login

CAPABILITIES = {"faceswap", "cutout", "enhance", "download"}
def configured_origin() -> str:
    value = json.loads(Path(__file__).with_name("bootstrap-service.json").read_text(encoding="utf-8"))
    if (value.get("version") != 1 or not isinstance(value.get("origin"), str)
            or value.get("state") != "reviewed-unified-worker-v1"):
        raise ManagedError("CENTRAL_BOOTSTRAP_CONTRACT_PENDING")
    return value["origin"]


def _caps(value, requested=None) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or item not in CAPABILITIES for item in value):
        raise ManagedError("INVALID_BOOTSTRAP_RESPONSE")
    result = sorted(set(value))
    if requested is not None and not set(result) <= set(requested):
        raise ManagedError("UNEXPECTED_AUTHORIZED_CAPABILITIES")
    return result


@dataclass(frozen=True)
class Bootstrap:
    origin: str
    pool: str
    client: str
    region: str
    models_path: str

    @classmethod
    def validate(cls, origin: str, value: dict):
        target = urlsplit(origin)
        if (target.scheme != "https" or not target.hostname or target.username or target.password
                or target.path not in {"", "/"} or target.query or target.fragment):
            raise ManagedError("INVALID_DISCOVERY_ORIGIN")
        if (not isinstance(value, dict) or set(value) != {"version", "apiOrigin", "cognito", "modelsEndpoint"}
                or type(value["version"]) is not int or value["version"] != 1 or value["apiOrigin"] != origin.rstrip("/")
                or value["modelsEndpoint"] != "/api/worker/models"):
            raise ManagedError("BOOTSTRAP_ORIGIN_OR_SCHEMA_MISMATCH")
        cognito = value["cognito"]
        if not isinstance(cognito, dict) or set(cognito) != {"userPoolId", "clientId", "region"}:
            raise ManagedError("INVALID_COGNITO_DISCOVERY")
        region, pool, client = cognito["region"], cognito["userPoolId"], cognito["clientId"]
        if (not all(isinstance(item, str) for item in (region, pool, client))
                or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", region)
                or not re.fullmatch(re.escape(region) + r"_[A-Za-z0-9]+", pool)
                or not re.fullmatch(r"[A-Za-z0-9]{1,128}", client)):
            raise ManagedError("COGNITO_DISCOVERY_UNAVAILABLE")
        return cls(origin.rstrip("/"), pool, client, region, value["modelsEndpoint"])


@dataclass(frozen=True)
class Asset:
    identifier: str
    capabilities: tuple[str, ...]
    directory: str
    filename: str
    size: int
    digest: str
    path: str

    @classmethod
    def validate(cls, value: dict, allowed: list[str], catalogue: dict):
        keys = {"assetId", "capabilities", "usage", "directory", "filename", "bytes", "sha256", "source", "downloadPath"}
        if not isinstance(value, dict) or set(value) != keys:
            raise ManagedError("INVALID_MODEL_MANIFEST")
        identifier = value["assetId"]
        filename = value["filename"]
        if (not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", identifier)
                or not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.onnx", filename)):
            raise ManagedError("INVALID_MODEL_MANIFEST")
        caps = _caps(value["capabilities"])
        known = catalogue.get(filename)
        if (not caps or not set(caps) & set(allowed) or not known
                or type(value["bytes"]) is not int or value["bytes"] <= 0 or value["bytes"] != known["bytes"]
                or value["sha256"] != known["sha256"] or value["directory"] != known["directory"]
                or value["usage"] != known["usage"] or not isinstance(value["source"], str)
                or value["downloadPath"] != "/api/worker/models/" + identifier):
            raise ManagedError("MODEL_NOT_IN_REVIEWED_CATALOGUE")
        return cls(identifier, tuple(caps), value["directory"], filename, value["bytes"],
                   value["sha256"], value["downloadPath"])


@dataclass(repr=False)
class SetupAPI:
    bootstrap: Bootstrap
    login: Login
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    storage_transport: httpx.BaseTransport | None = field(default=None, repr=False)
    token: str = field(default="", repr=False)

    @staticmethod
    def discover(origin: str, *, transport=None) -> Bootstrap:
        target = urlsplit(origin)
        if (target.scheme != "https" or not target.hostname or target.username or target.password
                or target.path not in {"", "/"} or target.query or target.fragment):
            raise ManagedError("INVALID_DISCOVERY_ORIGIN")
        try:
            with httpx.Client(follow_redirects=False, transport=transport, timeout=30) as client:
                response = client.get(origin.rstrip("/") + "/api/worker/bootstrap")
                if response.status_code != 200 or len(response.content) > 32 * 1024:
                    raise ManagedError("BOOTSTRAP_NOT_DEPLOYED_OR_UNAVAILABLE")
                return Bootstrap.validate(origin.rstrip("/"), response.json())
        except (httpx.HTTPError, ValueError, TypeError):
            raise ManagedError("BOOTSTRAP_NOT_DEPLOYED_OR_UNAVAILABLE") from None

    def authenticate(self) -> None:
        from pycognito import Cognito
        try:
            user = Cognito(user_pool_id=self.bootstrap.pool, client_id=self.bootstrap.client,
                           user_pool_region=self.bootstrap.region, username=self.login.username)
            user.authenticate(password=self.login.password)
            token = user.id_token
            if not token:
                raise ManagedError("PERMANENT_ACCOUNT_PASSWORD_REQUIRED")
            self.token = token
        except ManagedError:
            raise
        except Exception as error:
            response = getattr(error, "response", {})
            code = response.get("Error", {}).get("Code", "") if isinstance(response, dict) else ""
            if code in {"PasswordResetRequiredException", "UserNotConfirmedException"} or "Password" in type(error).__name__:
                raise ManagedError("PERMANENT_ACCOUNT_PASSWORD_REQUIRED") from None
            raise ManagedError("COGNITO_LOGIN_FAILED") from None

    def request(self, method: str, path: str, *, body=None, headers=None) -> httpx.Response:
        if not path.startswith("/api/worker/") or path.startswith("//") or "#" in path:
            raise ManagedError("INVALID_WORKER_API_PATH")
        for attempt in range(2):
            if not self.token:
                self.authenticate()
            try:
                with httpx.Client(base_url=self.bootstrap.origin, follow_redirects=False,
                                  transport=self.transport, timeout=30) as client:
                    response = client.request(method, path, json=body,
                        headers={**(headers or {}), "Authorization": "Bearer " + self.token})
            except httpx.HTTPError:
                raise ManagedError("WORKER_API_UNAVAILABLE") from None
            if response.status_code == 401 and attempt == 0:
                self.authenticate()
                continue
            if response.status_code == 403:
                raise ManagedError("WORKER_ACCOUNT_OR_NODE_NOT_AUTHORIZED")
            if response.status_code >= 400:
                raise ManagedError("WORKER_API_UNAVAILABLE")
            return response
        raise ManagedError("COGNITO_LOGIN_FAILED")

    def preflight(self, node: str, requested: list[str]) -> list[str]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", node):
            raise ManagedError("INVALID_NODE_IDENTITY")
        response = self.request("POST", "/api/worker/preflight", body={"nodeId": node, "capabilities": requested})
        try:
            if len(response.content) > 32 * 1024:
                raise ManagedError("INVALID_PREFLIGHT_RESPONSE")
            value = response.json()
            if not isinstance(value, dict):
                raise ManagedError("INVALID_PREFLIGHT_RESPONSE")
            if (type(value.get("version")) is not int or value.get("version") != 1 or not isinstance(value.get("enrollmentEnabled"), bool)
                    or value.get("nodeRegistrationAllowed") is not True):
                raise ManagedError("WORKER_ACCOUNT_OR_NODE_NOT_AUTHORIZED")
            result = _caps(value["capabilities"], requested)
            if not result:
                raise ManagedError("NO_AUTHORIZED_LOCAL_CAPABILITY")
            return result
        except (ValueError, TypeError, KeyError):
            raise ManagedError("INVALID_PREFLIGHT_RESPONSE") from None

    def account_binding(self) -> str:
        # This is only a local account-change guard AFTER server-verified
        # preflight. It is not a replacement for JWT authorization/verification.
        try:
            payload = self.token.split(".")[1]
            value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            issuer = f"https://cognito-idp.{self.bootstrap.region}.amazonaws.com/{self.bootstrap.pool}"
            if value["iss"] != issuer or value["aud"] != self.bootstrap.client or not isinstance(value["sub"], str):
                raise ValueError
            return hashlib.sha256((issuer + ":" + value["sub"]).encode()).hexdigest()
        except (ValueError, TypeError, KeyError, IndexError):
            raise ManagedError("INVALID_ACCOUNT_BINDING") from None

    def assets(self, allowed: list[str]) -> list[Asset]:
        path = self.bootstrap.models_path + "?capabilities=" + ",".join(allowed)
        response = self.request("GET", path)
        try:
            if len(response.content) > 128 * 1024:
                raise ValueError
            value = response.json()
            if value["version"] != 1 or not isinstance(value["assets"], list) or len(value["assets"]) > 32:
                raise ValueError
            _caps(value["capabilities"], allowed)
            catalogue = json.loads(Path(__file__).with_name("model-catalog.json").read_text())["assets"]
            assets = [Asset.validate(item, allowed, catalogue) for item in value["assets"]]
            names = [(asset.directory, asset.filename) for asset in assets]
            if len(set(names)) != len(names):
                raise ValueError
            return assets
        except (ValueError, TypeError, KeyError):
            raise ManagedError("INVALID_MODEL_MANIFEST") from None

    @staticmethod
    def _storage_url(url: str) -> str:
        try:
            target = urlsplit(url)
            reviewed_host = ((target.hostname == "release-assets.githubusercontent.com" and
                re.fullmatch(r"/github-production-release-asset/[0-9]+/[a-fA-F0-9-]+", target.path)) or
                re.fullmatch(r"(?:[a-z0-9][a-z0-9.-]*\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?", target.hostname or ""))
            valid = (target.scheme == "https" and target.username is None and target.password is None and target.port in {None, 443}
                and not target.fragment
                and reviewed_host)
        except ValueError:
            valid = False
        if not valid:
            raise ManagedError("UNEXPECTED_MODEL_STORAGE_ORIGIN")
        return url

    def download(self, asset: Asset, destination: Path) -> None:
        def digest(path):
            result = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    result.update(chunk)
            return result.hexdigest()
        if destination.is_symlink():
            raise ManagedError("UNSAFE_MODEL_DESTINATION")
        if destination.is_file() and destination.stat().st_size == asset.size and digest(destination) == asset.digest:
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        if partial.is_symlink():
            raise ManagedError("UNSAFE_MODEL_DESTINATION")
        for attempt in range(4):
            offset = partial.stat().st_size if partial.exists() else 0
            if offset >= asset.size:
                if offset == asset.size and digest(partial) == asset.digest:
                    os_replace(partial, destination)
                    return
                partial.unlink()
                offset = 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            response = self.request("GET", asset.path, headers=headers)
            if response.status_code != 302 or not response.headers.get("Location"):
                raise ManagedError("INVALID_MODEL_DOWNLOAD_REDIRECT")
            url = self._storage_url(response.headers["Location"])
            try:
                # Deliberately a NEW client: no queue/Cognito auth, cookies or
                # authenticated connection pool can cross this boundary.
                with httpx.Client(follow_redirects=False, transport=self.storage_transport,
                                  timeout=httpx.Timeout(30, read=600)) as anonymous:
                    with anonymous.stream("GET", url, headers={"Accept-Encoding": "identity", **headers}) as stored:
                        if stored.status_code == 403:
                            continue  # Expired signed URL: reauthorize same asset next attempt.
                        expected = asset.size - offset
                        if (stored.status_code != (206 if offset else 200)
                                or stored.headers.get("Content-Encoding", "identity") not in {"", "identity"}
                                or not re.fullmatch(r"[0-9]+", stored.headers.get("Content-Length", ""))
                                or int(stored.headers.get("Content-Length", "-1")) != expected):
                            raise ManagedError("MODEL_TRANSFER_LENGTH_OR_RANGE_MISMATCH")
                        if offset and stored.headers.get("Content-Range") != f"bytes {offset}-{asset.size-1}/{asset.size}":
                            raise ManagedError("MODEL_TRANSFER_LENGTH_OR_RANGE_MISMATCH")
                        written = offset
                        with partial.open("ab" if offset else "wb") as stream:
                            for chunk in stored.iter_bytes():
                                written += len(chunk)
                                if written > asset.size:
                                    raise ManagedError("MODEL_TRANSFER_TOO_LARGE")
                                stream.write(chunk)
                        if written != asset.size:
                            continue
                if digest(partial) != asset.digest:
                    partial.unlink(missing_ok=True)
                    raise ManagedError("MODEL_INTEGRITY_CHECK_FAILED")
                os_replace(partial, destination)
                return
            except httpx.HTTPError:
                if attempt == 3:
                    raise ManagedError("MODEL_TRANSFER_INTERRUPTED") from None
            if attempt < 3:
                time.sleep(min(4, 0.5 * 2**attempt))
        raise ManagedError("MODEL_TRANSFER_INTERRUPTED")

    def prepare_models(self, base: Path, assets: list[Asset]) -> Path:
        signature = hashlib.sha256(json.dumps(sorted((item.directory, item.filename, item.digest) for item in assets)).encode()).hexdigest()
        root = base / signature
        for asset in assets:
            self.download(asset, inside(root, asset.directory + "/" + asset.filename))
        write_json(root / "verified.json", {"catalogue": signature, "assets": len(assets)})
        return root


def os_replace(source: Path, destination: Path) -> None:
    import os
    os.replace(source, destination)
