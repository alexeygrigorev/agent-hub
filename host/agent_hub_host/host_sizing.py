"""Resource-sized host concurrency for ``"max_agents": "auto"`` (intake 88ff6766).

The installer used to write ``max_agents: 1`` on every new host, so Windows
desktops with 32 GB advertised one slot. ``auto`` derives the advertised
ceiling from physical RAM and logical CPUs; runtime admission (MemAvailable
floor, worker memory caps, CPU pressure) still gates every individual launch.
"""
import os
from typing import Callable, Optional

AUTO = 'auto'
# Matches the launcher worker memory cap (1500 MiB) and keeps headroom for the
# OS, the desktop session and the controller itself.
WORKER_MB = 1536
RESERVED_MB = 4096
AGENTS_PER_CPU = 2
MAX_AGENTS = 100


def size_max_agents(total_mb: Optional[int], cpus: Optional[int]) -> int:
    """Deterministic ceiling: min(RAM after reserve / worker, CPUs * 2), clamped 1..100."""
    limits = []
    if type(total_mb) is int and total_mb > 0:
        limits.append((total_mb - RESERVED_MB) // WORKER_MB)
    if type(cpus) is int and cpus > 0:
        limits.append(cpus * AGENTS_PER_CPU)
    if not limits:
        return 1
    return max(1, min(MAX_AGENTS, min(limits)))


def auto_max_agents(*, memory_fn: Optional[Callable] = None,
                    cpu_count_fn: Callable[[], Optional[int]] = os.cpu_count) -> int:
    from .host_memory import read_host_memory
    try:
        memory = (memory_fn or read_host_memory)()
    except Exception:
        memory = None
    total_mb = memory['total_bytes'] // (1024 * 1024) if isinstance(memory, dict) else None
    try:
        cpus = cpu_count_fn()
    except Exception:
        cpus = None
    return size_max_agents(total_mb, cpus)
