"""Waiting for a quiet machine before timed work.

The gate's checker has a per-file time limit and the benchmark measures speed; both read wall-clock
time, so whatever else runs on the machine (agents building, another factory's gate, unrelated jobs)
stretches them. Before either starts, the factory waits until the CPUs are mostly idle."""
from __future__ import annotations

import os
import time


def _cpu_times() -> tuple[int, int]:
    """(busy, total) jiffies over all CPUs since boot, from /proc/stat."""
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)     # idle + iowait
    total = sum(vals[:8])                                  # guest time is already in user/nice
    return total - idle, total


def busy_pct(sample_seconds: float = 5.0) -> float:
    """The share of all logical CPUs that was busy over the next `sample_seconds`, in percent."""
    b0, t0 = _cpu_times()
    time.sleep(sample_seconds)
    b1, t1 = _cpu_times()
    return 100.0 * (b1 - b0) / max(1, t1 - t0)


def wait_quiet(max_busy_pct: float, max_wait_seconds: float, sample_seconds: float = 10.0,
               stop=None) -> tuple[bool, float, float]:
    """Block until the machine's CPU use stays at or below `max_busy_pct` for one sample, or
    `max_wait_seconds` pass, or `stop()` is true. Returns (quiet, seconds waited, last busy %).
    `max_busy_pct <= 0` (or no /proc/stat) turns the wait off."""
    if max_busy_pct <= 0 or not os.path.exists("/proc/stat"):
        return True, 0.0, 0.0
    t0 = time.time()
    while True:
        pct = busy_pct(sample_seconds)
        waited = time.time() - t0
        if pct <= max_busy_pct:
            return True, waited, pct
        if waited >= max_wait_seconds or (stop is not None and stop()):
            return False, waited, pct
