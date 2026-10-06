"""The factory: configuration, store, runner, workspaces and gate, plus the loops that use them."""
from __future__ import annotations

import json
import os
import uuid
import threading
from contextlib import contextmanager
import time
from pathlib import Path

from .agents import FINAL, Runner
from .config import Config, load
from .gate import Gate
from .git import Workspaces, git
from .store import Store


class Factory:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.project.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(cfg.project.state_dir / "factory.db")
        self.runner = Runner(cfg, self.store)
        self.ws = Workspaces(cfg.project.repo, cfg.project.state_dir / "worktrees", cfg.project.main_branch)
        self.gate = Gate(cfg, self.store)
        self.loops: dict = {}
        self.instance = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"   # who owns adopted cycles
        self._view_lock = threading.Lock()
        self._view_locks: dict = {}
        self.maybe_reload()   # remembers the file's mtime

    def maybe_reload(self) -> bool:
        """Re-read the YAML when it changed. The Config object is shared by the runner, the gate and
        the loops, so it is updated in place: budgets, commands, models, prompts and timeouts apply
        to the next run and the next gate. (Worker counts and max_parallel_agents need a restart.)"""
        try:
            mtime = self.cfg.path.stat().st_mtime
        except OSError:
            return False
        if mtime == getattr(self, "_cfg_mtime", None):
            return False
        first = getattr(self, "_cfg_mtime", None) is None
        self._cfg_mtime = mtime
        if first:
            return False
        try:
            new = load(self.cfg.path)
        except Exception as e:
            self.store.event("factory", "error", f"factory.yaml changed but does not load; keeping the old one: {e}")
            return False
        self.cfg.__dict__.update(new.__dict__)
        self.store.event("factory", "config-reloaded", "factory.yaml reloaded")
        return True

    @classmethod
    def from_path(cls, path) -> "Factory":
        return cls(load(path))

    @contextmanager
    def main_view(self, name: str = "main"):
        """A detached worktree of the current main for one consumer (`name`: backlog, bench, ...),
        refreshed and cleaned on entry and held under a lock until the block ends, so two
        consumers never clean or rebuild under each other."""
        view = self.cfg.project.state_dir / f"main-view-{name}"
        with self._view_lock:
            lock = self._view_locks.setdefault(name, threading.Lock())
        with lock:
            if not view.exists():
                git(self.cfg.project.repo, "worktree", "add", "-q", "--detach", str(view), self.cfg.project.main_branch)
            else:
                git(view, "checkout", "-q", "--detach", "-f", self.cfg.project.main_branch)
                git(view, "clean", "-qfdx", "-e", "build/", check=False)   # keep build caches
            yield view

    def reconcile(self, loops: dict | None = None) -> tuple[list[str], list[str]]:
        """At daemon start, the loop runs a previous daemon left behind: a run whose process is still
        going and whose loop is running here is adopted (its cycle continues: commit, gate, retry);
        any other is stopped and its backlog item reopened. Detached ad-hoc runs and subagents have
        their own process and are left alone. Returns (adopted, stopped)."""
        import os
        import signal
        adopted, stopped = [], []
        # a cycle whose agent is done can still be on its way to main (commit, rebase, gate): its run
        # is flagged cycle-open until the cycle ends, and is adopted like a running one
        open_ids = {k["key"].split(":", 1)[1] for k in self.store.q("SELECT key FROM flags WHERE key LIKE 'cycle-open:%'")}
        rows = self.store.q("SELECT * FROM runs WHERE (status IN ('running', 'queued') OR id IN (%s)) AND loop IN "
                            "('implement', 'optimize', 'audit') ORDER BY started" % ",".join("?" * len(open_ids)),
                            tuple(open_ids)) if open_ids else self.store.q(
            "SELECT * FROM runs WHERE status IN ('running', 'queued') AND loop IN ('implement', 'optimize', 'audit') "
            "ORDER BY started")
        for r in rows:
            if r["status"] in FINAL:
                loop = (loops or {}).get(r["loop"])
                if loop is not None and r["cycle"] and Path(r["worktree"] or "/nonexistent").exists():
                    loop.adopt_in_thread(r)
                    adopted.append(r["id"])
                else:
                    self.store.set_flag(f"cycle-open:{r['id']}", None)
                continue
            alive = False
            if r["pid"]:
                try:
                    os.kill(r["pid"], 0)
                    alive = True
                except (ProcessLookupError, PermissionError):
                    alive = False
            loop = (loops or {}).get(r["loop"])
            cyc = json.loads(r["cycle"] or "{}") if r["cycle"] else {}
            if alive and loop is not None and cyc:
                loop.adopt_in_thread(r)
                adopted.append(r["id"])
                continue
            if alive:
                try:
                    os.killpg(os.getpgid(r["pid"]), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            self.store.run_update(r["id"], status="stopped", ended=time.time(),
                                  summary=(r["summary"] or "") + " [stopped: its daemon was restarted]")
            stopped.append(r["id"])
        adopted_items = {json.loads(r["cycle"] or "{}").get("item") for r in self.store.q(
            "SELECT cycle FROM runs WHERE id IN (%s)" % ",".join("?" * len(adopted)), adopted)} if adopted else set()
        for row in self.store.q("SELECT item FROM backlog WHERE status = 'running'"):
            if row["item"] not in adopted_items:
                self.store.x("UPDATE backlog SET status = 'open' WHERE item = ?", (row["item"],))
        if adopted or stopped:
            self.store.event("factory", "reconciled", f"previous daemon's runs: adopted {len(adopted)} "
                             f"({', '.join(adopted)}), stopped {len(stopped)} ({', '.join(stopped)})")
        return adopted, stopped

    def start_loops(self, names: list[str] | None = None) -> None:
        from .loops import LOOPS
        enabled = {"implement": self.cfg.implement, "optimize": self.cfg.optimize, "audit": self.cfg.audit}
        for name, cls in LOOPS.items():
            if names and name not in names:
                continue
            if not enabled[name].enabled:
                continue
            loop = cls(self)
            self.loops[name] = loop
        # adopt what a previous daemon left running before the workers take new items
        self.reconcile(self.loops)
        for name, loop in self.loops.items():
            workers = {"implement": self.cfg.implement.workers, "optimize": self.cfg.optimize.workers, "audit": 1}[name]
            # an adopted cycle occupies a worker's place
            busy = sum(1 for t in loop.threads if t.name.startswith(f"{name}-adopt-"))
            loop.start(max(0, workers - busy) if name != "audit" else workers)
            self.store.event("factory", "loop-start", f"loop {name} started with {workers} worker(s)")

    def stop_loops(self) -> None:
        for loop in self.loops.values():
            loop.stop.set()

    # ---------------------------------------------------------------- ad-hoc agents (API / CLI)

    def start_agent(self, role: str, task: str, parent: str | None = None, own_worktree: bool = True,
                    worktree: Path | None = None, branch: str | None = None, depth: int = 0) -> str:
        """An agent outside the loops. It gets its own worktree of main unless one is given (a
        subagent shares its parent's), and runs in a background process so it can be steered."""
        if own_worktree or worktree is None:
            from .agents import new_run_id
            tmp = new_run_id(role)
            worktree, branch = self.ws.create(tmp)
        return self.runner.launch_detached(role, task, loop="adhoc" if parent is None else "subagent",
                                           worktree=worktree, branch=branch or "", parent=parent, depth=depth)

    # ---------------------------------------------------------------- reporting

    def status(self) -> dict:
        s = self.store
        running = s.q("SELECT id, role, loop, parent, started, task FROM runs WHERE status IN ('running','queued') "
                      "ORDER BY started")
        gate = s.q("SELECT id, branch, status, reason, started, ended FROM gates ORDER BY id DESC LIMIT 1")
        decisions = s.q("SELECT id, question, options FROM decisions WHERE answer IS NULL")
        backlog = {r["status"]: r["n"] for r in s.q("SELECT status, COUNT(*) n FROM backlog GROUP BY status")}
        rounds = s.q("SELECT n, status, counts FROM rounds ORDER BY n DESC LIMIT 1")
        cost = s.q("SELECT SUM(json_extract(usage, '$.cost_usd')) c FROM runs")[0]["c"] or 0
        paused = [r["key"].split(":", 1)[1] for r in s.q("SELECT key FROM flags WHERE key LIKE 'paused:%' AND value = '1'")]
        return {
            "project": self.cfg.project.name,
            "language": self.cfg.project.language,
            "main": git(self.cfg.project.repo, "rev-parse", "--short", self.cfg.project.main_branch, check=False),
            "paused": paused,
            "optimize_target": ({"target": self.cfg.benchmark.target,
                                 "reached": s.flag("optimize:at-target") == git(self.cfg.project.repo, "rev-parse",
                                                                                self.cfg.project.main_branch, check=False)}
                                if self.cfg.benchmark.target is not None else None),
            "audit_waiting": s.flag("audit:waiting") or "",
            "running": [dict(r) | {"for_s": round(time.time() - (r["started"] or time.time()))} for r in running],
            "last_gate": dict(gate[0]) if gate else None,
            "open_decisions": [dict(d) | {"options": json.loads(d["options"])} for d in decisions],
            "backlog": backlog,
            "audit_round": dict(rounds[0]) if rounds else None,
            "cost_usd": round(cost, 2),
            "accounts": self.accounts(),
        }

    def accounts(self) -> list[dict]:
        now = time.time()
        out = []
        for r in self.runner.pool.list():
            state = "disabled" if r["disabled"] else (
                f"cooling until {time.strftime('%m-%d %H:%M', time.localtime(r['cooldown_until']))}"
                if r["cooldown_until"] > now else "ready")
            out.append({"name": r["name"], "kind": r["kind"], "email": r["email"], "state": state,
                        "active": r["active"], "runs": r["runs"], "limit_hits": r["limit_hits"]})
        return out

    def run_view(self, run_id: str, tail: int = 4000) -> dict | None:
        r = self.store.run(run_id)
        if r is None:
            return None
        d = dict(r)
        for k in ("result", "usage"):
            d[k] = json.loads(d[k] or "{}")
        d["children"] = [dict(id=c["id"], role=c["role"], status=c["status"]) for c in self.store.children(run_id)]
        d["messages"] = [dict(m) for m in self.store.q("SELECT sender, text, ts, delivered, via FROM messages "
                                                         "WHERE run_id = ? ORDER BY id", (run_id,))]
        t = Path(d.get("transcript") or "")
        d["transcript_tail"] = t.read_text(errors="replace")[-tail:] if t.is_file() else ""
        d["final"] = d["status"] in FINAL
        return d
