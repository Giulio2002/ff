"""Running agents: Claude Code, Codex, GLM (Claude Code pointed at another endpoint), or a script.

Every run gets a directory under <state>/runs/<id>/ with the prompt, the raw transcript, the
result file and stderr; a row in the store; and a steering channel:

- kind=claude (Claude Code and GLM): the session is opened with --input-format stream-json, so
  messages sent to the run are written into the live session as new user turns. After each turn
  the runner delivers anything pending, and closes the session only when nothing is left.
- kind=codex: messages wait in the run's inbox (`ff inbox`, which the agent is told to check);
  what is still undelivered when the turn ends is sent by resuming the session
  (`codex exec resume <session> -`).
- kind=script: inbox only.

Stopping is a flag in the store (`stop:<run_id>`) that the watchdog polls, so `ff stop`, the
API and parent agents can stop runs owned by any process.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts
from .config import Config, Provider, Role
from .store import Store

FINAL = ("done", "blocked", "failed", "timeout", "stopped")


@dataclass
class RunResult:
    run_id: str
    status: str
    summary: str = ""
    result: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    transcript: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "done"


def new_run_id(role: str) -> str:
    return f"{role}-{time.strftime('%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def ff_bin(state_dir: Path) -> Path:
    """A tiny `ff` launcher on the agents' PATH that runs this very package."""
    d = state_dir / "bin"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "ff"
    pkg_root = Path(__file__).resolve().parent.parent
    body = f'#!/bin/sh\nPYTHONPATH="{pkg_root}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{sys.executable}" -m ff "$@"\n'
    if not f.exists() or f.read_text() != body:
        f.write_text(body)
        f.chmod(0o755)
    return d


def _last_json_object(text: str) -> dict:
    for m in reversed(list(re.finditer(r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}", text, re.S))):
        try:
            v = json.loads(m.group(0))
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            continue
    return {}


class Runner:
    def __init__(self, cfg: Config, store: Store):
        self.cfg, self.store = cfg, store
        self.slots = threading.BoundedSemaphore(max(1, cfg.limits.max_parallel_agents))
        self.runs_dir = cfg.project.state_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- public entry points

    def run(self, role_name: str, task: str, *, loop: str, worktree: Path, branch: str,
            parent: str | None = None, depth: int = 0, attempt: int = 1, result_extra: str = "",
            extra: dict | None = None, use_slot: bool = True, run_id: str | None = None) -> RunResult:
        role = self.cfg.role(role_name)
        run_id = run_id or new_run_id(role_name)
        if not self.store.run(run_id):
            self.store.run_start(run_id, parent=parent, loop=loop, role=role_name, provider=role.provider,
                                 model=self.cfg.model_for(role), task=task, branch=branch,
                                 worktree=str(worktree), attempt=attempt, status="queued")
        if use_slot:
            self.slots.acquire()
        try:
            return self._run(run_id, role, task, loop, worktree, branch, depth, result_extra, extra)
        finally:
            if use_slot:
                self.slots.release()

    def launch_detached(self, role_name: str, task: str, *, loop: str, worktree: Path, branch: str,
                        parent: str | None, depth: int) -> str:
        """Start a run in a background process (subagents, ad-hoc agents from the API/CLI)."""
        role = self.cfg.role(role_name)
        run_id = new_run_id(role_name)
        self.store.run_start(run_id, parent=parent, loop=loop, role=role_name, provider=role.provider,
                             model=self.cfg.model_for(role), task=task, branch=branch, worktree=str(worktree),
                             attempt=1, status="queued")
        d = self.runs_dir / run_id
        d.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, FF_DEPTH=str(depth))
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        p = subprocess.Popen([sys.executable, "-m", "ff", "--config", str(self.cfg.path), "_run-agent", run_id],
                             stdout=open(d / "launcher.log", "w"), stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        self.store.run_update(run_id, pid=p.pid)
        return run_id

    def execute_queued(self, run_id: str) -> RunResult:
        r = self.store.run(run_id)
        return self.run(r["role"], r["task"], loop=r["loop"], worktree=Path(r["worktree"]), branch=r["branch"],
                        parent=r["parent"], depth=int(os.environ.get("FF_DEPTH", "1")), use_slot=False,
                        run_id=run_id)

    def wait(self, run_id: str, timeout: float | None = None, poll: float = 2.0) -> RunResult:
        t0 = time.time()
        while True:
            r = self.store.run(run_id)
            if r is None:
                raise KeyError(run_id)
            if r["status"] in FINAL:
                return RunResult(run_id, r["status"], r["summary"] or "", json.loads(r["result"] or "{}"),
                                 json.loads(r["usage"] or "{}"), r["transcript"] or "")
            if timeout is not None and time.time() - t0 > timeout:
                return RunResult(run_id, "running")
            time.sleep(poll)

    def stop(self, run_id: str, cascade: bool = True) -> list[str]:
        ids = [run_id] + ([k["id"] for k in self.store.children(run_id, True)] if cascade else [])
        for i in ids:
            self.store.set_flag(f"stop:{i}", "1")
        self.store.event("steer", "stop", f"stop requested for {', '.join(ids)}", run_id)
        return ids

    def steer(self, run_id: str, text: str, sender: str = "api", cascade: bool = False) -> list[str]:
        ids = [run_id] + ([k["id"] for k in self.store.children(run_id, True)
                           if k["status"] not in FINAL] if cascade else [])
        for i in ids:
            self.store.send(i, text if i == run_id else f"(forwarded from {sender} via {run_id}) {text}", sender)
        return ids

    # ---------------------------------------------------------------- the run itself

    def _run(self, run_id: str, role: Role, task: str, loop: str, worktree: Path, branch: str, depth: int,
             result_extra: str, extra: dict | None) -> RunResult:
        prov = self.cfg.provider_for(role)
        model = self.cfg.model_for(role)
        d = self.runs_dir / run_id
        d.mkdir(parents=True, exist_ok=True)
        result_file = d / "result.json"
        result_file.unlink(missing_ok=True)
        system, user = prompts.build(self.cfg, role, task, worktree=str(worktree), branch=branch,
                                     result_file=str(result_file), result_extra=result_extra, extra=extra)
        (d / "system.md").write_text(system)
        (d / "prompt.md").write_text(user)
        transcript = d / "transcript.jsonl"
        env = dict(os.environ)
        env.update(prov.env)
        env.update(role.env)
        env.update(FF_CONFIG=str(self.cfg.path), FF_RUN_ID=run_id, FF_ROLE=role.name, FF_DEPTH=str(depth),
                   FF_WORKTREE=str(worktree), FF_BRANCH=branch, FF_RESULT_FILE=str(result_file),
                   FF_STATE=str(self.cfg.project.state_dir))
        env["PATH"] = str(ff_bin(self.cfg.project.state_dir)) + os.pathsep + env.get("PATH", "")
        self.store.run_update(run_id, status="running", started=time.time(), transcript=str(transcript))
        self.store.event(loop, "agent-start", f"{role.name} started ({prov.name}/{model}): {task.splitlines()[0][:160]}",
                         run_id)
        deadline = time.time() + role.timeout_minutes * 60
        try:
            if prov.kind == "claude":
                status, final_text, usage = self._claude(run_id, prov, role, model, system, user, worktree, env,
                                                         transcript, d, deadline)
            elif prov.kind == "codex":
                status, final_text, usage = self._codex(run_id, prov, role, model, system, user, worktree, env,
                                                        transcript, d, deadline)
            else:
                status, final_text, usage = self._script(run_id, prov, role, system, user, worktree, env,
                                                         transcript, d, deadline, result_file)
        except Exception as e:  # a launcher failure must not take the loop down
            status, final_text, usage = "failed", f"runner error: {e!r}", {}
        result = {}
        if result_file.exists():
            try:
                result = json.loads(result_file.read_text())
            except json.JSONDecodeError:
                result = {}
        if not result:
            result = _last_json_object(final_text or "")
        if status == "exited":
            status = result.get("status") if result.get("status") in ("done", "blocked") else (
                "done" if result else "failed")
        prev = json.loads((self.store.run(run_id) or {"result": None})["result"] or "{}")
        if prev.get("session_id"):
            result.setdefault("session_id", prev["session_id"])
        summary = str(result.get("summary") or (final_text or "")[-1500:]).strip()
        self.store.run_update(run_id, status=status, ended=time.time(), summary=summary,
                              result=result, usage=usage)
        self.store.set_flag(f"stop:{run_id}", None)
        self.store.event(loop, "agent-end", f"{role.name} {status}: {summary[:300]}", run_id,
                         cost_usd=usage.get("cost_usd"))
        return RunResult(run_id, status, summary, result, usage, str(transcript))

    def _spawn(self, argv: list[str], cwd: Path, env: dict, d: Path, stdin=subprocess.PIPE) -> subprocess.Popen:
        nice = self.cfg.limits.nice
        return subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE,
                                stderr=open(d / "stderr.log", "a"), text=True, bufsize=1,
                                start_new_session=True, preexec_fn=(lambda: os.nice(nice)) if nice else None)

    def _watchdog(self, run_id: str, p: subprocess.Popen, deadline: float, state: dict) -> threading.Thread:
        def watch():
            while p.poll() is None:
                if time.time() > deadline:
                    state["killed"] = "timeout"
                elif self.store.flag(f"stop:{run_id}") == "1":
                    state["killed"] = "stopped"
                if state.get("killed"):
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                        time.sleep(5)
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return
                time.sleep(2)
        t = threading.Thread(target=watch, daemon=True)
        t.start()
        return t

    # ---- Claude Code (and GLM through Claude Code)

    def _claude(self, run_id, prov: Provider, role: Role, model, system, user, worktree, env, transcript, d, deadline):
        argv = [prov.binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--permission-mode", prov.permission_mode, "--append-system-prompt", system]
        if model:
            argv += ["--model", model]
        argv += prov.args + role.args
        p = self._spawn(argv, worktree, env, d)
        self.store.run_update(run_id, pid=p.pid)
        state: dict = {}
        lock = threading.Lock()
        idle = threading.Event()    # set after a turn ended with nothing pending

        def send(text: str) -> bool:
            msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
            with lock:
                if p.stdin is None or p.stdin.closed:
                    return False
                try:
                    p.stdin.write(json.dumps(msg) + "\n")
                    p.stdin.flush()
                    return True
                except (BrokenPipeError, ValueError):
                    return False

        def deliver_pending() -> bool:
            msgs = self.store.pending(run_id)
            if not msgs:
                return False
            text = "\n\n".join(f"[message from {m['sender']}] {m['text']}" for m in msgs)
            if send(text):
                self.store.mark_delivered([m["id"] for m in msgs], "stdin")
                return True
            return False

        def poll_inbox():
            while p.poll() is None and not idle.is_set():
                deliver_pending()
                time.sleep(2)

        send(user)
        self._watchdog(run_id, p, deadline, state)
        threading.Thread(target=poll_inbox, daemon=True).start()
        final_text, usage = "", {}
        with transcript.open("a") as tf:
            for line in p.stdout:
                tf.write(line)
                tf.flush()
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "system" and ev.get("session_id"):
                    self.store.run_update(run_id, result={"session_id": ev["session_id"]})
                if ev.get("type") == "result":
                    final_text = ev.get("result") or final_text
                    u = ev.get("usage") or {}
                    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                        usage[k] = usage.get(k, 0) + int(u.get(k) or 0)
                    usage["cost_usd"] = round(usage.get("cost_usd", 0) + float(ev.get("total_cost_usd") or 0), 4)
                    usage["turns"] = usage.get("turns", 0) + int(ev.get("num_turns") or 0)
                    # a turn ended: continue with pending messages, otherwise end the session
                    if not deliver_pending():
                        idle.set()
                        with lock:
                            try:
                                p.stdin.close()
                            except Exception:
                                pass
        p.wait()
        if state.get("killed"):
            return state["killed"], final_text, usage
        return ("exited" if p.returncode == 0 or final_text else "failed"), final_text, usage

    # ---- Codex

    def _codex(self, run_id, prov: Provider, role: Role, model, system, user, worktree, env, transcript, d, deadline):
        last = d / "last_message.txt"
        perm = (["--dangerously-bypass-approvals-and-sandbox"] if prov.sandbox == "danger-full-access"
                else ["--sandbox", prov.sandbox])
        common = ["--json", "--skip-git-repo-check", "-o", str(last)] + (["-m", model] if model else [])
        argv = [prov.binary, "exec", *common, "-C", str(worktree), *perm, *prov.args, *role.args, "-"]
        usage: dict = {}
        thread_id = None
        text = system + "\n\n" + user
        status = "exited"
        while True:
            p = self._spawn(argv, worktree, env, d)
            self.store.run_update(run_id, pid=p.pid)
            p.stdin.write(text)
            p.stdin.close()
            state: dict = {}
            self._watchdog(run_id, p, deadline, state)
            with transcript.open("a") as tf:
                for line in p.stdout:
                    tf.write(line)
                    tf.flush()
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "thread.started" and ev.get("thread_id"):
                        thread_id = ev["thread_id"]
                        self.store.run_update(run_id, result={"session_id": thread_id})
                    if ev.get("type") == "turn.completed":
                        for k, v in (ev.get("usage") or {}).items():
                            if isinstance(v, (int, float)):
                                usage[k] = usage.get(k, 0) + v
            p.wait()
            if state.get("killed"):
                status = state["killed"]
                break
            if p.returncode != 0:
                status = "failed"
                break
            msgs = self.store.pending(run_id)
            if not msgs or not thread_id or time.time() > deadline:
                break
            # steering that arrived after the turn: resume the same session with it
            text = "\n\n".join(f"[message from {m['sender']}] {m['text']}" for m in msgs)
            self.store.mark_delivered([m["id"] for m in msgs], "resume")
            argv = [prov.binary, "exec", "resume", thread_id, *common, *perm, "-"]
        final_text = last.read_text() if last.exists() else ""
        return status, final_text, usage

    # ---- any command

    def _script(self, run_id, prov: Provider, role: Role, system, user, worktree, env, transcript, d, deadline,
                result_file):
        prompt_file = d / "full_prompt.md"
        prompt_file.write_text(system + "\n\n" + user)
        argv = [a.format(prompt_file=prompt_file, workdir=worktree, result_file=result_file, run_id=run_id,
                         role=role.name) for a in prov.command]
        p = self._spawn(argv, worktree, env, d)
        self.store.run_update(run_id, pid=p.pid)
        p.stdin.write(system + "\n\n" + user)
        p.stdin.close()
        state: dict = {}
        self._watchdog(run_id, p, deadline, state)
        out = []
        with transcript.open("a") as tf:
            for line in p.stdout:
                tf.write(line)
                out.append(line)
        p.wait()
        if state.get("killed"):
            return state["killed"], "".join(out), {}
        return ("exited" if p.returncode == 0 else "failed"), "".join(out), {}
