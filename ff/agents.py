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
from .accounts import Pool, is_limit, parse_reset
from .config import Config, Provider, Role
from .store import Store

FINAL = ("done", "blocked", "failed", "timeout", "stopped")

# a provider with any of these in its env brings its own credentials and never uses the account pool
OWN_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY",
                   "CLAUDE_CONFIG_DIR", "CODEX_HOME")

CONTINUE = ("You were moved to another account after the previous one hit its usage limit. This is the same "
            "session: continue exactly where you stopped.")


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _children(pid: int) -> list[int]:
    """Live child processes of pid (Linux /proc): an agent CLI's background commands."""
    out = []
    try:
        for t in os.listdir(f"/proc/{pid}/task"):
            with open(f"/proc/{pid}/task/{t}/children") as fh:
                out += [int(x) for x in fh.read().split()]
    except OSError:
        return []
    alive = []
    for c in out:
        try:
            with open(f"/proc/{c}/stat") as fh:
                if fh.read().split(")")[-1].split()[0] != "Z":
                    alive.append(c)
        except OSError:
            pass
    return alive


def _claude_limit(ev: dict) -> str | None:
    """A usage-limit report from Claude Code: an error result, or the synthetic assistant message it
    emits for API errors. Tool output is never inspected (it could quote anything)."""
    if ev.get("type") == "result" and (ev.get("is_error") or str(ev.get("subtype", "")).startswith("error")):
        text = str(ev.get("result") or "") + " " + json.dumps(ev.get("errors") or "")
        return text.strip() if is_limit(text) else None
    if ev.get("type") == "assistant":
        msg = ev.get("message") or {}
        if msg.get("model") == "<synthetic>" or ev.get("error") or msg.get("error"):
            text = " ".join(c.get("text", "") for c in msg.get("content", []) if isinstance(c, dict))
            return text.strip() if is_limit(text) else None
    if ev.get("type") in ("rate_limit_event", "rate_limit") and str(ev.get("status", ev.get("rate_limit_info", ""))).lower() in ("rejected", "exceeded"):
        return json.dumps(ev)
    return None


def _resume_perm(perm: list[str]) -> list[str]:
    """`codex exec resume` takes no --sandbox flag: carry the sandbox over as config."""
    out, i = [], 0
    while i < len(perm):
        if perm[i] == "--sandbox":
            out += ["-c", f"sandbox_mode={json.dumps(perm[i + 1])}"]
            i += 2
        else:
            out.append(perm[i])
            i += 1
    return out


def _codex_limit(ev: dict) -> str | None:
    if ev.get("type") in ("error", "turn.failed"):
        text = str(ev.get("message") or (ev.get("error") or {}).get("message") or ev)
        return text if is_limit(text) else None
    return None


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
        self.pool = Pool()
        self.runs_dir = cfg.project.state_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- public entry points

    def run(self, role_name: str, task: str, *, loop: str, worktree: Path, branch: str,
            parent: str | None = None, depth: int = 0, attempt: int = 1, result_extra: str = "",
            extra: dict | None = None, use_slot: bool = True, run_id: str | None = None,
            detached: bool = False, cycle: dict | None = None, resume_session: str | None = None) -> RunResult:
        """Run an agent and return its result. detached=True runs it in a process of its own (the
        loops do), so it outlives a daemon restart and the next daemon can adopt it."""
        role = self.cfg.role(role_name)
        run_id = run_id or new_run_id(role_name)
        if not self.store.run(run_id):
            self.store.run_start(run_id, parent=parent, loop=loop, role=role_name, provider=role.provider,
                                 model=self.cfg.model_for(role), task=task, branch=branch,
                                 worktree=str(worktree), attempt=attempt, status="queued",
                                 extra={"result_extra": result_extra, "extra": extra or {}}, cycle=cycle or {})
        if use_slot:
            self.slots.acquire()
        try:
            if detached:
                self._spawn_runner(run_id, depth)
                return self.wait(run_id)
            return self._run(run_id, role, task, loop, worktree, branch, depth, result_extra, extra,
                             resume_session=resume_session)
        finally:
            if use_slot:
                self.slots.release()

    def _spawn_runner(self, run_id: str, depth: int) -> int:
        d = self.runs_dir / run_id
        d.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, FF_DEPTH=str(depth))
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        p = subprocess.Popen([sys.executable, "-m", "ff", "--config", str(self.cfg.path), "_run-agent", run_id],
                             stdout=open(d / "launcher.log", "a"), stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        self.store.run_update(run_id, pid=p.pid)
        return p.pid

    def launch_detached(self, role_name: str, task: str, *, loop: str, worktree: Path, branch: str,
                        parent: str | None, depth: int) -> str:
        """Start a run in a background process (subagents, ad-hoc agents from the API/CLI)."""
        role = self.cfg.role(role_name)
        run_id = new_run_id(role_name)
        self.store.run_start(run_id, parent=parent, loop=loop, role=role_name, provider=role.provider,
                             model=self.cfg.model_for(role), task=task, branch=branch, worktree=str(worktree),
                             attempt=1, status="queued")
        self._spawn_runner(run_id, depth)
        return run_id

    def execute_queued(self, run_id: str) -> RunResult:
        r = self.store.run(run_id)
        ex = json.loads(r["extra"] or "{}")
        return self.run(r["role"], r["task"], loop=r["loop"], worktree=Path(r["worktree"]), branch=r["branch"],
                        parent=r["parent"], depth=int(os.environ.get("FF_DEPTH", "1")), use_slot=False,
                        run_id=run_id, attempt=r["attempt"] or 1, result_extra=ex.get("result_extra", ""),
                        extra=ex.get("extra") or None, resume_session=ex.get("resume_session"))

    def wait(self, run_id: str, timeout: float | None = None, poll: float = 2.0) -> RunResult:
        t0 = time.time()
        gone_since = None
        while True:
            r = self.store.run(run_id)
            if r is None:
                raise KeyError(run_id)
            if r["status"] in FINAL:
                return RunResult(run_id, r["status"], r["summary"] or "", json.loads(r["result"] or "{}"),
                                 json.loads(r["usage"] or "{}"), r["transcript"] or "")
            # a process that died without recording its end must not be waited for forever
            alive = True
            if r["pid"]:
                try:
                    os.kill(r["pid"], 0)
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    pass
            if alive:
                gone_since = None
            elif gone_since is None:
                gone_since = time.time()
            elif time.time() - gone_since > 60:
                self.store.run_update(run_id, status="failed", ended=time.time(),
                                      summary=(r["summary"] or "") + " [its process died without a result]")
                continue
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
        """Message a run. A running agent gets it in its session; a finished one (done or blocked) is
        continued: a new run resumes the same session in the same worktree with the message as its
        next turn (its id is returned in place of the finished one)."""
        r = self.store.run(run_id)
        if r is not None and r["status"] in ("done", "blocked"):
            new = self.continue_run(run_id, text, sender)
            if new:
                return [new]
        ids = [run_id] + ([k["id"] for k in self.store.children(run_id, True)
                           if k["status"] not in FINAL] if cascade else [])
        for i in ids:
            self.store.send(i, text if i == run_id else f"(forwarded from {sender} via {run_id}) {text}", sender)
        return ids

    def continue_run(self, run_id: str, text: str, sender: str = "api") -> str | None:
        """Resume a finished run's session as a new run (same role, worktree, branch and parent)."""
        r = self.store.run(run_id)
        sid = json.loads(r["result"] or "{}").get("session_id")
        prov = self.cfg.providers.get(r["provider"])
        if not sid or prov is None or prov.kind not in ("claude", "codex"):
            return None
        new = new_run_id(r["role"])
        msg = f"[message from {sender}] {text}"
        self.store.run_start(new, parent=r["parent"], loop=r["loop"], role=r["role"], provider=r["provider"],
                             model=r["model"], task=msg, branch=r["branch"], worktree=r["worktree"], attempt=1,
                             status="queued", extra={"resume_session": sid, "continue_of": run_id})
        self.store.event(r["loop"] or "steer", "continued", f"{run_id} had finished; {new} resumes its session "
                         f"with the message from {sender}", new)
        self._spawn_runner(new, int(os.environ.get("FF_DEPTH", "1")))
        return new

    # ---------------------------------------------------------------- the run itself

    def _run(self, run_id: str, role: Role, task: str, loop: str, worktree: Path, branch: str, depth: int,
             result_extra: str, extra: dict | None, resume_session: str | None = None) -> RunResult:
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
        self.store.run_update(run_id, status="running", started=time.time(), transcript=str(transcript),
                              pid=os.getpid())
        self.store.event(loop, "agent-start", f"{role.name} started ({prov.name}/{model}): {task.splitlines()[0][:160]}",
                         run_id)
        deadline = time.time() + role.timeout_minutes * 60
        try:
            if prov.kind in ("claude", "codex"):
                status, final_text, usage = self._rotating(run_id, loop, prov, role, model, system, user, worktree,
                                                           env, transcript, d, deadline,
                                                           resume_session=resume_session,
                                                           resume_text=task if resume_session else None)
            else:
                status, final_text, usage = self._script(run_id, prov, role, system, user, worktree, env,
                                                         transcript, d, deadline, result_file)
        except Exception as e:  # a launcher failure must not take the loop down
            status, final_text, usage = "failed", f"runner error: {e!r}", {}
        launch_error = None
        if status == "failed":
            err = (d / "stderr.log").read_text(errors="replace").strip() if (d / "stderr.log").exists() else ""
            started_work = transcript.exists() and transcript.stat().st_size > 0
            if not started_work or final_text.startswith("runner error"):
                launch_error = (final_text if final_text.startswith("runner error") else "") or err[-2000:] or \
                    "the agent CLI exited before producing any output"
            if not final_text and err:
                final_text = "stderr: " + err[-1500:]
        result = {}
        if result_file.exists():
            try:
                result = json.loads(result_file.read_text())
            except json.JSONDecodeError:
                result = {}
        if not result:
            result = _last_json_object(final_text or "")
        if status == "exited":
            # The agent ended its session normally. Its own verdict wins; with no result file, a final
            # message still means it finished (whether the work is any good is the gate's call).
            if result.get("status") in ("done", "blocked"):
                status = result["status"]
            else:
                status = "done" if (result or (final_text or "").strip()) else "failed"
        if launch_error:
            result["launch_error"] = launch_error
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

    # ---- subscription rotation around the Claude Code / Codex launchers

    def _pool_for(self, prov: Provider) -> tuple[str, list[str] | None] | None:
        """(kind, allowed names) when this provider draws from the account pool, else None."""
        acc = prov.accounts
        if acc in (None, "none", False, []):
            return None
        if acc == "auto" and any(k in prov.env for k in OWN_CREDENTIALS):
            return None   # e.g. GLM: its own endpoint and token, never a Claude subscription
        return prov.kind, (list(acc) if isinstance(acc, list) else None)

    def _rotating(self, run_id, loop, prov: Provider, role: Role, model, system, user, worktree, env, transcript,
                  d, deadline, resume_session: str | None = None, resume_text: str | None = None):
        launch = self._claude if prov.kind == "claude" else self._codex
        spec = self._pool_for(prov)
        acct = None
        if spec:
            acct = self.pool.acquire(spec[0], spec[1], strategy=self.cfg.accounts.strategy,
                                     stop=lambda: self.store.flag(f"stop:{run_id}") == "1",
                                     on_wait=lambda n, t: self.store.event(
                                         loop, "accounts-exhausted", f"every {prov.kind} account is cooling down; "
                                         f"waiting for {n} (until {time.strftime('%H:%M', time.localtime(t))})", run_id))
        total: dict = {}
        resume = resume_session
        switches = 0
        while True:
            run_env = dict(env, **(acct.env() if acct else {}))
            if acct:
                self.store.run_update(run_id, account=acct.name)
            info: dict = {"resume_text": resume_text} if resume_text else {}
            status, final_text, usage = launch(run_id, prov, role, model, system, user, worktree, run_env,
                                               transcript, d, deadline, resume=resume, info=info)
            resume_text = None
            for k, v in usage.items():
                total[k] = round(total.get(k, 0) + v, 4) if isinstance(v, float) else total.get(k, 0) + v
            if acct:
                self.pool.release(acct, usage)
            if status != "limited":
                if switches:
                    total["account_switches"] = switches
                return status, final_text, total
            # out of credits: cool this account down, move the session to the next one, resume there
            reason = info.get("limit", "usage limit")
            if not acct:
                self.store.event(loop, "limit", f"{prov.kind} usage limit and no account pool to rotate to: {reason[:200]}",
                                 run_id)
                return "failed", final_text or reason, total
            until = self.pool.cooldown(acct, parse_reset(reason), reason,
                                       self.cfg.accounts.default_cooldown_minutes * 60)
            nxt = self.pool.acquire(spec[0], spec[1], strategy=self.cfg.accounts.strategy,
                                    stop=lambda: self.store.flag(f"stop:{run_id}") == "1",
                                    on_wait=lambda n, t: self.store.event(
                                        loop, "accounts-exhausted", f"every {prov.kind} account is cooling down; "
                                        f"waiting for {n} (until {time.strftime('%H:%M', time.localtime(t))})", run_id))
            if nxt is None or time.time() > deadline:
                return "stopped" if nxt is None else "timeout", final_text or reason, total
            sid = info.get("session_id")
            resume = sid if sid and self.pool.move_session(acct, nxt, sid) else None
            switches += 1
            self.store.event(loop, "account-switch",
                             f"{acct.name} hit its limit (cooling down until {time.strftime('%m-%d %H:%M', time.localtime(until))}); "
                             f"continuing on {nxt.name}" + (" with the same session" if resume else " from the worktree"), run_id)
            if not resume:   # no session to carry over: a fresh agent continues from the worktree
                user = user + ("\n\n# Note\n\nA previous agent worked on this task in this worktree and was "
                               "interrupted. Look at `git log` and `git status` first and continue its work.")
            acct = nxt

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

    def _claude(self, run_id, prov: Provider, role: Role, model, system, user, worktree, env, transcript, d, deadline,
                resume: str | None = None, info: dict | None = None):
        info = {} if info is None else info
        argv = [prov.binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--permission-mode", prov.permission_mode, "--append-system-prompt", system]
        if model:
            argv += ["--model", model]
        if resume:
            argv += ["--resume", resume]
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

        def close_when_idle(turn: int):
            """After the turn numbered `turn`, close the session once the agent has no background
            processes left, unless a new turn starts (output arrives) or a message comes first."""
            while p.poll() is None and time.time() < deadline:
                if state.get("turns_seen") != turn or state.get("output_after", 0) > turn:
                    return            # the agent woke up (or a message arrived): a new turn is running
                if deliver_pending():
                    return
                if not _children(p.pid):
                    break
                time.sleep(3)
            if state.get("turns_seen") == turn and state.get("output_after", 0) <= turn:
                idle.set()
                with lock:
                    try:
                        p.stdin.close()
                    except Exception:
                        pass

        send((info.get("resume_text") or CONTINUE) if resume else user)
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
                if ev.get("type") != "result" and state.get("turns_seen"):
                    state["output_after"] = state["turns_seen"] + 1   # a new turn started after the last result
                if ev.get("type") == "system" and ev.get("session_id"):
                    info["session_id"] = ev["session_id"]
                    self.store.run_update(run_id, result={"session_id": ev["session_id"]})
                limit = _claude_limit(ev)
                if limit:
                    info["limit"] = limit
                    state["killed"] = "limited"
                    _kill(p)
                    break
                if ev.get("type") == "result":
                    final_text = ev.get("result") or final_text
                    u = ev.get("usage") or {}
                    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                        usage[k] = usage.get(k, 0) + int(u.get(k) or 0)
                    usage["cost_usd"] = round(usage.get("cost_usd", 0) + float(ev.get("total_cost_usd") or 0), 4)
                    usage["turns"] = usage.get("turns", 0) + int(ev.get("num_turns") or 0)
                    # A turn ended: continue with pending messages; otherwise end the session, but not
                    # while the agent still has background work running (a command it started in the
                    # background wakes it with a new turn when it finishes).
                    if not deliver_pending():
                        turn = state.get("turns_seen", 0) + 1
                        state["turns_seen"] = turn
                        threading.Thread(target=close_when_idle, args=(turn,), daemon=True).start()
        p.wait()
        if state.get("killed"):
            return state["killed"], final_text, usage
        if p.returncode != 0 and not final_text:
            err = (d / "stderr.log").read_text(errors="replace")[-3000:] if (d / "stderr.log").exists() else ""
            if is_limit(err):
                info["limit"] = err.strip().splitlines()[-1]
                return "limited", final_text, usage
        return ("exited" if p.returncode == 0 or final_text else "failed"), final_text, usage

    # ---- Codex

    def _codex(self, run_id, prov: Provider, role: Role, model, system, user, worktree, env, transcript, d, deadline,
               resume: str | None = None, info: dict | None = None):
        info = {} if info is None else info
        last = d / "last_message.txt"
        if prov.sandbox == "danger-full-access":
            perm = ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            # A sandboxed agent still has to commit (git's metadata for a worktree lives in the main
            # repository's .git) and report (its result file, ff note/inbox in the factory's store):
            # those two places are writable too, nothing else outside the worktree.
            roots = [str(self.cfg.project.state_dir)]
            gitdir = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                    cwd=worktree, capture_output=True, text=True).stdout.strip()
            if gitdir:
                roots.append(gitdir)
            perm = ["--sandbox", prov.sandbox,
                    "-c", "sandbox_workspace_write.writable_roots=" + json.dumps(roots)]
        common = ["--json", "--skip-git-repo-check", "-o", str(last)] + (["-m", model] if model else [])
        argv = [prov.binary, "exec", *common, "-C", str(worktree), *perm, *prov.args, *role.args, "-"]
        usage: dict = {}
        thread_id = resume
        text = system + "\n\n" + user
        if resume:
            argv = [prov.binary, "exec", "resume", resume, *common, *_resume_perm(perm), "-"]
            text = info.get("resume_text") or CONTINUE
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
                        info["session_id"] = thread_id
                        self.store.run_update(run_id, result={"session_id": thread_id})
                    limit = _codex_limit(ev)
                    if limit:
                        info["limit"] = limit
                        state["killed"] = "limited"
                        _kill(p)
                        break
                    if ev.get("type") == "turn.completed":
                        for k, v in (ev.get("usage") or {}).items():
                            if isinstance(v, (int, float)):
                                usage[k] = usage.get(k, 0) + v
            p.wait()
            if state.get("killed"):
                status = state["killed"]
                break
            if p.returncode != 0:
                err = (d / "stderr.log").read_text(errors="replace")[-3000:] if (d / "stderr.log").exists() else ""
                if is_limit(err):
                    info["limit"] = err.strip().splitlines()[-1]
                    status = "limited"
                else:
                    status = "failed"
                break
            msgs = self.store.pending(run_id)
            if not msgs or not thread_id or time.time() > deadline:
                break
            # steering that arrived after the turn: resume the same session with it
            text = "\n\n".join(f"[message from {m['sender']}] {m['text']}" for m in msgs)
            self.store.mark_delivered([m["id"] for m in msgs], "resume")
            argv = [prov.binary, "exec", "resume", thread_id, *common, *_resume_perm(perm), "-"]
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
