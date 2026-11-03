"""Cross-platform physical memory reading (intake 63a9ee3a).

Linux reads /proc/meminfo; Windows uses GlobalMemoryStatusEx with a CIM
(Win32_OperatingSystem) fallback; macOS uses sysctl hw.memsize plus vm_stat.
An unreadable host returns None. Callers must report memory as unknown and
never substitute 0, which Hub placement would read as a real empty host.
"""
from __future__ import annotations

import platform
import re
import subprocess
from typing import Callable, Dict, Optional

def _valid(total: Optional[int], available: Optional[int], source: str) -> Optional[Dict]:
    if type(total) is not int or type(available) is not int or total <= 0 or available < 0:
        return None
    return {"total_bytes": total, "available_bytes": min(available, total), "source": source}


def read_proc_meminfo(path: str = "/proc/meminfo", open_fn: Callable = open) -> Optional[Dict]:
    values: Dict[str, int] = {}
    try:
        with open_fn(path, "r", encoding="utf-8") as handle:
            for line in handle:
                key, sep, rest = line.partition(":")
                parts = rest.split()
                if sep and parts:
                    try:
                        values[key.strip()] = int(parts[0]) * 1024
                    except ValueError:
                        continue
    except Exception:
        return None
    available = values.get("MemAvailable")
    if available is None and "MemFree" in values:
        # Older kernels/containers without MemAvailable.
        available = values["MemFree"] + values.get("Buffers", 0) + values.get("Cached", 0)
    return _valid(values.get("MemTotal"), available, "proc_meminfo")


def read_proc_swap_used_percent(path: str = "/proc/meminfo", open_fn: Callable = open) -> Optional[float]:
    """Return swap use percent on Linux, or None when proc telemetry is absent."""
    values: Dict[str, int] = {}
    try:
        with open_fn(path, "r", encoding="utf-8") as handle:
            for line in handle:
                key, sep, rest = line.partition(":")
                parts = rest.split()
                if sep and parts and key.strip() in ("SwapTotal", "SwapFree"):
                    values[key.strip()] = int(parts[0]) * 1024
    except (OSError, ValueError, TypeError):
        return None
    total, free = values.get("SwapTotal"), values.get("SwapFree")
    if type(total) is not int or type(free) is not int or total < 0 or free < 0 or free > total:
        return None
    if total == 0:
        return 0.0
    return round((total - free) * 100.0 / total, 2)


def read_host_swap_used_percent(*, system: Optional[str] = None,
                                meminfo_path: str = "/proc/meminfo",
                                open_fn: Callable = open) -> Optional[float]:
    """Return measured host swap use where the OS exposes it; unknown otherwise."""
    system = system or platform.system()
    if system != "Linux":
        return None
    return read_proc_swap_used_percent(meminfo_path, open_fn)


def read_windows_global_memory() -> Optional[Dict]:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return None
        return _valid(int(stat.ullTotalPhys), int(stat.ullAvailPhys), "global_memory_status_ex")
    except Exception:
        return None


def _run(argv, run_fn: Callable) -> Optional[str]:
    try:
        done = run_fn(argv, capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        return None
    return done.stdout if getattr(done, "returncode", 1) == 0 and isinstance(done.stdout, str) else None


def read_windows_cim(run_fn: Callable = subprocess.run) -> Optional[Dict]:
    out = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "$o=Get-CimInstance Win32_OperatingSystem; \"$($o.TotalVisibleMemorySize) $($o.FreePhysicalMemory)\""],
               run_fn)
    match = re.fullmatch(r"\s*(\d+)\s+(\d+)\s*", out or "")
    if not match:
        return None
    return _valid(int(match.group(1)) * 1024, int(match.group(2)) * 1024, "cim_win32_operating_system")


def read_macos(run_fn: Callable = subprocess.run) -> Optional[Dict]:
    total_out = _run(["sysctl", "-n", "hw.memsize"], run_fn)
    vm_out = _run(["vm_stat"], run_fn)
    if not total_out or not vm_out or not total_out.strip().isdigit():
        return None
    page = re.search(r"page size of (\d+) bytes", vm_out)
    pages: Dict[str, int] = {}
    for name, count in re.findall(r"^(Pages [^:]+):\s+(\d+)\.?$", vm_out, re.M):
        pages[name] = int(count)
    if not page or "Pages free" not in pages:
        return None
    # Free + inactive + speculative + purgeable approximates reclaimable memory.
    available = sum(pages.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"))
    return _valid(int(total_out.strip()), available * int(page.group(1)), "macos_vm_stat")


def read_host_memory(*, system: Optional[str] = None, meminfo_path: str = "/proc/meminfo",
                     open_fn: Callable = open, run_fn: Callable = subprocess.run,
                     windows_api: Callable[[], Optional[Dict]] = read_windows_global_memory) -> Optional[Dict]:
    """Return {total_bytes, available_bytes, source} or None when unknown."""
    system = system or platform.system()
    if system == "Windows":
        return windows_api() or read_windows_cim(run_fn)
    if system == "Darwin":
        return read_macos(run_fn)
    return read_proc_meminfo(meminfo_path, open_fn)
