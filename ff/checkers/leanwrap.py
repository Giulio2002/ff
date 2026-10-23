"""`lean` as Lake runs it during the checker's build: the real `lean`, timed per file.

Installed as bin/lean of a sysroot that links everything else to the real toolchain (Lake takes it from
LAKE_OVERRIDE_LEAN + LEAN_SYSROOT), so one `lake build` both builds the tree and times every module,
instead of a build followed by a second elaboration of every file. At most FF_LEAN_JOBS files elaborate
at once (the per-file budget is wall-clock time); each is killed past FF_LEAN_TIMEOUT seconds. One JSON
line per file goes to FF_LEAN_TIMES."""
import fcntl
import json
import os
import subprocess
import sys
import time


def main() -> int:
    real, args = os.environ["FF_REAL_LEAN"], sys.argv[1:]
    src = next((a for a in args if a.endswith(".lean")), None)
    if src is None or not os.environ.get("FF_LEAN_TIMES"):
        os.execv(real, [real, *args])
    slots, jobs = os.environ["FF_LEAN_SLOTS"], max(1, int(os.environ.get("FF_LEAN_JOBS", "1")))
    held = None
    while held is None:
        for i in range(jobs):
            f = open(os.path.join(slots, f"slot{i}"), "a")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = f
                break
            except BlockingIOError:
                f.close()
        else:
            time.sleep(0.05)
    t0 = time.monotonic()
    p = subprocess.Popen([real, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         errors="replace", start_new_session=True)
    timed_out = False
    try:
        out, _ = p.communicate(timeout=float(os.environ.get("FF_LEAN_TIMEOUT", "0")) or None)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        out, _ = p.communicate()
        timed_out = True
    secs = time.monotonic() - t0
    held.close()
    rec = {"file": os.path.realpath(src), "seconds": secs, "code": p.returncode, "timed_out": timed_out,
           "output": out[-6000:]}
    with open(os.environ["FF_LEAN_TIMES"], "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(rec) + "\n")
    sys.stdout.write(out)
    return 124 if timed_out else p.returncode


if __name__ == "__main__":
    sys.exit(main())
