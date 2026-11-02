"""Process-tree resource sampling on Windows (ctypes, no third-party deps).

Mirrors the /proc based sampler used on Linux: given root PIDs, walk the exact
descendant tree and sum working-set memory, private bytes and CPU seconds.
Unreadable processes are skipped; nothing readable returns None (unknown, never
a fabricated zero). Callers must only use this on ``os.name == "nt"``.
"""
from __future__ import annotations

import os
from typing import Optional

MAX_TREE = 1024
_QUERY_LIMITED = 0x1000
_SNAPSHOT_PROCESS = 0x2


def _kernel32():
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


def process_parents() -> dict[int, int]:
    """Return {pid: parent_pid} for every live process, {} when unavailable."""
    if os.name != "nt":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

        k32 = _kernel32()
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(_SNAPSHOT_PROCESS, 0)
        if snap in (None, wintypes.HANDLE(-1).value):
            return {}
        parents: dict[int, int] = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
        return parents
    except Exception:
        return {}


def tree_pids(roots: list[int], parents: Optional[dict[int, int]] = None) -> list[int]:
    """Roots plus all live descendants (bounded). Roots need not be alive."""
    valid = [p for p in roots if type(p) is int and p > 0]
    if not valid:
        return []
    parents = process_parents() if parents is None else parents
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    seen = set(valid)
    queue = list(valid)
    while queue:
        for child in children.get(queue.pop(), [])[:256]:
            if child not in seen:
                seen.add(child)
                queue.append(child)
                if len(seen) >= MAX_TREE:
                    return sorted(seen)
    return sorted(seen)


def process_stats(pid: int) -> Optional[dict]:
    """{working_set_bytes, private_bytes, cpu_seconds} for one pid, None if unreadable."""
    try:
        import ctypes
        from ctypes import wintypes

        class COUNTERS(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                        ("PrivateUsage", ctypes.c_size_t)]

        k32 = _kernel32()
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(_QUERY_LIMITED, False, int(pid))
        if not handle:
            return None
        try:
            counters = COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if not k32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            cpu = None
            if k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                   ctypes.byref(kernel), ctypes.byref(user)):
                ticks = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime
                cpu = (ticks(kernel) + ticks(user)) / 1e7
            return {"working_set_bytes": int(counters.WorkingSetSize),
                    "private_bytes": int(counters.PrivateUsage), "cpu_seconds": cpu}
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def sample_tree(roots: list[int], parents: Optional[dict[int, int]] = None, stats=process_stats) -> Optional[dict]:
    """Aggregate memory (MiB) and CPU seconds across the process tree under roots."""
    pids = tree_pids(roots, parents)
    working = private = 0
    cpu = 0.0
    counted = ticked = False
    for pid in pids:
        row = stats(pid)
        if not row:
            continue
        counted = True
        working += row["working_set_bytes"]
        private += row["private_bytes"]
        if row.get("cpu_seconds") is not None:
            cpu += row["cpu_seconds"]
            ticked = True
    if not counted:
        return None
    mib = 1024 * 1024
    return {"rss_mb": working / mib, "pss_mb": private / mib,
            "cpu_seconds": cpu if ticked else None, "pids": len(pids)}
