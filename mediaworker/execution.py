"""Ready engine contexts before claim; task ownership continues through terminal ACK."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import gc
import logging
import threading
import time

from .config import MAX_CONCURRENCY, MAX_DOWNLOAD_CONCURRENCY
from .single_instance import FileLock, WorkerAlreadyRunning
from .memory import MemoryEstimate, MemoryLedger, headroom as memory_headroom

log = logging.getLogger(__name__)
MIB = 1024 * 1024




@dataclass
class Context:
    engine: object
    primary: bool = False
    busy: bool = False
    claimed: bool = False
    last_used: float = 0
    cost: MemoryEstimate = MemoryEstimate()
    resident_epoch: int = 0
    spec: object = None
    demand: object = None


class EngineLease:
    """Only run owns the borrowed context; policy methods remain stateless."""
    def __init__(self, owner, capability, context):
        self.owner, self.capability, self.context = owner, capability, context
        self.policy = owner.engines[capability]

    def __getattr__(self, name):
        context = self.context
        return getattr(context.engine if context is not None else self.policy, name)

    def claimed(self, job=None):
        with self.owner.lock:
            if self.context is not None:
                self.context.claimed = True
                method = getattr(self.context.engine, "context_spec", None)
                self.context.demand = method(job.params) if callable(method) and job is not None else None

    def release(self):
        with self.owner.lock:
            if self.context is not None:
                self.context.busy = self.context.claimed = False
                self.context.demand = None
                self.owner.memory.release_workspace(self.context.cost)
                self.context.last_used = time.monotonic()
                self.context = None

    def run(self, ctx):
        context = self.context
        if context is None:
            raise RuntimeError("execution context is not reserved")
        try:
            return context.engine.run(ctx)
        finally:
            # Includes failure/cancellation. Native Run must return before reuse.
            self.release()


class Admission:
    def __init__(self, owner, capability, engine, slot, download):
        self.owner, self.capability, self.engine = owner, capability, engine
        self.slot, self.download = slot, download

    def claimed(self, job=None):
        if isinstance(self.engine, EngineLease):
            self.engine.claimed(job)

    def release(self):
        with self.owner.lock:
            if self.slot is None:
                return
            if isinstance(self.engine, EngineLease):
                self.engine.release()
            try:
                self.slot.release()
            finally:
                self.slot = None
                try:
                    if self.download is not None:
                        self.download.release()
                finally:
                    self.download = None
                    self.owner.active -= 1


class ExecutionResources:
    """One runtime's fair preclaim admission, lazy contexts and owned executors."""
    def __init__(self, cfg, engines, *, headroom=memory_headroom):
        self.cfg, self.headroom = cfg, headroom
        self.limit = min(MAX_CONCURRENCY, max(1, cfg.concurrency))
        self.context_limit = min(self.limit, max(1, cfg.ai_contexts))
        self.download_limit = min(self.limit, MAX_DOWNLOAD_CONCURRENCY, cfg.download_concurrency)
        self.engines = dict(engines)
        self.pools = {cap: [Context(engine, primary=True, cost=self._cost(engine))]
                      for cap, engine in engines.items() if cap != "download"}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=self.limit, thread_name_prefix="task-execution")
        self.control = ThreadPoolExecutor(max_workers=1, thread_name_prefix="context-preparation")
        self.growing, self.futures = set(), set()
        self.cooldown, self.growth_retry, self.requested = {}, {}, {}
        self.states = {cap: "ready" for cap in self.pools}
        self.active, self.cursor, self.closed = 0, 0, False
        self.memory = MemoryLedger()
        for pool in self.pools.values():
            for context in pool:self.memory.account_materialized(context.cost)
        self.refreshing = False
        self.last_refresh = 0
        self.budget_ready = False
        self._refresh_memory()

    @staticmethod
    def _cost(engine, spec=None):
        method = getattr(engine, "context_requirements", None)
        return method(spec) if callable(method) else MemoryEstimate()

    def _refresh_memory(self):
        try:
            with self.lock:
                devices = tuple({device for pool in self.pools.values() for c in pool for device in c.cost.devices})
            ram, gpu = self.headroom(devices)
            with self.lock:
                if self.memory.rebase(ram, gpu):
                    self.budget_ready = True
                self.last_refresh = time.monotonic()
        finally:
            with self.lock:
                self.refreshing = False

    def _refresh_if_idle(self):
        if not self.memory.tickets and not self.refreshing and time.monotonic() - self.last_refresh > 5:
            self.refreshing = True
            future = self.control.submit(self._refresh_memory)
            self.futures.add(future)
            future.add_done_callback(self._construction_finished)

    def register(self, capability, engine):
        with self.lock:
            if self.active or self.memory.tickets or self.growing:
                raise RuntimeError("cannot register primary while execution resources are owned")
            for context in self.pools.get(capability,[]):
                self._dispose(context)
                if not context.primary or context.cost.external_gpu:
                    self.memory.release_resident(context.cost,context.resident_epoch)
            self.engines[capability] = engine
            if capability != "download":
                self.pools[capability] = [Context(engine, primary=True, cost=self._cost(engine))]
                self.memory.account_materialized(self.pools[capability][0].cost)
                self.states[capability] = "ready"
                self.last_refresh = 0
                self.budget_ready = False

    def defer_empty(self, capability, seconds):
        with self.lock:
            self.cooldown[capability] = time.monotonic() + seconds

    def _grow_if_needed(self, cap):
        pool = self.pools[cap]
        if self.closed or cap in self.growing or time.monotonic()<self.growth_retry.get(cap,0):
            return
        dead=next((c for c in pool if not c.busy and not getattr(c.engine,"is_alive",True)),None)
        if dead is not None or not pool:
            self.growing.add(cap)
            future=self.control.submit(self._replace_dead,cap,dead)
            self.futures.add(future);future.add_done_callback(self._construction_finished)
            return
        desired = next((c.demand for c in reversed(pool) if c.claimed), None)
        if desired is None and callable(getattr(pool[0].engine,"context_spec",None)):
            return  # Invalid/unvalidated params cannot trigger model loading.
        stale = any(not c.primary and not c.busy and c.spec != desired for c in pool)
        if (self.closed or cap in self.growing or len(pool) >= self.context_limit
                and not stale
                or time.monotonic() < self.growth_retry.get(cap, 0)
                or not any(c.claimed for c in pool)
                or not callable(getattr(pool[0].engine, "spawn_context", None))):
            return
        self.growing.add(cap)
        future = self.control.submit(self._grow, cap, desired)
        self.futures.add(future)
        future.add_done_callback(self._construction_finished)

    def _replace_dead(self,cap,context):
        # Rebuild capacity only for future claims, never rerun an accepted job.
        reference=self.engines[cap] if context is None else context.engine
        primary=context is None or context.primary
        spec=None if primary else context.spec
        if context is not None:
            self._dispose(context)  # exact child exit before refund/removal
            with self.lock:
                self.pools[cap].remove(context)
                if not primary or context.cost.external_gpu:
                    self.memory.release_resident(context.cost,context.resident_epoch)
        self._grow(cap,spec,primary_context=primary,reference=reference)

    def _construction_finished(self, future):
        with self.lock:
            self.futures.discard(future)

    @staticmethod
    def _dispose(context):
        engine, context.engine = context.engine, None
        try:
            close = getattr(engine, "close", None)
            if callable(close):
                close()
        finally:
            del engine
            gc.collect()

    def _evict_idle_extra(self, exclude, mismatched_spec=None):
        choices = [(c.last_used, cap, c) for cap, pool in self.pools.items() for c in pool
                   if not c.primary and not c.busy and (cap != exclude or c.spec != mismatched_spec)]
        if not choices:
            return None
        _, cap, context = min(choices, key=lambda item: (item[1] != exclude, item[0]))
        self.pools[cap].remove(context)
        return context

    def _grow(self, cap, spec, *, primary_context=False, reference=None):
        candidate = None
        booked = None
        booked_epoch = None
        try:
            with self.lock:
                primary = reference if reference is not None else self.engines[cap]
                cost = self._cost(primary, spec)
            # Book residents before construction. Workspace tickets from jobs
            # still downloading input remain charged in the same budget epoch.
            while True:
                with self.lock:
                    if self.closed:
                        return
                    quota = sum(map(len, self.pools.values())) < MAX_CONCURRENCY and len(self.pools[cap]) < self.context_limit
                    if quota and self.memory.reserve_resident(cost):
                        booked = cost
                        booked_epoch = self.memory.epoch
                        break
                    victim = self._evict_idle_extra(cap, spec)
                if victim is None:
                    break
                self._dispose(victim)
                with self.lock:self.memory.release_resident(victim.cost,victim.resident_epoch)
            ram, vram = self.headroom(cost.devices)
            if booked is None or ram < cost.resident_ram + 1024 * MIB or cost.resident_gpu and vram < cost.resident_gpu + 256 * MIB:
                with self.lock:
                    changed = self.states[cap] != "waiting_for_memory"
                    self.states[cap] = "waiting_for_memory"
                if changed:
                    log.info("execution context waiting capability=%s reason=memory ready_contexts=%d limit=%d",
                             cap, len(self.pools[cap]), self.context_limit)
                return
            with self.lock:
                if self.closed or sum(map(len, self.pools.values())) >= MAX_CONCURRENCY:
                    return
            engine = primary.spawn_context(spec)
            candidate = Context(engine, primary=primary_context, cost=cost, resident_epoch=booked_epoch, spec=spec)
            if not set(engine.available_models) <= set(primary.available_models):
                raise RuntimeError("context advertised an unvalidated model")
            for name in ("available_face_models", "available_media"):
                if not set(getattr(engine, name, [])) <= set(getattr(primary, name, [])):
                    raise RuntimeError("context advertised an unvalidated feature")
            original = getattr(primary, "execution_sessions", {})
            cloned = getattr(engine, "execution_sessions", {})
            if set(map(id, original.values())) & set(map(id, cloned.values())):
                raise RuntimeError("execution context shared a model session")
            for name, session in cloned.items():
                if name not in original or session.get_providers() != original[name].get_providers():
                    raise RuntimeError("execution context changed provider")
            with self.lock:
                if not self.closed:
                    self.pools[cap].append(candidate)
                    if primary_context:self.engines[cap]=engine
                    self.memory.resident_ready(booked_epoch)
                    self.states[cap] = "ready"
                    candidate = None
                    booked = None
        except Exception as error:
            with self.lock:
                self.states[cap] = "context_unavailable"
            log.warning("additional execution context unavailable capability=%s kind=%s", cap, type(error).__name__)
        finally:
            if candidate is not None:
                self._dispose(candidate)
            gc.collect()
            with self.lock:
                if booked is not None:
                    self.memory.release_resident(booked, booked_epoch, building=True)
                self.growing.discard(cap)
                self.growth_retry[cap] = time.monotonic() + 5

    def try_acquire(self, capabilities):
        """Never wait in a task thread or claim without a ready context."""
        from .scheduling import acquire_worker_slot
        now = time.monotonic()
        with self.lock:
            if self.closed:
                return None
            self._refresh_if_idle()
            if not self.budget_ready:
                return None
            for cap in capabilities:
                if cap in self.engines:
                    self.requested[cap] = now
            caps = list(self.engines)
            ready = {}
            for cap in caps:
                if now < self.cooldown.get(cap, 0) or now - self.requested.get(cap, -100) > .5:
                    continue
                if cap == "download":
                    ready[cap] = None
                else:
                    desired = next((c.demand for c in reversed(self.pools[cap]) if c.claimed), None)
                    free = next((c for c in self.pools[cap] if not c.busy and getattr(c.engine,"is_alive",True) and
                                 (c.primary or desired is None or c.spec == desired)), None)
                    if free is not None:
                        ready[cap] = free
                    else:
                        self._grow_if_needed(cap)
            if not ready or self.active >= self.limit:
                return None
            order = caps[self.cursor:] + caps[:self.cursor]
            for cap in (c for c in order if c in ready and c in capabilities):
                download = slot = None
                context = (Context(self.engines[cap], primary=True,
                                   cost=MemoryEstimate(workspace_ram=256 * MIB)) if cap == "download" else ready[cap])
                workspace = False
                try:
                    workspace = self.memory.reserve_workspace(context.cost, context.primary)
                    if not workspace:
                        self.states[cap] = "waiting_for_memory"
                        continue
                    if cap == "download":
                        for index in range(self.download_limit):
                            try:
                                download = FileLock(self.cfg.runtime_dir / "download-slots" / f"slot-{index}.lock").acquire()
                                break
                            except WorkerAlreadyRunning:
                                pass
                        if download is None:
                            continue
                    slot = acquire_worker_slot(self.cfg.runtime_dir)
                    if slot is None:
                        return None
                    context.busy = True
                    engine = EngineLease(self, cap, context)
                    self.active += 1
                    self.cursor = (caps.index(cap) + 1) % len(caps)
                    admission = Admission(self, cap, engine, slot, download)
                    # Ownership transfers only after the complete admission exists.
                    slot = download = None
                    workspace = False
                    return admission
                finally:
                    try:
                        if slot is not None:
                            slot.release()
                    finally:
                        try:
                            if download is not None:
                                download.release()
                        finally:
                            if workspace:
                                context.busy = context.claimed = False
                                self.memory.release_workspace(context.cost)
            return None

    def snapshot(self):
        with self.lock:
            return {"taskLimit": self.limit, "downloadLimit": self.download_limit,
                    "aiContextLimit": self.context_limit, "activeTasks": self.active,
                    "reservedWorkspaceRAM": self.memory.workspace_ram,
                    "reservedResidentRAM": self.memory.resident_ram,
                    "reservedModelChildGPU": self.memory.external_gpu,
                    "downloadState": self.states.get("download", "ready"),
                    "contexts": {cap: {"ready": len(pool), "busy": sum(c.busy for c in pool),
                                       "state": self.states[cap]} for cap, pool in self.pools.items()}}

    def close(self):
        with self.lock:
            self.closed = True
            if self.active:
                raise RuntimeError("cannot release execution resources with owned tasks")
        # Cancel work not entered; native construction already running remains
        # owned until it returns. Never claim cleanup while a native thread lives.
        self.control.shutdown(wait=True, cancel_futures=True)
        self.executor.shutdown(wait=True, cancel_futures=False)
        for pool in self.pools.values():
            for context in pool:
                self._dispose(context)
                if not context.primary or context.cost.external_gpu:
                    self.memory.release_resident(context.cost, context.resident_epoch)
        self.pools.clear()
