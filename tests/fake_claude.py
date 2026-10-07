#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json --output-format stream-json`.

Sessions live in $CLAUDE_CONFIG_DIR/projects/fake/<session>.jsonl, like the real CLI's. If the
account directory contains a file named EXHAUSTED, the first turn ends with Claude Code's
usage-limit error (reset time: the number in the file). Otherwise it writes $FF_RESULT_FILE with the
account it ran on and whether it resumed an existing session."""
import json
import os
import sys
import uuid
from pathlib import Path

args = sys.argv[1:]
cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
resume = args[args.index("--resume") + 1] if "--resume" in args else None
sid = resume or str(uuid.uuid4())
sess = cfg / "projects" / "fake" / f"{sid}.jsonl"
resumed = bool(resume) and sess.exists()
sess.parent.mkdir(parents=True, exist_ok=True)


def emit(ev):
    print(json.dumps(ev), flush=True)


first = sys.stdin.readline()
with sess.open("a") as f:
    f.write(first)
emit({"type": "system", "subtype": "init", "session_id": sid})
if "BACKGROUND-WAKE" in first:
    # like Claude Code: end the turn while a background command runs, wake with a new turn when it ends
    import subprocess as sp
    import time as tm
    bg = sp.Popen(["sleep", "4"])
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "waiting for my background job",
          "session_id": sid, "total_cost_usd": 0, "usage": {}})
    bg.wait()
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "background job finished"}]}})
    Path(os.environ["FF_RESULT_FILE"]).write_text(json.dumps({"status": "done", "summary": "woke up and finished",
                                                              "account": cfg.name, "resumed": resumed, "first_message": "x"}))
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": sid,
          "total_cost_usd": 0, "usage": {}})
    sys.exit(0)
if "LEFTOVER-JOB" in first:
    # finish (result file written) but leave a background job that never ends, then wait for stdin
    import subprocess as sp
    bg = sp.Popen(["sleep", "1000"])
    (Path(os.environ["FF_RESULT_FILE"]).parent / "leftover.pid").write_text(str(bg.pid))
    Path(os.environ["FF_RESULT_FILE"]).write_text(json.dumps({"status": "done", "summary": "finished, job left"}))
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": sid,
          "total_cost_usd": 0, "usage": {}})
    sys.stdin.read()      # the CLI ends when its input closes
    sys.exit(0)
if (cfg / "EXHAUSTED").exists():
    reset = (cfg / "EXHAUSTED").read_text().strip()
    emit({"type": "assistant", "message": {"model": "<synthetic>", "content": [
        {"type": "text", "text": f"Claude AI usage limit reached|{reset}"}]}})
    emit({"type": "result", "subtype": "success", "is_error": True, "result": f"Claude AI usage limit reached|{reset}",
          "session_id": sid, "total_cost_usd": 0, "usage": {}})
    sys.exit(1)
Path(os.environ["FF_RESULT_FILE"]).write_text(json.dumps({
    "status": "done", "summary": f"ran on {cfg.name}", "account": cfg.name, "resumed": resumed,
    "first_message": json.loads(first)["message"]["content"][0]["text"][:80]}))
emit({"type": "result", "subtype": "success", "is_error": False, "result": "ok", "session_id": sid,
      "total_cost_usd": 0.01, "num_turns": 1, "usage": {"output_tokens": 5}})
