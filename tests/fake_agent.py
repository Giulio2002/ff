#!/usr/bin/env python3
"""A deterministic stand-in for an agent CLI (provider kind: script), driven by FF_ROLE and the
task text. It does what the real roles do, in miniature, on the toy Bend project of the tests."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

role = os.environ["FF_ROLE"]
prompt = sys.stdin.read()
wt = Path(os.environ["FF_WORKTREE"])
result_file = Path(os.environ["FF_RESULT_FILE"])
task = prompt.split("# Your task", 1)[-1]


def sh(*a, check=True):
    return subprocess.run(list(a), cwd=wt, check=check, capture_output=True, text=True).stdout


def commit(msg):
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=fake", "-c", "user.email=f@x", "commit", "-q", "-m", msg)


def done(**kw):
    kw.setdefault("status", "done")
    result_file.write_text(json.dumps(kw))


def wait_inbox(timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        out = sh("ff", "inbox")
        if "no new messages" not in out:
            return " ".join(re.findall(r"^\d+\. from (\S+): (.*)$", out, re.M)[0]) if re.search(r"^\d+\. from", out, re.M) else out.strip()
        time.sleep(1)
    return ""


if role == "implementer":
    if "CHEAT-WEAKEN" in task:          # weaken a frozen law: the gate must refuse
        p = wt / "proofs/laws.bend"
        p.write_text(p.read_text().replace("{I.add(a,b) == S.spec_add(a,b) : Nat}", "{I.add(a,b) == I.add(a,b) : Nat}"))
        commit("make the law easier")
    elif "CHEAT-HANDEDIT" in task:      # edit a generated file by hand: the gate must refuse
        p = wt / "src/add.bend"
        p.write_text(p.read_text().replace("Nat.add(b,a)", "Nat.add(a,b)"))
        commit("hand edit")
    else:                               # the honest fix: in the generator
        g = wt / "gen.py"
        g.write_text(g.read_text().replace("Nat.add(b,a)", "Nat.add(a,b)"))
        sh(sys.executable, "gen.py")
        (wt / "TODO.txt").write_text("")
        commit("generator: add in the right order")
    done(summary="did " + role)
elif role.startswith("auditor_"):
    flavor = role.split("_", 1)[1]
    done(summary=f"{flavor} audit", findings=[
        {"title": f"{flavor}: missing law add_comm_spec", "severity": "critical",
         "description": "no law ties add to spec in the other order", "reproducer": "add(1,2) vs spec(2,1)"},
        {"title": f"{flavor}: hand-built object", "severity": "low",
         "description": "only with an object the API never returns", "reproducer": "internal constructor"}])
elif role == "judge":
    ids = [int(x) for x in re.findall(r"## Finding (\d+)", task)]
    titles = re.findall(r"## Finding \d+ \([^)]*\): (.*)", task)
    done(summary="judged", verdicts=[{"id": i, "reachable": "hand-built" not in t, "severity": "critical" if "hand-built" not in t else "low",
                                      "reason": "via the API" if "hand-built" not in t else "not constructible"}
                                     for i, t in zip(ids, titles)])
elif role == "fixer":
    n = re.search(r"Finding (\d+)", task).group(1)
    if "# Rebase conflict" in task:     # resolve by redoing the change on top of the new main
        sh("git", "reset", "-q", "--hard", "main")
    laws = wt / "proofs/laws.bend"
    laws.write_text(laws.read_text() + f"\nlaw add_again_{n}:\n  for +a: Nat\n  for +b: Nat\n  {{I.add(a,b) == Nat.add(a,b) : Nat}}\n")
    g = wt / "gen.py"
    g.write_text(g.read_text().replace("EXTRA = []", f"EXTRA = []\nEXTRA.append('add_again_{n}')", 1))
    sh(sys.executable, "gen.py")
    commit(f"law for finding {n}")
    done(summary=f"added law add_again_{n}")
elif role == "steerable":                # waits for a message, optionally relays to a subagent
    if "DELEGATE" in task:
        child = sh("ff", "subagent", "start", "echo", "wait for my message and echo it").strip()
        msg = wait_inbox()
        sh("ff", "subagent", "steer", child, "relay: " + msg)
        out = json.loads(sh("ff", "subagent", "wait", child))
        done(summary="relayed", child=child, child_summary=out["summary"])
    else:
        done(summary="got: " + wait_inbox())
elif role == "echo":
    done(summary="echo: " + wait_inbox())
else:
    done(summary="nothing to do for " + role)
