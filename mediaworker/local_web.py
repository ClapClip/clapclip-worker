"""Loopback-only standalone workbench. No cluster authentication or claims."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import secrets
import shutil
import threading
import time
from urllib.parse import urlsplit

from .config import Config
from .engine.base import JobContext
from .images import validate_image
from .scheduling import acquire_worker_slot
from .single_instance import FileLock

TOOLS = ("faceswap", "cutout", "enhance", "download")
ASSETS = Path(__file__).with_name("web")
log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    tool: str
    params: dict
    workdir: Path
    inputs: dict
    status: str = "queued"
    progress: int = 0
    error: str = ""
    result: object = None
    metadata: dict | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    created: float = field(default_factory=time.monotonic)


class Workbench:
    def __init__(self, cfg, root, engines=None):
        self.cfg, self.root = cfg, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.engines = engines if engines is not None else {}
        self.states = {tool: ("ready" if tool in self.engines else "loading") for tool in TOOLS}
        self.jobs, self.lock = {}, threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="local-job")
        self.download_lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.closing = threading.Event()

    def load(self):
        from .engine.registry import ENGINE_FACTORIES
        from .local_faceswap import LocalFaceSwap
        for tool in TOOLS:
            if self.closing.is_set():
                return
            try:
                engine = LocalFaceSwap(self.cfg) if tool == "faceswap" else ENGINE_FACTORIES[tool](self.cfg)
                engine.load()
                if tool == "enhance" and not engine.available_models:
                    raise RuntimeError("no installed enhancement models")
                with self.lock:
                    self.engines[tool], self.states[tool] = engine, "ready"
            except Exception:
                log.exception("local engine unavailable: %s", tool)
                with self.lock:
                    self.states[tool] = "unavailable"

    def state(self):
        with self.lock:
            self.clean()
            enhance = self.engines.get("enhance")
            faces = list(getattr(enhance, "_faces", {})) if getattr(enhance, "_detector", None) is not None else []
            tools = {}
            for tool in TOOLS:
                models = self.engines[tool].available_models if tool in self.engines else []
                if tool == "enhance" and not faces:
                    models = [name for name in models if name != "portrait"]
                tools[tool] = {"state": self.states[tool], "models": models}
                if tool == "enhance" and self.states[tool] == "ready" and not models:
                    tools[tool]["state"] = "unavailable"
            return {"tools": tools,
                    "faceModels": faces, "maxImageBytes": self.cfg.max_image_bytes, "concurrency": 3}

    def clean(self):
        # Only terminal directories created by this server instance are removed.
        for identifier, job in list(self.jobs.items()):
            if job.status in {"ready", "failed", "cancelled"} and time.monotonic() - job.created > 3600:
                shutil.rmtree(job.workdir, ignore_errors=True)
                del self.jobs[identifier]

    def submit(self, data):
        if not isinstance(data, dict) or data.get("tool") not in TOOLS:
            raise ValueError("请选择处理工具")
        tool = data["tool"]
        with self.lock:
            self.clean()
            if self.closing.is_set() or self.states[tool] != "ready":
                raise ValueError("工具未就绪，请检查模型与运行环境")
            if len(self.jobs) >= 32:
                raise ValueError("本次会话任务已满，请清理结果后重试")
            if sum(j.status in {"queued", "running"} for j in self.jobs.values()) >= 9:
                raise ValueError("等待中的任务较多，请稍后重试")
            params = data.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("参数格式不正确")
            allowed = {"model", "scale", "face", "faceStrength", "faceEdgeBlend", "faceFusionArea", "edgeBlur", "fusionArea"}
            if tool == "download":
                from .downloader.policy import DOWNLOAD_LIMITS
                operation = params.get("operation")
                if operation == "info":
                    params = {"operation": "info", "url": params.get("url"), "limits": dict(DOWNLOAD_LIMITS)}
                elif operation == "download":
                    source = self.jobs.get(params.get("infoId"))
                    if not source or source.tool != "download" or source.status != "ready" or not source.metadata:
                        raise ValueError("请先解析链接")
                    option = source.metadata["options"].get(params.get("formatId"))
                    if not option:
                        raise ValueError("请选择有效下载格式")
                    params = {"operation": "download", "url": source.params["url"], "option": option, "limits": dict(DOWNLOAD_LIMITS)}
                else:
                    raise ValueError("无效下载操作")
            elif set(params) - allowed:
                raise ValueError("包含不支持的参数")
            identifier = secrets.token_hex(16)
            directory = self.root / identifier
            directory.mkdir()
            inputs = {}
            try:
                names = ("target", "source") if tool == "faceswap" else () if tool == "download" else ("image",)
                payload = data.get("inputs", {})
                if not isinstance(payload, dict) or set(payload) != set(names):
                    raise ValueError("请上传所需图片")
                for name in names:
                    value = payload[name]
                    if not isinstance(value, str) or len(value) > (self.cfg.max_image_bytes + 2) // 3 * 4:
                        raise ValueError("图片超过大小限制")
                    try:
                        raw = base64.b64decode(value, validate=True)
                    except (ValueError, TypeError):
                        raise ValueError("图片编码无效") from None
                    path = directory / (name + ".image")
                    path.write_bytes(raw)
                    try:
                        validate_image(path, self.cfg)
                    except Exception:
                        raise ValueError("图片格式或尺寸不符合要求，请使用 JPG、PNG 或 WebP") from None
                    inputs[name] = path
                job = Job(identifier, tool, params, directory, inputs)
                self.jobs[identifier] = job
                self.pool.submit(self.execute, job)
                return self.public(job)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise

    def execute(self, job):
        slot, download = None, False
        try:
            if job.tool == "download":
                while not job.cancel.is_set():
                    download = self.download_lock.acquire(timeout=.2)
                    if download:
                        break
            while not job.cancel.is_set():
                slot = acquire_worker_slot(self.cfg.runtime_dir)
                if slot:
                    break
                job.cancel.wait(.2)
            if job.cancel.is_set():
                return
            job.status = "running"
            def progress(_status, percent):
                job.progress = max(0, min(99, int(percent)))
            context = JobContext(job.id, job.tool, job.params, job.inputs, job.workdir, job.cancel, progress)
            result = self.engines[job.tool].run(context)
            if not result.output_path.resolve().is_relative_to(job.workdir.resolve()):
                raise RuntimeError("engine output outside job directory")
            if job.cancel.is_set():
                return
            if result.content_type == "application/json":
                job.metadata = json.loads(result.output_path.read_text(encoding="utf-8"))
            job.result, job.progress, job.status = result, 100, "ready"
        except Exception:
            log.exception("local task failed: %s", job.tool)
            job.error, job.status = "处理失败，请检查素材、模型与运行环境后重试。", "failed"
        finally:
            if job.cancel.is_set():
                job.result, job.status = None, "cancelled"
            if slot:
                slot.release()
            if download:
                self.download_lock.release()

    def public(self, job):
        data = {"id": job.id, "status": job.status, "progress": job.progress, "error": job.error}
        if job.status == "ready" and job.metadata:
            data["media"] = {key: job.metadata[key] for key in ("title", "formats", "duration")}
        elif job.status == "ready":
            data["result"] = "/api/jobs/" + job.id + "/result"
            data["contentType"] = job.result.content_type
        return data

    def close(self):
        self.closing.set()
        with self.lock:
            for job in self.jobs.values():
                job.cancel.set()
        self.pool.shutdown(wait=True, cancel_futures=False)
        for job in self.jobs.values():
            shutil.rmtree(job.workdir, ignore_errors=True)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, workbench):
        self.app = workbench
        super().__init__(address, Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_args):
        pass  # Never log submitted URLs or tokens.

    def send(self, code, body, kind="application/json", attachment=None):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; media-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if attachment:
            self.send_header("Content-Disposition", 'attachment; filename="' + attachment + '"')
        self.end_headers()
        self.wfile.write(body)

    def json(self, code, value):
        self.send(code, json.dumps(value, ensure_ascii=False).encode())

    def authorized(self, mutate=False):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        return not mutate or (origin == self.server.origin and secrets.compare_digest(self.headers.get("X-Local-Token", ""), self.server.app.token))

    def do_GET(self):
        if not self.authorized():
            return self.json(403, {"error": "访问被拒绝"})
        path = urlsplit(self.path).path
        if path in {"/", "/app.js", "/style.css"}:
            name = "index.html" if path == "/" else path[1:]
            kind = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[name]
            return self.send(200, (ASSETS / name).read_bytes(), kind)
        if path == "/api/state":
            return self.json(200, {**self.server.app.state(), "token": self.server.app.token})
        parts = path.strip("/").split("/")
        if len(parts) in {3, 4} and parts[:2] == ["api", "jobs"]:
            with self.server.app.lock:
                job = self.server.app.jobs.get(parts[2])
                if job:
                    if len(parts) == 3:
                        return self.json(200, self.server.app.public(job))
                    if parts[3] == "result" and job.status == "ready" and job.result and not job.metadata:
                        result = job.result
                        # Stream media without loading a whole video into memory.
                        self.send_response(200)
                        self.send_header("Content-Type", result.content_type)
                        self.send_header("Content-Length", str(result.output_path.stat().st_size))
                        self.send_header("Content-Disposition", 'attachment; filename="result' + result.output_path.suffix + '"')
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        with result.output_path.open("rb") as stream:
                            shutil.copyfileobj(stream, self.wfile)
                        return
        return self.json(404, {"error": "未找到"})

    def do_POST(self):
        if not self.authorized(mutate=True):
            # Closing with unread POST bytes can reset the socket on Windows
            # before the client receives 403. Drain only small, bounded bodies;
            # never parse them or accept an unbounded/chunked rejected upload.
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not self.headers.get("Transfer-Encoding") and 0 < length <= 65536:
                self.rfile.read(length)
            return self.json(403, {"error": "请从本机页面操作"})
        try:
            if self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding"):
                raise ValueError("请求格式不正确")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= self.server.app.cfg.max_image_bytes * 3 + 65536:
                return self.json(413, {"error": "上传内容过大"})
            data = json.loads(self.rfile.read(length))
            path = urlsplit(self.path).path
            if path == "/api/jobs":
                return self.json(202, self.server.app.submit(data))
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] in {"cancel", "delete"}:
                with self.server.app.lock:
                    job = self.server.app.jobs.get(parts[2])
                    if job:
                        if parts[3] == "cancel":
                            job.cancel.set()
                        elif job.status in {"ready", "failed", "cancelled"}:
                            shutil.rmtree(job.workdir, ignore_errors=True)
                            del self.server.app.jobs[job.id]
                        else:
                            raise ValueError("请先取消正在处理的任务")
                        return self.json(200, {"ok": True})
            return self.json(404, {"error": "未找到"})
        except (ValueError, TypeError, KeyError):
            return self.json(400, {"error": "请求无效，请检查图片和参数，或等待工具就绪后重试。"})


def run(args):
    from .redaction import RedactingFormatter
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.env_file:
        from dotenv import load_dotenv
        if not Path(args.env_file).is_file():
            raise SystemExit("本地配置文件不存在")
        load_dotenv(args.env_file, override=True)
    cfg = Config.from_env(load_env=False)
    cfg.auto_update = False
    cfg.work_dir = Path.home() / ".mediaworker" / "local"
    with FileLock(cfg.runtime_dir / "local-web.lock"):
        app = Workbench(cfg, cfg.work_dir / secrets.token_hex(8))
        server = LocalServer(("127.0.0.1", args.port), app)
        loader = threading.Thread(target=app.load, name="local-models")
        loader.start()
        print(f"MediaWorker 本地工作台：{server.origin}（Ctrl+C 退出）", flush=True)
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            app.close()
            loader.join()
            shutil.rmtree(app.root, ignore_errors=True)
