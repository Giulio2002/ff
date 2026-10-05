"""The gate: the only way to main.

Deterministic Python, not an agent, so nobody can talk it into a shortcut. For a candidate branch:

  0. the candidate must contain the current main (otherwise the caller rebases and asks again)
  1. cold: a fresh clone of the candidate commit, no caches
  2. the lock file is untouched by the candidate (only the gate writes it)
  3. regenerate every generated file; the tree must be byte-identical to what was committed
  4. frozen statements: unchanged, or changed with a recorded, proved strengthening (lock from main)
  5. no forbidden construct; every file checks within its budget; language extras
     (bend --verdict kernel recheck, lean axiom audit)
  6. unit tests, test vectors, runtime tests
  7. green: commit the updated lock (new statements locked) on top, and move main to exactly that
     tree with a compare-and-swap; push to the remote when one is configured.

`verify_tree` (steps 3-6) is also what `ff check` runs inside an agent's worktree, so agents see the
gate's verdict before they ask for it.
"""
from __future__ import annotations

import fcntl
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import frozen
from .checkers import CheckReport, make, run_cmd
from .config import Config
from .git import git, is_ancestor, sha, show
from .store import Store


@dataclass
class Verdict:
    ok: bool
    stage: str = ""
    reason: str = ""
    log: str = ""
    check: CheckReport | None = None
    new_lock: dict[str, str] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)


def verify_tree(cfg: Config, root: Path, lock: dict[str, str], *, regenerate: bool = True,
                only: list[str] | None = None, run_tests: bool = True, full: bool = True) -> Verdict:
    log = []
    if regenerate and cfg.commands.get("regenerate"):
        before = git(root, "status", "--porcelain", "--untracked-files=all", check=False)
        code, out, secs, _ = run_cmd(cfg.commands["regenerate"], root, 3600, cfg.limits.nice)
        log.append(f"$ {cfg.commands['regenerate']}  ({secs:.1f}s, exit {code})\n{out[-4000:]}")
        if code != 0:
            return Verdict(False, "regenerate", "the generators failed", "\n".join(log))
        after = git(root, "status", "--porcelain", "--untracked-files=all", check=False)
        if after != before:
            diff = git(root, "diff", "--stat", check=False)
            return Verdict(False, "regenerate", "generated files differ from what was committed "
                           "(edit the generators, not their output, and commit the regenerated files)",
                           "\n".join(log) + "\n" + after + "\n" + diff)
    checker = make(cfg.project.language, cfg.checker, cfg.limits.nice)
    names = frozen.declared_names(root, cfg.checker.files + cfg.spec.frozen, cfg.project.language)
    changes = (root / cfg.spec.changes_file).read_text() if (root / cfg.spec.changes_file).exists() else None
    fr = frozen.verify(root, cfg.spec.frozen, cfg.project.language, lock, changes,
                       cfg.spec.require_implication_proof, names)
    if not fr.ok:
        return Verdict(False, "frozen", "frozen statements changed", "\n".join(log + fr.problems))
    log.append(f"frozen: {len(lock)} locked statements ok, {len(fr.added)} new, {len(fr.changed_ok)} recorded changes")
    files = [root / f for f in only] if only else None
    rep = checker.check(root, only=files, frozen_globs=cfg.spec.frozen, full=full)
    log.append("checker: " + rep.summary())
    if not rep.ok:
        return Verdict(False, "check", "the checker rejected the tree", "\n".join(log) + "\n\n" + rep.details(),
                       check=rep)
    if run_tests:
        for name in ("unit_tests", "vectors", "runtime_tests"):
            cmd = cfg.commands.get(name)
            if not cmd:
                continue
            code, out, secs, to = run_cmd(cmd, root, 7200, cfg.limits.nice)
            log.append(f"$ {cmd}  ({secs:.1f}s, exit {code})\n{out[-3000:]}")
            if code != 0 or to:
                return Verdict(False, name, f"{name} failed", "\n".join(log), check=rep)
    return Verdict(True, "green", "", "\n".join(log), check=rep, new_lock=fr.new_lock, added=fr.added)


class Gate:
    def __init__(self, cfg: Config, store: Store):
        self.cfg, self.store = cfg, store
        self.repo = cfg.project.repo
        self.main = cfg.project.main_branch
        self.dir = cfg.project.state_dir / "gate"
        self.dir.mkdir(parents=True, exist_ok=True)

    def main_lock(self) -> dict[str, str]:
        return frozen.load_lock(show(self.repo, self.main, self.cfg.spec.lock_file))

    def submit(self, branch: str, run_id: str | None = None, message: str = "") -> Verdict:
        """Gate `branch`; on green main moves to the gated tree. Serialised across processes."""
        with open(self.dir / "gate.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            return self._submit(branch, run_id, message)

    def _submit(self, branch: str, run_id: str | None, message: str) -> Verdict:
        t0 = time.time()
        main_sha, cand = sha(self.repo, self.main), sha(self.repo, branch)
        gid = self.store.x("INSERT INTO gates (run_id, branch, commit_sha, status, started) VALUES (?,?,?,?,?)",
                           (run_id, branch, cand, "running", t0))
        self.store.event("gate", "gate-start", f"gate #{gid}: {branch} @ {cand[:10]}", run_id)

        def done(v: Verdict) -> Verdict:
            log_path = self.dir / f"gate-{gid}.log"
            log_path.write_text(f"stage: {v.stage}\nreason: {v.reason}\n\n{v.log}")
            durations = {f.path: round(f.seconds, 2) for f in (v.check.files if v.check else [])}
            self.store.x("UPDATE gates SET status = ?, reason = ?, log = ?, ended = ?, durations = ? WHERE id = ?",
                         ("green" if v.ok else "red", v.reason, str(log_path), time.time(), json.dumps(durations), gid))
            self.store.event("gate", "gate-green" if v.ok else "gate-red",
                             f"gate #{gid} {'GREEN' if v.ok else 'RED'} for {branch}"
                             + ("" if v.ok else f" at {v.stage}: {v.reason}"), run_id, log=str(log_path))
            return v

        if not is_ancestor(self.repo, main_sha, cand):
            return done(Verdict(False, "stale", "the candidate does not contain the current main; rebase it"))
        if cand == main_sha:
            return done(Verdict(False, "empty", "the candidate has no new commits"))
        lock_path = self.cfg.spec.lock_file
        if git(self.repo, "diff", "--name-only", main_sha, cand, "--", lock_path):
            return done(Verdict(False, "lock", f"the candidate modifies {lock_path}; only the gate writes it"))
        if self.cfg.gate.host != "local":
            return done(self._remote(branch, cand))
        tree = self.dir / f"tree-{gid}"
        shutil.rmtree(tree, ignore_errors=True)
        git(self.dir, "clone", "-q", "--no-hardlinks", str(self.repo), str(tree))
        git(tree, "checkout", "-q", "--detach", cand)
        v = verify_tree(self.cfg, tree, self.main_lock(), regenerate=self.cfg.gate.regenerate)
        if v.ok:
            final = cand
            if v.added or v.new_lock != self.main_lock():
                (tree / lock_path).write_text(frozen.dump_lock(v.new_lock))
                git(tree, "add", lock_path)
                git(tree, "-c", "user.name=formal-factory gate", "-c", "user.email=gate@localhost",
                    "commit", "-q", "-m", f"gate: lock {len(v.added)} new frozen statement(s)")
                final = sha(tree, "HEAD")
            if not self._promote(tree, final, main_sha):
                v = Verdict(False, "race", "main moved while gating; rebase and resubmit", v.log, v.check)
            else:
                v.log += f"\n\nmain: {main_sha[:10]} -> {final[:10]}"
                shutil.rmtree(tree, ignore_errors=True)
        self._prune_trees()
        return done(v)

    def _promote(self, tree: Path, final: str, expected_main: str) -> bool:
        git(self.repo, "fetch", "-q", str(tree), f"{final}:refs/ff/gated/{final[:12]}")
        if sha(self.repo, self.main) != expected_main:
            return False
        head = git(self.repo, "symbolic-ref", "-q", "HEAD", check=False)
        if head == f"refs/heads/{self.main}":
            # main is checked out in the factory's clone: move it and its files together
            git(self.repo, "reset", "-q", "--keep", final)
        else:
            git(self.repo, "update-ref", f"refs/heads/{self.main}", final, expected_main)
        if self.cfg.project.remote:
            git(self.repo, "push", "-q", self.cfg.project.remote, f"{self.main}:{self.main}")
        return True

    def _remote(self, branch: str, cand: str) -> Verdict:
        """Gate on a build server: push the candidate there and run `ff gate-run` over ssh."""
        remote = self.cfg.project.remote
        if not remote or not self.cfg.gate.remote_config:
            return Verdict(False, "config", "gate.host needs project.remote and gate.remote_config")
        git(self.repo, "push", "-q", "-f", remote, f"{cand}:refs/heads/{branch}")
        p = subprocess.run(["ssh", self.cfg.gate.host, "ff", "--config", self.cfg.gate.remote_config,
                            "gate-run", branch, "--json"], capture_output=True, text=True)
        try:
            r = json.loads(p.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return Verdict(False, "remote", "the remote gate did not answer", p.stdout + p.stderr)
        if r.get("ok"):
            git(self.repo, "fetch", "-q", remote, f"{self.main}:{self.main}")
        return Verdict(bool(r.get("ok")), r.get("stage", ""), r.get("reason", ""), r.get("log", ""))

    def _prune_trees(self) -> None:
        trees = sorted(self.dir.glob("tree-*"), key=lambda p: p.stat().st_mtime)
        for t in trees[:-max(0, self.cfg.gate.keep_failed_trees)] if self.cfg.gate.keep_failed_trees else trees:
            shutil.rmtree(t, ignore_errors=True)
