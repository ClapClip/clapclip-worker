from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelAsset:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class PublicModelAsset(ModelAsset):
    url: str


MODEL_MANIFESTS: dict[str, tuple[ModelAsset, ...]] = {
    "faceswap-models-v1.0.0": (
        ModelAsset("arcface_w600k_r50.onnx", 174383860, "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43"),
        ModelAsset("hyperswap_1a_256.onnx", 402742682, "c0e98a8a03a238f461ed3d2570e426b49f46745ee400854a60dceeb70c246add"),
        ModelAsset("hyperswap_1b_256.onnx", 402742682, "5124031789c42f71b9558fb71954ef7aedb6da7ed9fac79293e23c61a792a73e"),
        ModelAsset("hyperswap_1c_256.onnx", 402742682, "5528c2d76fe9986c99d829278987ef9f3a630cb606db7628d02b57b330f406a5"),
        ModelAsset("inswapper_128_fp16.onnx", 277680829, "c4eccca86ad177586c85c28bf1a64a9d9ed237e283a15818d831f7facfd3f420"),
    ),
}

CUTOUT_MODELS: dict[str, PublicModelAsset] = {
    "isnet-general-use": PublicModelAsset(
        "isnet-general-use.onnx",
        178648008,
        "60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a",
        "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx",
    ),
}

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path: Path, asset: ModelAsset) -> bool:
    return path.is_file() and path.stat().st_size == asset.size and _sha256(path) == asset.sha256


def _download(client: httpx.Client, url: str, destination: Path, asset: ModelAsset) -> None:
    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    written = 0
    try:
        with client.stream("GET", url, headers={"Accept": "application/octet-stream"}) as response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes():
                    written += len(chunk)
                    if written > asset.size:
                        raise ValueError(f"model download exceeds expected size: {asset.name}")
                    digest.update(chunk)
                    handle.write(chunk)
        if written != asset.size or digest.hexdigest() != asset.sha256:
            raise ValueError(f"model checksum mismatch: {asset.name}")
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def sync_faceswap_models(
    cfg: Config,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    if not cfg.faceswap_model_auto_download or "faceswap" not in cfg.capabilities:
        return
    manifest = MODEL_MANIFESTS.get(cfg.faceswap_model_release)
    if not manifest:
        raise RuntimeError(
            f"no bundled manifest for FaceSwap release {cfg.faceswap_model_release!r}"
        )
    destination = cfg.faceswap_models_dir
    destination.mkdir(parents=True, exist_ok=True)
    missing = [asset for asset in manifest if not _matches(destination / asset.name, asset)]
    if not missing:
        log.info("FaceSwap model bundle ready (%s)", cfg.faceswap_model_release)
        return

    if not cfg.faceswap_model_repository:
        raise RuntimeError("FaceSwap models are missing; configure FACESWAP_MODEL_REPOSITORY or install the verified models locally")

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "clapclip-mediaworker",
    }
    if cfg.github_token:
        headers["Authorization"] = f"Bearer {cfg.github_token}"
    api_url = (
        f"https://api.github.com/repos/{cfg.faceswap_model_repository}/releases/tags/"
        f"{cfg.faceswap_model_release}"
    )
    timeout = httpx.Timeout(30.0, read=600.0, write=30.0, pool=30.0)
    with httpx.Client(
        headers=headers,
        follow_redirects=True,
        timeout=timeout,
        transport=transport,
    ) as client:
        response = client.get(api_url)
        if response.status_code in {401, 404} and not cfg.github_token:
            raise RuntimeError(
                "FaceSwap models are missing and the GitHub repository is private; "
                "set a read-only GITHUB_TOKEN"
            )
        response.raise_for_status()
        remote_assets: dict[str, dict[str, Any]] = {
            item["name"]: item for item in response.json().get("assets", [])
        }
        for asset in missing:
            remote = remote_assets.get(asset.name)
            if not remote:
                raise RuntimeError(f"release asset is missing: {asset.name}")
            log.info("downloading FaceSwap model %s", asset.name)
            _download(client, remote["url"], destination / asset.name, asset)
    log.info("FaceSwap model bundle synchronized (%s)", cfg.faceswap_model_release)


def sync_cutout_model(
    cfg: Config,
    *,
    transport: httpx.BaseTransport | None = None,
    force: bool = False,
) -> None:
    if not cfg.cutout_model_auto_download or (
        not force and "cutout" not in cfg.capabilities
    ):
        return
    asset = CUTOUT_MODELS.get(cfg.cutout_model)
    if asset is None:
        raise RuntimeError(f"no bundled manifest for cutout model {cfg.cutout_model!r}")
    cfg.cutout_models_dir.mkdir(parents=True, exist_ok=True)
    destination = cfg.cutout_models_dir / asset.name
    if _matches(destination, asset):
        log.info("cutout model ready (%s)", cfg.cutout_model)
        return

    log.info("downloading cutout model %s", asset.name)
    timeout = httpx.Timeout(30.0, read=600.0, write=30.0, pool=30.0)
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        transport=transport,
        headers={"User-Agent": "clapclip-mediaworker"},
    ) as client:
        _download(client, asset.url, destination, asset)
    log.info("cutout model synchronized (%s)", cfg.cutout_model)


def sync_models(cfg: Config) -> list[str]:
    """Return capabilities whose model preparation succeeded independently."""
    ready = []
    synchronizers = {
        "faceswap": sync_faceswap_models,
        "cutout": sync_cutout_model,
    }
    for capability in dict.fromkeys(cfg.capabilities):
        if capability in {"enhance", "download"}:
            # Enhance uses provisioned local ONNX files. Its engine verifies
            # model/GPU readiness in load(); no unverified download is attempted.
            ready.append(capability)
            continue
        synchronize = synchronizers.get(capability)
        if synchronize is None:
            log.warning("no model synchronizer for %r; skipping", capability)
            continue
        try:
            synchronize(cfg)
        except Exception:
            log.exception("model preparation failed for %r; disabling capability", capability)
        else:
            ready.append(capability)
    return ready
