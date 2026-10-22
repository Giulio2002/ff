"""Waiting for a quiet machine before timed work.

The gate's checker has a per-file time limit and the benchmark measures speed; both read wall-clock
time, so whatever else runs on the machine (agents building, another factory's gate, unrelated jobs)
stretches them. Timed work holds one machine-wide lock, pauses every agent while it runs, and first
waits until the CPUs are mostly idle."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import time

# one holder at a time across every factory on the machine: a gate's checks or one benchmark run
TIMED_LOCK = os.environ.get("FF_TIMED_LOCK", "/var/tmp/ff-timed.lock")
# the agent processes frozen for timed work, so a later holder can resume them if this one died
FROZEN_FILE = TIMED_LOCK + ".frozen"
FREEZE_MAX_SECONDS = 90 * 60     # a watchdog resumes everything after this, whatever happens to us


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


def _ppid(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def _ours() -> set[int]:
    """This process and its ancestors: never frozen (timed work itself carries no FF_RUN_ID, but it
    could run under an agent)."""
    keep, p = {os.getpid()}, os.getpid()
    while p > 1:
        p = _ppid(p)
        keep.add(p)
    return keep


def agent_pids() -> list[int]:
    """Every process on the machine that belongs to a factory agent (FF_RUN_ID in its environment;
    an agent's children inherit it), except this process and its ancestors."""
    ours, out = _ours(), []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) in ours:
            continue
        try:
            with open(f"/proc/{d}/environ", "rb") as f:
                if b"\0FF_RUN_ID=" in b"\0" + f.read():
                    out.append(int(d))
        except OSError:
            pass
    return out


def _signal(pids, sig) -> None:
    for p in pids:
        try:
            os.kill(p, sig)
        except (ProcessLookupError, PermissionError):
            pass


def resume_frozen() -> None:
    """Resume the agents a previous holder froze (it may have died before resuming them)."""
    try:
        pids = json.loads(open(FROZEN_FILE).read())
    except (OSError, ValueError):
        return
    _signal(pids, signal.SIGCONT)
    with contextlib.suppress(OSError):
        os.unlink(FROZEN_FILE)


@contextlib.contextmanager
def frozen_agents(enabled: bool = True):
    """Stop (SIGSTOP) every agent process on the machine for the duration, then continue them."""
    if not enabled:
        yield []
        return
    frozen: list[int] = []
    for _ in range(3):              # a process forked between the scan and the stop is caught next pass
        new = [p for p in agent_pids() if p not in frozen]
        if not new:
            break
        _signal(new, signal.SIGSTOP)
        frozen += new
    with open(FROZEN_FILE, "w") as f:
        f.write(json.dumps(frozen))
    watchdog = subprocess.Popen(["sh", "-c", f"sleep {FREEZE_MAX_SECONDS}; kill -CONT {' '.join(map(str, frozen)) or '0'}"
                                 " 2>/dev/null"], start_new_session=True, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL) if frozen else None
    try:
        yield frozen
    finally:
        _signal(frozen, signal.SIGCONT)
        _signal([p for p in agent_pids() if p not in frozen], signal.SIGCONT)   # anything stopped meanwhile
        with contextlib.suppress(OSError):
            os.unlink(FROZEN_FILE)
        if watchdog:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(watchdog.pid, signal.SIGKILL)


@contextlib.contextmanager
def timed_work(pause_agents: bool = True):
    """Hold the machine-wide lock for timed work (the gate's checks, a benchmark run), so two of them
    (from this factory or another) never overlap. While it is held every agent on the machine is
    paused (pause_agents); the quiet wait then covers everything else."""
    with open(TIMED_LOCK, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            resume_frozen()
            with frozen_agents(pause_agents):
                yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
