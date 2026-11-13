"""Private local model contexts. No authentication, queue or network entrypoint.

Each child owns one complete AI engine/session subgraph. The coordinator owns
leases, source transfers and ACKs; only bounded public model settings, image
paths and the engine's parameter subset cross its anonymous pipes.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mediaworker.engine.base import Engine, FailureReport, JobContext, RunResult
from mediaworker.memory import MemoryEstimate

log = logging.getLogger(__name__)
MAX_MESSAGE = 1024 * 1024
CONFIG_FIELDS = {
    "onnx_providers", "models_dir", "faceswap_models_dir", "cutout_models_dir",
    "enhance_models_dir", "enhance_models", "enhance_face_models", "cutout_model",
    "max_image_bytes", "max_image_pixels", "max_image_dimension", "max_output_pixels", "max_result_bytes",
}
PARAM_FIELDS = {
    "cutout": {"model"},
    "enhance": {"model", "scale", "face", "faceStrength", "faceEdgeBlend", "faceFusionArea"},
    "faceswap": {"model", "mode", "targetMedia", "gifProtocol", "outputFormat", "faceSelection", "edgeBlur", "fusionArea"},
}


def encode(value):
    data = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    if len(data) > MAX_MESSAGE:
        raise ValueError("model control message exceeds limit")
    return data + b"\n"


class OwnedJob:
    """Windows kernel kills all model descendants when the coordinator dies."""
    def __init__(self, process):
        self.handle = None
        self.pgid = process.pid
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("per_process", ctypes.c_longlong), ("per_job", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read", "write", "other")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), *[(name, ctypes.c_size_t) for name in ("process_memory", "job_memory", "peak_process", "peak_job")]]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = self.kernel.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("model process ownership unavailable")
        self.handle = handle
        try:
            limits = Extended(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError("model process ownership unavailable")
            if not self.kernel.AssignProcessToJobObject(handle, int(process._handle)):
                raise OSError("model process ownership unavailable")
        except BaseException:
            self.close(); raise

    def terminate(self, process):
        if self.handle is not None:
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise OSError("model process termination pending")
        else:
            # The child is a new session. Its parent-EOF monitor also kills the
            # group if the coordinator disappears while native code is running.
            try:os.killpg(self.pgid, signal.SIGKILL)
            except ProcessLookupError:pass

    def empty(self):
        if self.handle is None:
            try:os.killpg(self.pgid,0)
            except ProcessLookupError:return True
            return False
        import ctypes
        from ctypes import wintypes
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")]+[(name,wintypes.DWORD) for name in ("faults", "total", "active", "terminated")]
        value = Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(value), ctypes.sizeof(value), None):
            raise OSError("model process ownership unknown")
        return value.active == 0

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle); self.handle = None


class ModelFailure(RuntimeError):
    def __init__(self, report):
        self.report = report
        super().__init__(report.message)


class SessionDescriptor:
    def __init__(self, value):
        self.value = value
    def get_providers(self):
        return self.value["providers"]
    def get_provider_options(self):
        return self.value["options"]


class ModelProcess(Engine):
    def __init__(self, cfg, capability, *, profile=None, local=False):
        if capability not in PARAM_FIELDS:
            raise ValueError("unsupported model capability")
        self.cfg, self.task_type, self.profile, self.local = cfg, capability, profile, local
        self.process = self.job = self.private = None
        self.messages, self.threads = queue.Queue(maxsize=256), []
        self.metadata = None
        self.nonce = secrets.token_hex(24)
        self.request = 0
        self.lock = threading.Lock()
        self.protocol_failed = threading.Event()

    @property
    def is_alive(self):
        return self.process is not None and self.process.poll() is None and not self.protocol_failed.is_set()

    @property
    def available_models(self):
        return self.metadata["models"] if self.metadata else []

    @property
    def available_face_models(self):
        return self.metadata["faces"] if self.metadata else []

    @property
    def available_media(self):
        return self.metadata["media"] if self.metadata else []

    @property
    def execution_sessions(self):
        return self.sessions

    def context_spec(self, params):
        if self.task_type == "cutout":
            return self.cfg.cutout_model
        if self.task_type == "enhance":
            model, face = params.get("model", "standard"), params.get("face", "off")
            return (model, face) if isinstance(model,str) and isinstance(face,str) and model in self.available_models and face in {"off",*self.available_face_models} and not (model=="portrait" and face=="off") else None
        model = params.get("model", "hyperswap_256_b" if self.local else "inswapper_128")
        animated = params.get("mode")=="gif" or params.get("targetMedia")=="gif"
        return (model,animated) if isinstance(model,str) and model in self.available_models and (not animated or "gif" in self.available_media) else None

    def context_requirements(self, spec=None):
        # Child reports estimates only; no model or session enters this process.
        values = self.metadata["cost"] if spec is None else self.metadata["profile_costs"][json.dumps(spec,separators=(",",":"))]
        return MemoryEstimate(**{**values, "resident_ram":values["resident_ram"]+256*1024**2,
                               "resident_gpu":values["resident_gpu"]+128*1024**2 if values["devices"] else 0,
                               "devices":tuple(tuple(v) for v in values["devices"]), "external_gpu":True})

    def spawn_context(self, spec=None):
        engine = type(self)(self.cfg,self.task_type,profile=spec,local=self.local)
        engine.load()
        return engine

    def failure_report(self, error, params):
        return error.report if isinstance(error,ModelFailure) else super().failure_report(error,params)

    def _read(self, stream):
        try:
            while True:
                line = stream.readline(MAX_MESSAGE+2)
                if not line:
                    self.protocol_failed.set();break
                if len(line)>MAX_MESSAGE+1 or not line.endswith(b"\n"):
                    raise ValueError("invalid model message")
                value = json.loads(line)
                if not isinstance(value,dict) or value.get("nonce")!=self.nonce:
                    raise ValueError("invalid model message")
                self.messages.put_nowait(value)
        except Exception:
            self.protocol_failed.set()

    def _stderr(self, stream):
        # Diagnostics are bounded and never relayed as task errors/raw logs.
        size=0
        while True:
            block=stream.read(65536)
            if not block:return
            size+=len(block)
            if size>8*1024*1024:
                self.protocol_failed.set();return

    def _send(self,value):
        value={**value,"nonce":self.nonce}
        self.process.stdin.write(encode(value));self.process.stdin.flush()

    def _receive(self,request,deadline,cancel=None,progress=None):
        while True:
            if cancel is not None and cancel.is_set():
                self.close();raise RuntimeError("CANCELLED")
            if time.monotonic()>=deadline:
                self.close();raise RuntimeError("model execution timeout")
            try:value=self.messages.get(timeout=.05)
            except queue.Empty:
                if not self.is_alive:
                    self.close();raise RuntimeError("model context exited")
                continue
            if value.get("request")!=request:
                self.close();raise RuntimeError("model protocol rejected")
            if value.get("type")=="progress" and progress is not None:
                percent=value.get("percent")
                if type(percent) is not int or not 0<=percent<=100:
                    self.close();raise RuntimeError("model protocol rejected")
                progress("processing",percent);continue
            return value

    def load(self):
        if self.is_alive:return
        visible=os.environ.get("CUDA_VISIBLE_DEVICES")
        if visible is not None and (len(visible)>1024 or re.fullmatch(r"[A-Za-z0-9,/_-]*",visible) is None):
            raise ValueError("invalid device selection")
        self.cfg.work_dir.mkdir(parents=True,exist_ok=True)
        self.private=tempfile.TemporaryDirectory(prefix="model-context-",dir=self.cfg.work_dir)
        environment={k:v for k,v in os.environ.items() if k.upper() in {"SYSTEMROOT","WINDIR","COMSPEC","PATHEXT","PATH","LANG","LC_ALL"}}
        if visible is not None:
            environment["CUDA_VISIBLE_DEVICES"]=visible
        environment.update(MEDIAWORKER_LOAD_DOTENV="0",PYTHONNOUSERSITE="1",PYTHONDONTWRITEBYTECODE="1",HOME=self.private.name,USERPROFILE=self.private.name,TEMP=self.private.name,TMP=self.private.name)
        executable=sys.executable
        if os.name=="nt":
            executable=sys._base_executable;environment["__PYVENV_LAUNCHER__"]=sys.executable
        try:
            self.process=subprocess.Popen([executable,"-B","-s",str(Path(__file__).resolve()),"--model-child"],cwd=self.private.name,env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=os.name!="nt",creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0) if os.name=="nt" else 0)
            # No INIT/model load until kernel ownership is established.
            self.job=OwnedJob(self.process)
            for function,stream in ((self._read,self.process.stdout),(self._stderr,self.process.stderr)):
                thread=threading.Thread(target=function,args=(stream,),daemon=True);thread.start();self.threads.append(thread)
            settings={name:str(value) if isinstance(value,Path) else value for name in CONFIG_FIELDS for value in [getattr(self.cfg,name)]}
            self._send({"type":"init","request":0,"capability":self.task_type,"profile":self.profile,"local":self.local,"config":settings,"legacy_swap_path":os.environ.get("FACESWAP_SWAP_MODEL","")})
            value=self._receive(0,time.monotonic()+120)
            if value.get("type")!="ready" or value.get("pid")!=self.process.pid or Path(value.get("prefix","")).resolve()!=Path(sys.prefix).resolve() or Path(value.get("source","")).resolve()!=Path(__file__).resolve():
                raise RuntimeError("model context failed readiness")
            self.metadata=value
            self.sessions={name:SessionDescriptor(descriptor) for name,descriptor in value["sessions"].items()}
        except BaseException:
            self.close();raise

    def run(self,ctx):
        with self.lock:
            if not self.is_alive:raise RuntimeError("model context is unavailable")
            params={name:value for name,value in ctx.params.items() if name in PARAM_FIELDS[self.task_type]}
            if self.task_type=="enhance" and set(ctx.params)-PARAM_FIELDS[self.task_type]:
                raise ValueError("unsupported enhance image parameters")
            self.request+=1
            workdir=ctx.workdir.resolve()
            inputs={name:str(path.resolve()) for name,path in ctx.inputs.items()}
            if any(not Path(path).is_relative_to(workdir) for path in inputs.values()):
                raise ValueError("model input outside attempt")
            try:
                self._send({"type":"run","request":self.request,"workdir":str(workdir),"inputs":inputs,"params":params})
                value=self._receive(self.request,time.monotonic()+300,ctx.cancel,ctx.progress)
            except (OSError,ValueError):
                self.close();raise RuntimeError("model control failed") from None
            if value.get("type")=="failed":
                report=value.get("failure",{})
                raise ModelFailure(FailureReport(**report))
            if value.get("type")!="result":
                self.close();raise RuntimeError("model protocol rejected")
            output=Path(value["path"]).resolve()
            if not output.is_relative_to(workdir) or not output.is_file():
                self.close();raise RuntimeError("model output outside attempt")
            return RunResult(output,value["width"],value["height"],value["content_type"],value.get("duration"))

    def close(self):
        process=self.process
        if process is not None:
            # Unknown exit remains owned synchronously: caller cannot return
            # the context, budget, attempt or task slot while a writer survives.
            while True:
                try:
                    if process.poll() is None or self.job is not None and not self.job.empty():
                        if self.job is not None:self.job.terminate(process)
                        else:process.kill()
                    process.wait(timeout=1)
                    if self.job is None or self.job.empty():break
                except (OSError,subprocess.TimeoutExpired):
                    log.warning("model context cleanup pending")
                time.sleep(.05)
            for thread in self.threads:thread.join()
            for stream in (process.stdin,process.stdout,process.stderr):
                if stream is not None:
                    try:stream.close()
                    except OSError:pass  # exact processes/reader threads already exited
        if self.job is not None:self.job.close();self.job=None
        if self.private is not None:self.private.cleanup();self.private=None


def child_main():
    # Preserve the anonymous protocol pipe before redirecting BOTH Python and
    # native CRT/Win32 stdout. Library loading must not corrupt control frames.
    descriptor=os.dup(sys.stdout.fileno());os.set_inheritable(descriptor,False)
    control_out=os.fdopen(descriptor,"wb",buffering=0)
    os.dup2(sys.stderr.fileno(),sys.stdout.fileno())
    if os.name=="nt":
        import ctypes,msvcrt
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.SetStdHandle.argtypes=[ctypes.c_ulong,ctypes.c_void_p]
        if not kernel.SetStdHandle(ctypes.c_ulong(-11),msvcrt.get_osfhandle(sys.stderr.fileno())):return
    def send(value):
        control_out.write(encode({**value,"nonce":nonce}));control_out.flush()
    first=sys.stdin.buffer.readline(MAX_MESSAGE+2)
    if not first or len(first)>MAX_MESSAGE+1:return
    init=json.loads(first);nonce=init["nonce"]
    if init.get("type")!="init" or init.get("capability") not in PARAM_FIELDS or set(init.get("config",{}))!=CONFIG_FIELDS:return
    incoming=queue.Queue(maxsize=1)
    def monitor():
        while True:
            line=sys.stdin.buffer.readline(MAX_MESSAGE+2)
            if not line:
                if os.name!="nt":os.killpg(os.getpgrp(),signal.SIGKILL)
                os._exit(0)
            if len(line)>MAX_MESSAGE+1:os._exit(1)
            try:value=json.loads(line)
            except Exception:os._exit(1)
            if value.get("nonce")!=nonce:os._exit(1)
            incoming.put(value)
    # Windows Job ownership covers parent death even during native imports.
    # Do not start a pipe-reading Python thread before NumPy/ORT DLL startup.
    # POSIX needs the EOF monitor to terminate its owned session group.
    if os.name!="nt":threading.Thread(target=monitor,daemon=True).start()
    # All Python/library stdout is diagnostic-only. Control writes use the
    # original private pipe; untrusted diagnostics cannot forge its nonce.
    with redirect_stdout(sys.stderr):
        from mediaworker.config import Config
        cfg=Config.from_env(load_env=False)
        for name,value in init["config"].items():setattr(cfg,name,Path(value) if name.endswith("_dir") else value)
        cfg.faceswap_model_auto_download=cfg.cutout_model_auto_download=cfg.auto_update=False
        if init.get("legacy_swap_path"):os.environ["FACESWAP_SWAP_MODEL"]=init["legacy_swap_path"]
        cap=init["capability"];profile=init.get("profile")
        if init.get("local") and cap=="faceswap":
            from mediaworker.local_faceswap import LocalFaceSwap
            engine=LocalFaceSwap(cfg,model_keys=[profile[0]] if profile else None)
        elif cap=="faceswap":
            from mediaworker.engine.faceswap_engine import FaceSwapEngine
            engine=FaceSwapEngine(cfg,model_keys=[profile[0]] if profile else None,enable_gif=bool(profile[1]) if profile else True)
        else:
            if cap=="enhance" and profile:
                cfg.enhance_models=[] if profile[0]=="portrait" else [profile[0]]
                cfg.enhance_face_models=[] if profile[1]=="off" else [profile[1]]
            if cap=="enhance":
                from mediaworker.engine.enhance_engine import EnhanceEngine
                engine=EnhanceEngine(cfg)
            else:
                from mediaworker.engine.cutout_engine import CutoutEngine
                engine=CutoutEngine(cfg)
        engine.load()
        sessions={name:{"providers":session.get_providers(),"options":session.get_provider_options() if hasattr(session,"get_provider_options") else {}} for name,session in engine.execution_sessions.items()}
        if cap=="cutout":profiles=[cfg.cutout_model]
        elif cap=="enhance":profiles=[(model,face) for model in engine.available_models for face in ["off",*engine.available_face_models] if not(model=="portrait" and face=="off")]
        else:profiles=[(model,animated) for model in engine.available_models for animated in ([False,True] if "gif" in getattr(engine,"available_media",[]) and not init.get("local") else [False])]
        costs={json.dumps(spec,separators=(",",":")):asdict(engine.context_requirements(spec)) for spec in profiles}
        send({"type":"ready","request":0,"pid":os.getpid(),"prefix":sys.prefix,"source":str(Path(__file__).resolve()),"models":engine.available_models,"faces":getattr(engine,"available_face_models",[]),"media":getattr(engine,"available_media",[]),"sessions":sessions,"cost":asdict(engine.context_requirements()),"profile_costs":costs})
        while True:
            if os.name=="nt":
                line=sys.stdin.buffer.readline(MAX_MESSAGE+2)
                if not line or len(line)>MAX_MESSAGE+1:return
                value=json.loads(line)
                if value.get("nonce")!=nonce:return
            else:value=incoming.get()
            request=value.get("request")
            if value.get("type")!="run" or type(request) is not int:return
            workdir=Path(value["workdir"]).resolve();inputs={name:Path(path).resolve() for name,path in value["inputs"].items()}
            if any(not path.is_relative_to(workdir) for path in inputs.values()):return
            ctx=JobContext("private-model",cap,value["params"],inputs,workdir,progress=lambda _s,p:send({"type":"progress","request":request,"percent":min(100,max(0,int(p)))}))
            try:
                result=engine.run(ctx)
                if not result.output_path.resolve().is_relative_to(workdir):raise ValueError("model output outside attempt")
                send({"type":"result","request":request,"path":str(result.output_path.resolve()),"width":result.width,"height":result.height,"content_type":result.content_type,"duration":result.duration})
            except Exception as error:
                send({"type":"failed","request":request,"failure":asdict(engine.failure_report(error,ctx.params))})


if __name__=="__main__" and sys.argv[1:]==["--model-child"]:
    child_main()
