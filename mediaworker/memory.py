"""Query-only provider budgets and explicit unmaterialized workspace accounting."""
from dataclasses import dataclass
import ctypes
import os
import re
import subprocess
import sys
import uuid

MIB = 1024 * 1024


@dataclass(frozen=True)
class MemoryEstimate:
    resident_ram: int = 0
    resident_gpu: int = 0
    workspace_ram: int = 0
    workspace_gpu: int = 0
    devices: tuple = ()
    external_gpu: bool = False


def available_ram():
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except ImportError:
        pass
    if os.name == "nt":
        class Memory(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        *[(name, ctypes.c_ulonglong) for name in
                          ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]]
        value = Memory(); value.length = ctypes.sizeof(value)
        return int(value.available) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)) else 0
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=2, check=True)
            page = re.search(r"page size of (\d+) bytes", result.stdout)
            values = [int(x) for x in re.findall(r"Pages (?:free|inactive|speculative):\s+(\d+)\.", result.stdout)]
            return int(page[1]) * sum(values) if page and len(values) == 3 else 0
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 0


def dml_budget(device_id):
    """IDXGI adapter ordinal matches the explicit/default DML device_id.

    QueryVideoMemoryInfo is this process's WDDM budget, not NVIDIA global free
    VRAM and not an inference-peak guarantee. COM pointers are always released.
    """
    if os.name != "nt" or type(device_id) is not int or not 0 <= device_id < 64:
        return 0
    from ctypes import wintypes
    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]
    class Info(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("budget", "usage", "available", "reservation")]
    def guid(value):
        result = GUID(); result.data[:] = uuid.UUID(value).bytes_le; return result
    def call(pointer, index, result, *arguments):
        address = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[index]
        return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(address)
    factory, adapter, adapter3 = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    dxgi = ctypes.WinDLL("dxgi")
    dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    dxgi.CreateDXGIFactory1.restype = wintypes.HRESULT if hasattr(wintypes, "HRESULT") else ctypes.c_long
    try:
        if dxgi.CreateDXGIFactory1(ctypes.byref(guid("770aae78-f26f-4dba-a829-253c83d1b387")), ctypes.byref(factory)) != 0:
            return 0
        if call(factory, 12, ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(factory, device_id, ctypes.byref(adapter)) != 0:
            return 0
        if call(adapter, 0, ctypes.c_long, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                adapter, ctypes.byref(guid("645967a4-1392-4310-a798-8053ce3e93fd")), ctypes.byref(adapter3)) != 0:
            return 0
        info = Info()
        if call(adapter3, 14, ctypes.c_long, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(Info))(
                adapter3, 0, 0, ctypes.byref(info)) != 0:
            return 0
        return max(0, info.budget - info.usage)
    finally:
        for pointer in (adapter3, adapter, factory):
            if pointer:
                call(pointer, 2, ctypes.c_ulong)(pointer)


def headroom(devices):
    values = []
    for provider, device in devices:
        if provider == "DmlExecutionProvider":
            try:
                values.append(dml_budget(device))
            except (OSError, ValueError, TypeError, AttributeError):
                values.append(0)
        else:
            # CUDA ordinal can be remapped. Do not pretend an unrelated GPU's
            # nvidia-smi free memory describes the selected session.
            values.append(0)
    return available_ram(), min(values) if values else 0


class MemoryLedger:
    """Stable budget epoch: loaded residents and future workspaces are distinct.

    Rebase only without workspace or construction tickets. Fresh free already
    excludes loaded sessions, so their old estimates become non-refundable.
    Workspaces stay booked from before claim through actual Run exit.
    """
    def __init__(self):
        self.ram = self.gpu = 0
        self.resident_ram = self.resident_gpu = 0
        self.workspace_ram = self.workspace_gpu = 0
        self.tickets = 0
        self.known_gpu = False
        self.unknown_gpu_active = 0
        self.epoch = 0
        self.building = 0
        # DXGI's query is for THIS process. Model-child residents are not in
        # that usage figure and must remain charged across every quiet epoch.
        self.external_gpu = 0

    def rebase(self, ram, gpu):
        if self.tickets or self.building:
            return False
        # Fresh free already excludes materialized sessions. Prior estimates
        # become non-refundable in this epoch; eviction waits for a real probe
        # before recovering their actual (not estimated) released bytes.
        self.epoch += 1
        self.resident_ram = self.resident_gpu = 0
        self.ram = max(0, ram - 1024 * MIB)
        self.gpu = max(0, gpu - 256 * MIB)
        self.known_gpu = gpu > 0
        return True

    def reserve_workspace(self, cost, primary):
        if self.workspace_ram + self.resident_ram + cost.workspace_ram > self.ram:
            return False
        if cost.workspace_gpu:
            if not self.known_gpu:
                if not primary or self.unknown_gpu_active:
                    return False
                self.unknown_gpu_active += 1
            elif self.workspace_gpu + self.resident_gpu + self.external_gpu + cost.workspace_gpu > self.gpu:
                return False
        self.workspace_ram += cost.workspace_ram
        self.workspace_gpu += cost.workspace_gpu
        self.tickets += 1
        return True

    def release_workspace(self, cost):
        self.workspace_ram -= cost.workspace_ram
        self.workspace_gpu -= cost.workspace_gpu
        self.tickets -= 1
        if cost.workspace_gpu and not self.known_gpu:
            self.unknown_gpu_active -= 1

    def reserve_resident(self, cost):
        if (self.resident_ram + self.workspace_ram + cost.resident_ram > self.ram
                or cost.resident_gpu and (not self.known_gpu or self.resident_gpu + self.external_gpu + self.workspace_gpu + cost.resident_gpu > self.gpu)):
            return False
        self.resident_ram += cost.resident_ram
        if cost.external_gpu:self.external_gpu += cost.resident_gpu
        else:self.resident_gpu += cost.resident_gpu
        self.building += 1
        return True

    def resident_ready(self, epoch):
        if epoch != self.epoch or self.building < 1:
            raise RuntimeError("resident construction ownership changed")
        self.building -= 1

    def release_resident(self, cost, epoch, *, building=False):
        if cost.external_gpu:
            self.external_gpu -= cost.resident_gpu
        if epoch == self.epoch:
            self.resident_ram -= cost.resident_ram
            if not cost.external_gpu:self.resident_gpu -= cost.resident_gpu
            if building:
                self.building -= 1

    def account_materialized(self, cost):
        # RAM free is system-wide and already reflects the loaded child;
        # parent DXGI usage does not, so keep the child's GPU commitment.
        if cost.external_gpu:self.external_gpu += cost.resident_gpu
