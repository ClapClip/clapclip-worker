from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAX_CONCURRENCY = 3


def _split(value: str | None) -> list[str]:
    return [s.strip() for s in (value or "").split(",") if s.strip()]


def _path(value: str | None, default: Path) -> Path:
    return Path(os.path.expanduser(value)).resolve() if value else default


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def default_onnx_providers() -> list[str]:
    """Preferred ONNX Runtime execution providers for this OS, in priority
    order. They are filtered down to what's actually installed at engine load
    time. Legacy engines may fall back to CPU (engine.base.resolve_providers);
    enhance filters this list to DirectML/CUDA and requires GPU inference."""
    if sys.platform == "darwin":
        return ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    if sys.platform.startswith("win"):
        return ["DmlExecutionProvider", "CPUExecutionProvider"]
    return ["CUDAExecutionProvider", "CPUExecutionProvider"]


@dataclass
class Config:
    # Connection to the worker API.
    api_base_url: str
    node_id: str
    node_token: str = field(repr=False)
    cognito_username: str
    cognito_password: str = field(repr=False)
    cognito_user_pool_id: str
    cognito_client_id: str
    cognito_region: str
    # Capability + trust advertised to the API on every claim.
    capabilities: list[str]
    # Optional per-process claim filter. Registration still uses the full node
    # capability set so separate runtimes can share one node identity/token.
    claim_capabilities: list[str] | None
    trust_tier: str
    # Runtime.
    concurrency: int
    poll_interval_ms: int
    claim_timeout_s: float
    auto_update: bool
    auto_update_interval_s: float
    auto_update_remote: str
    auto_update_branch: str
    # Engine.
    onnx_providers: list[str]
    models_dir: Path
    faceswap_models_dir: Path
    cutout_models_dir: Path
    enhance_models_dir: Path
    enhance_models: list[str]
    enhance_face_models: list[str]
    cutout_model: str
    cutout_model_auto_download: bool
    faceswap_model_auto_download: bool
    faceswap_model_repository: str
    faceswap_model_release: str
    github_token: str = field(repr=False)
    work_dir: Path
    max_image_bytes: int = 6 * 1024 * 1024
    max_image_pixels: int = 16_000_000
    max_image_dimension: int = 8192
    max_output_pixels: int = 32_000_000
    max_result_bytes: int = 32 * 1024 * 1024
    node_token_file: Path = Path.home() / ".mediaworker" / "node-token.json"
    runtime_dir: Path = Path.home() / ".mediaworker"
    download_api_prefix: str = "/api/worker/download"

    @classmethod
    def from_env(cls, load_env: bool = True) -> "Config":
        if load_env and os.environ.get("MEDIAWORKER_LOAD_DOTENV", "1") != "0":
            from dotenv import load_dotenv

            load_dotenv()
        home = Path.home() / ".mediaworker"
        return cls(
            api_base_url=os.environ.get("API_BASE_URL", "").rstrip("/"),
            node_id=os.environ.get("NODE_ID") or socket.gethostname(),
            node_token=os.environ.get("NODE_TOKEN", ""),
            cognito_username=os.environ.get("WORKER_COGNITO_USERNAME", ""),
            cognito_password=os.environ.get("WORKER_COGNITO_PASSWORD", ""),
            cognito_user_pool_id=os.environ.get(
                "WORKER_COGNITO_USER_POOL_ID", ""
            ),
            cognito_client_id=os.environ.get(
                "WORKER_COGNITO_CLIENT_ID", ""
            ),
            cognito_region=os.environ.get("WORKER_COGNITO_REGION", "ap-northeast-1"),
            capabilities=_split(os.environ.get("CAPABILITIES"))
            or ["faceswap", "cutout", "enhance", "download"],
            claim_capabilities=_split(os.environ.get("WORKER_CLAIM_CAPABILITIES")) or None,
            trust_tier=os.environ.get("TRUST_TIER", "trusted"),
            concurrency=min(
                MAX_CONCURRENCY,
                max(1, int(os.environ.get("CONCURRENCY", "1"))),
            ),
            poll_interval_ms=max(100, int(os.environ.get("POLL_INTERVAL_MS", "2000"))),
            claim_timeout_s=max(1.0, float(os.environ.get("CLAIM_TIMEOUT_S", "30"))),
            auto_update=_bool(os.environ.get("WORKER_AUTO_UPDATE"), False),
            auto_update_interval_s=max(
                30.0, float(os.environ.get("WORKER_AUTO_UPDATE_INTERVAL_S", "300"))
            ),
            auto_update_remote=os.environ.get("WORKER_AUTO_UPDATE_REMOTE", "origin").strip()
            or "origin",
            auto_update_branch=os.environ.get("WORKER_AUTO_UPDATE_BRANCH", "main").strip()
            or "main",
            onnx_providers=_split(os.environ.get("ONNX_PROVIDERS"))
            or default_onnx_providers(),
            models_dir=_path(os.environ.get("MODELS_DIR"), home / "models"),
            faceswap_models_dir=_path(
                os.environ.get("FACESWAP_MODELS_DIR"),
                _path(os.environ.get("MODELS_DIR"), home / "models"),
            ),
            cutout_models_dir=_path(
                os.environ.get("CUTOUT_MODELS_DIR"),
                _path(os.environ.get("MODELS_DIR"), home / "models") / "cutout",
            ),
            enhance_models_dir=_path(
                os.environ.get("ENHANCE_MODELS_DIR"), home / "models" / "enhance"
            ),
            enhance_models=_split(
                os.environ.get("ENHANCE_MODELS", "standard,fast,deblur,animation")
            ),
            enhance_face_models=_split(
                os.environ.get("ENHANCE_FACE_MODELS", "gfpgan,gpen")
            ),
            cutout_model=os.environ.get(
                "CUTOUT_MODEL", "isnet-general-use"
            ).strip()
            or "isnet-general-use",
            cutout_model_auto_download=_bool(
                os.environ.get("CUTOUT_MODEL_AUTO_DOWNLOAD"), True
            ),
            faceswap_model_auto_download=_bool(
                os.environ.get("FACESWAP_MODEL_AUTO_DOWNLOAD"), True
            ),
            faceswap_model_repository=os.environ.get(
                "FACESWAP_MODEL_REPOSITORY", ""
            ).strip(),
            faceswap_model_release=os.environ.get(
                "FACESWAP_MODEL_RELEASE", "faceswap-models-v1.0.0"
            ).strip(),
            github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
            work_dir=_path(os.environ.get("WORK_DIR"), home / "work"),
            max_image_bytes=max(1, int(os.environ.get("MAX_IMAGE_BYTES", str(6 * 1024 * 1024)))),
            max_image_pixels=max(1, int(os.environ.get("MAX_IMAGE_PIXELS", "16000000"))),
            max_image_dimension=max(1, int(os.environ.get("MAX_IMAGE_DIMENSION", "8192"))),
            max_output_pixels=max(1, int(os.environ.get("MAX_OUTPUT_PIXELS", "32000000"))),
            max_result_bytes=max(1, int(os.environ.get("MAX_RESULT_BYTES", str(32 * 1024 * 1024)))),
            node_token_file=_path(os.environ.get("NODE_TOKEN_FILE"), home / "node-token.json"),
            runtime_dir=_path(os.environ.get("NODE_RUNTIME_DIR"), home),
            download_api_prefix=os.environ.get("WORKER_DOWNLOAD_API_PREFIX", "/api/worker/download").rstrip("/"),
        )

    @property
    def poll_interval_s(self) -> float:
        return self.poll_interval_ms / 1000.0

    def require_api(self) -> None:
        """Fail fast if the fields needed to reach the API are missing."""
        missing: list[str] = []
        if not self.api_base_url:
            missing.append("API_BASE_URL")
        if not self.node_token and not (
            self.cognito_username and self.cognito_password
        ):
            missing.append(
                "NODE_TOKEN or WORKER_COGNITO_USERNAME/WORKER_COGNITO_PASSWORD"
            )
        if not self.node_token and self.cognito_username and self.cognito_password:
            if not self.cognito_user_pool_id:
                missing.append("WORKER_COGNITO_USER_POOL_ID")
            if not self.cognito_client_id:
                missing.append("WORKER_COGNITO_CLIENT_ID")
        if missing:
            raise SystemExit(
                "missing required env: "
                + ", ".join(missing)
                + " (see .env.example)"
            )
