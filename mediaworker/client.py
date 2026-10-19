from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx

from .config import Config
from .redaction import redact
from .models import ClaimedJob, ClaimResponse
from .queues import QueueRoutes
from .single_instance import FileLock, WorkerAlreadyRunning

log = logging.getLogger(__name__)


class LeaseLost(RuntimeError):
    """The server fenced this attempt out; no further result writes are allowed."""


class WorkerClient:
    """Async HTTP client for the Next.js worker API. The only network peer this
    node has (besides presigned S3 URLs). All calls carry the node's bearer
    token; the API resolves the node's tier + capabilities from it."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._auth_lock = asyncio.Lock()
        self._routes = QueueRoutes(cfg.download_api_prefix)
        self._job_routes = self._routes.job_routes
        self._http = httpx.AsyncClient(
            base_url=cfg.api_base_url,
            timeout=httpx.Timeout(cfg.claim_timeout_s),
            headers={"Authorization": f"Bearer {cfg.node_token}"},
        )

    async def __aenter__(self) -> "WorkerClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def _authenticated_post(self, path: str, payload: dict) -> httpx.Response:
        previous = self._http.headers.get("Authorization")
        response = await self._http.post(path, json=payload)
        if response.status_code != 401 or not (self._cfg.cognito_username and self._cfg.cognito_password):
            return response

        # Concurrent requests share one refresh; a newer token must not be
        # replaced just because another request used the expired token.
        async with self._auth_lock:
            if self._http.headers.get("Authorization") == previous:
                await bootstrap_node_token(self._cfg, force=True, previous_token=(previous or "").removeprefix("Bearer "))
                self._http.headers["Authorization"] = f"Bearer {self._cfg.node_token}"
        return await self._http.post(path, json=payload)

    async def _post(self, path: str, payload: dict, retries: int = 3) -> dict:
        delay = 0.5
        for attempt in range(retries):
            try:
                resp = await self._authenticated_post(path, payload)
                resp.raise_for_status()
                return resp.json() if resp.content else {}
            except Exception as exc:
                if isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise
                if attempt == retries - 1:
                    raise
                log.debug("%s failed (attempt %d): %s", path, attempt + 1, exc)
                await asyncio.sleep(delay)
                delay *= 2
        return {}

    def _job_path(self, job_id: str, operation: str) -> str:
        return self._routes.job_path(job_id, operation)

    async def claim(self, capabilities: list[str], models: list[str]) -> ClaimedJob | None:
        path = self._routes.claim_path(capabilities)
        from .downloader.policy import DOWNLOAD_POLICY_VERSIONS
        data = await self._post(
            path,
            {
                "node_id": self._cfg.node_id,
                "capabilities": capabilities,
                "trust_tier": self._cfg.trust_tier,
                "models": models,
                **({"downloadPolicyVersions": list(DOWNLOAD_POLICY_VERSIONS)}
                   if capabilities and set(capabilities) == {"download"} else {}),
            },
            retries=1,  # Claim is not idempotent after an ambiguous response loss.
        )
        job = ClaimResponse.model_validate(data).job
        if job:
            self._routes.bind(job)
        return job

    async def complete(self, job_id: str, lease_token: str, result_key: str, width: int, height: int, *, duration: float | None = None) -> None:
        response = await self._post(
            self._job_path(job_id, "complete"),
            {
                "node_id": self._cfg.node_id,
                "job_id": job_id,
                "lease_token": lease_token,
                "result_key": result_key,
                "w": width,
                "h": height,
                **({"duration": duration} if duration is not None and job_id in self._job_routes else {}),
            },
        )
        if response.get("stale"):
            raise LeaseLost("completion rejected: lease is stale")
        self._job_routes.pop(job_id, None)

    async def fail(self, job_id: str, lease_token: str, error: str) -> None:
        await self._post(
            self._job_path(job_id, "fail"),
            {"node_id": self._cfg.node_id, "job_id": job_id, "lease_token": lease_token, "error": redact(error)[:500]},
        )
        self._job_routes.pop(job_id, None)

    async def heartbeat(self, job_id: str, lease_token: str, progress: int | None = None) -> None:
        # Best-effort; a missed beat just risks an early requeue, not correctness.
        response = await self._post(
            self._job_path(job_id, "heartbeat"),
            {"node_id": self._cfg.node_id, "job_id": job_id, "lease_token": lease_token,
             **({"progress": progress} if progress is not None else {})},
            retries=1,
        )
        if response.get("stale"):
            raise LeaseLost("heartbeat rejected: lease is stale")


def _cognito_id_token(cfg: Config) -> str:
    from pycognito import Cognito

    user = Cognito(
        user_pool_id=cfg.cognito_user_pool_id,
        client_id=cfg.cognito_client_id,
        user_pool_region=cfg.cognito_region,
        username=cfg.cognito_username,
    )
    user.authenticate(password=cfg.cognito_password)
    if not user.id_token:
        raise RuntimeError("Cognito login returned no ID token")
    return user.id_token


def _read_shared_token(cfg: Config) -> tuple[str, float] | None:
    path = cfg.node_token_file
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        raise RuntimeError("shared Worker token cache is unreadable; inspect it before restarting the node") from None
    if not isinstance(payload, dict):
        raise RuntimeError("shared Worker token cache is invalid; inspect it before restarting the node")
    if payload.get("node_id") != cfg.node_id or payload.get("api_base_url") != cfg.api_base_url:
        raise RuntimeError("shared Worker token belongs to a different node or API origin; align both runtimes before starting")
    token = payload.get("token")
    expires_at = payload.get("expires_at")
    if not isinstance(token, str) or not token or not isinstance(expires_at, (int, float)):
        raise RuntimeError("shared Worker token cache is invalid; inspect it before restarting the node")
    return token, float(expires_at)


def _write_shared_token(cfg: Config, token: str, expires_at: float) -> None:
    path = cfg.node_token_file
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"node_id": cfg.node_id, "api_base_url": cfg.api_base_url,
                       "token": token, "expires_at": expires_at}, handle)
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


async def _acquire_token_lock(path: Path) -> FileLock:
    lock = FileLock(path)
    deadline = time.monotonic() + 30
    while True:
        try:
            return lock.acquire()
        except WorkerAlreadyRunning:
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for the shared Worker token refresh") from None
            await asyncio.sleep(0.05)


async def bootstrap_node_token(
    cfg: Config, force: bool = False, previous_token: str | None = None
) -> str:
    """Use a legacy node token or log in through Cognito and exchange the ID
    token for a fresh per-node token. The Cognito password is never sent to the
    worker API; only a verified Cognito ID token is used for the exchange."""
    if cfg.node_token and not force:
        return cfg.node_token
    if not (cfg.cognito_username and cfg.cognito_password):
        raise RuntimeError("set NODE_TOKEN or WORKER_COGNITO_USERNAME/PASSWORD")
    lock = await _acquire_token_lock(cfg.node_token_file.with_suffix(".lock"))
    try:
        cached = _read_shared_token(cfg)
        if cached:
            token, expires_at = cached
            token_is_fresh = expires_at > time.time() + 60
            if token_is_fresh and (not force or token != previous_token):
                cfg.node_token = token
                log.info("reused shared worker token for node %s", cfg.node_id)
                return token

        id_token = await asyncio.to_thread(_cognito_id_token, cfg)
        async with httpx.AsyncClient(
            base_url=cfg.api_base_url,
            timeout=httpx.Timeout(cfg.claim_timeout_s),
        ) as client:
            response = await client.post(
                "/api/worker/login",
                json={
                    "idToken": id_token,
                    "nodeId": cfg.node_id,
                    "capabilities": cfg.capabilities,
                },
            )
            response.raise_for_status()
            payload = response.json()
        cfg.node_token = str(payload["nodeToken"])
        cfg.node_id = str(payload["nodeId"])
        cfg.trust_tier = str(payload["tier"])
        cfg.capabilities = [str(item) for item in payload["capabilities"]]
        expires_at = time.time() + max(60, int(payload.get("expiresIn", 3600)))
        _write_shared_token(cfg, cfg.node_token, expires_at)
        log.info("authenticated shared worker node %s", cfg.node_id)
        return cfg.node_token
    finally:
        lock.release()
