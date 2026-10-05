"""The factory: configuration, store, runner, workspaces and gate, plus the loops that use them."""
from __future__ import annotations

import json
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
        self._view_lock = threading.Lock()
        self._view_locks: dict = {}

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

    def start_loops(self, names: list[str] | None = None) -> None:
        from .loops import LOOPS
        enabled = {"implement": self.cfg.implement, "optimize": self.cfg.optimize, "audit": self.cfg.audit}
        for name, cls in LOOPS.items():
            if names and name not in names:
                continue
            if not enabled[name].enabled:
                continue
            loop = cls(self)
            workers = {"implement": self.cfg.implement.workers, "optimize": self.cfg.optimize.workers, "audit": 1}[name]
            loop.start(workers)
            self.loops[name] = loop
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
