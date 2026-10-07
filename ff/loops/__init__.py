"""The three loops: implement (builds it), optimize (makes it fast), audit (tries to break it).
All of them hand their work to the same gate."""
from __future__ import annotations

import json
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents import RunResult, new_run_id
from ..checkers import run_cmd
from ..git import git, sha

if TYPE_CHECKING:
    from ..factory import Factory


@dataclass
class CycleOutcome:
    ok: bool
    status: str           # merged | blocked | gave-up | no-change | rejected | superseded
    run_ids: list[str]
    summary: str = ""
    result: dict | None = None


class Loop:
    name = ""

    def __init__(self, f: "Factory"):
        self.f, self.cfg, self.store = f, f.cfg, f.store
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def log(self, kind: str, msg: str, run_id: str | None = None, **data) -> None:
        self.store.event(self.name, kind, msg, run_id, **data)

    def wait_unpaused(self) -> bool:
        while self.store.paused(self.name):
            if self.stop.wait(10):
                return False
        return not self.stop.is_set()

    def start(self, workers: int) -> None:
        for i in range(workers):
            t = threading.Thread(target=self._guard, args=(i,), name=f"{self.name}-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    def _guard(self, i: int) -> None:
        while not self.stop.is_set():
            try:
                if self.worker(i) is False:
                    return
            except Exception as e:  # keep the loop alive; tell the coordinator
                self.log("error", f"{self.name} worker {i} crashed: {e!r}; restarting in 60s")
                self.stop.wait(60)

    def worker(self, i: int):
        raise NotImplementedError

    # ---------------------------------------------------------------- the shared cycle

    def cycle(self, role: str, task: str, *, max_attempts: int, result_extra: str = "",
              extra: dict | None = None, accept=None, item: str | None = None, resume: dict | None = None,
              state: dict | None = None) -> CycleOutcome:
        """worktree -> agent -> commit -> rebase -> gate, retrying with the gate's log on red.
        `accept(result, worktree)` may veto before the gate (the optimizer's benchmark).

        Agents run in processes of their own and the cycle's state is saved on each run (`cycle`),
        so a restarted daemon can adopt a run that is still going: `resume` is that run's row."""
        f = self.f
        if resume:
            first = resume["id"]
            worktree, branch = Path(resume["worktree"]), resume["branch"]
            start = int(resume["attempt"] or 1)
            brief = resume["task"]
        else:
            first = new_run_id(role)
            worktree, branch = f.ws.create(first)
            start, brief = 1, task
        runs = []
        detached = self.cfg.limits.detached_agents
        try:
            for attempt in range(start, max_attempts + 1):
                if resume and attempt == start:
                    f.runner.slots.acquire(force=True)    # it is running already: it counts
                    try:
                        r: RunResult = f.runner.wait(first)
                    finally:
                        f.runner.slots.release()
                else:
                    if not self.wait_unpaused():
                        return CycleOutcome(False, "gave-up", runs, "stopped")
                    rid = first if (attempt == start and not resume) else new_run_id(role)
                    for old in runs:        # the cycle lives on in its newest run only
                        self.store.set_flag(f"cycle-open:{old}", None)
                    self.store.set_flag(f"cycle-open:{rid}", "1")
                    cyc = {"loop": self.name, "role": role, "task": task, "max_attempts": max_attempts,
                           "item": item, "result_extra": result_extra, **(state or {})}
                    r = f.runner.run(role, brief, loop=self.name, worktree=worktree, branch=branch,
                                     attempt=attempt, result_extra=result_extra, extra=extra, run_id=rid,
                                     detached=detached, cycle=cyc)
                runs.append(r.run_id)
                owner = self.store.flag(f"owner:{r.run_id}")
                if owner and owner != f.instance:
                    # a newer daemon adopted this run and carries the cycle on; leave it to that one
                    return CycleOutcome(False, "superseded", runs, f"adopted by another daemon", r.result)
                if r.result.get("launch_error"):
                    # the agent never started (a CLI refusing to run, a bad binary, no login): not the
                    # agent's failure, so it costs no attempt; stop this loop until a human looks
                    self.store.set_flag(f"paused:{self.name}", "1")
                    self.log("launch-error", f"{role} could not start, loop {self.name} paused: "
                                             f"{r.result['launch_error'][:500]}", r.run_id)
                    return CycleOutcome(False, "launch-error", runs, r.result["launch_error"], r.result)
                if r.status == "blocked":
                    return CycleOutcome(False, "blocked", runs, r.summary, r.result)
                if r.status in ("stopped",):
                    return CycleOutcome(False, "gave-up", runs, "stopped by request", r.result)
                f.ws.commit_pending(worktree, f"{role}: {r.summary.splitlines()[0][:72] if r.summary else 'work in progress'}")
                if not f.ws.has_new_commits(worktree):
                    if r.status != "done":
                        brief = task + f"\n\n# Previous attempt\n\nThe previous run ended with status {r.status} " \
                                       f"and no commits. Summary: {r.summary[:1500]}"
                        continue
                    return CycleOutcome(False, "no-change", runs, r.summary, r.result)
                if accept is not None:
                    ok, why = accept(r, worktree)
                    if not ok:
                        return CycleOutcome(False, "rejected", runs, why, r.result)
                for _ in range(3):  # stale or raced: rebase and resubmit without bothering the agent
                    if not f.ws.rebase_on_main(worktree):
                        v = None
                        break
                    v = f.gate.submit(branch, r.run_id, r.summary, rebase=lambda: f.ws.rebase_on_main(worktree))
                    if v.ok or v.stage not in ("stale", "race"):
                        break
                if v is None:
                    brief = task + "\n\n# Rebase conflict\n\nmain moved and your branch no longer rebases " \
                                   f"cleanly onto {self.cfg.project.main_branch}. Rebase it yourself " \
                                   f"(`git rebase {self.cfg.project.main_branch}`), resolve, " + \
                                   ("regenerate, " if self.cfg.project.workflow == "generators" else "") + "commit."
                    continue
                if v.ok:
                    return CycleOutcome(True, "merged", runs, r.summary, r.result)
                brief = task + f"\n\n# The gate rejected your previous attempt (stage: {v.stage})\n\n" \
                               f"{v.reason}\n\n```\n{v.log[-6000:]}\n```\nFix it on the same branch and commit."
            return CycleOutcome(False, "gave-up", runs, f"no green gate after {max_attempts} attempts")
        finally:
            if not (runs and self.store.flag(f"owner:{runs[-1]}") not in (None, f.instance)):
                f.ws.remove(worktree)   # (a superseded cycle leaves the worktree to its new owner)
                for old in runs + ([resume["id"]] if resume else []):
                    self.store.set_flag(f"cycle-open:{old}", None)

    def adopt(self, row) -> None:
        """Continue the cycle of a run a previous daemon started and that is still going."""
        raise NotImplementedError

    def adopt_in_thread(self, row) -> None:
        """Adopt `row` in a thread that then carries on as one of the loop's workers (the audit loop
        has a single orchestrating worker, so an adopted fixer just ends)."""
        self.store.set_flag(f"owner:{row['id']}", self.f.instance)

        def go():
            try:
                self.adopt(row)
            except Exception as e:
                import traceback
                self.log("error", f"adopting {row['id']} failed: {e!r}\n{traceback.format_exc()[-1500:]}", row["id"])
            if self.name != "audit":
                self._guard(len(self.threads))
        t = threading.Thread(target=go, name=f"{self.name}-adopt-{row['id']}", daemon=True)
        t.start()
        self.threads.append(t)

    def fresh_tree(self, role: str):
        rid = new_run_id(role)
        wt, br = self.f.ws.create(rid)
        return rid, wt, br


# ===================================================================== implement

class ImplementLoop(Loop):
    name = "implement"
    _pick = threading.Lock()

    def refresh_backlog(self) -> None:
        cmd = self.cfg.implement.backlog_command
        if not cmd:
            return
        # the backlog is a function of main: list it again only when main moved (a failed listing is
        # retried after 30 minutes). It can be expensive (it may run the checker over every file).
        import hashlib
        main = sha(self.cfg.project.repo, self.cfg.project.main_branch) + ":" + hashlib.sha1(cmd.encode()).hexdigest()[:8]
        last = self.store.flag("backlog:main") or ""
        if last == main or (last == "failed:" + main
                            and time.time() - float(self.store.flag("backlog:failed-at") or 0) < 1800):
            return
        with self.f.main_view("backlog") as view:
            code, out, secs, timed_out = run_cmd(cmd, view, 1800, self.cfg.limits.nice)
        if code != 0 or timed_out:
            self.store.set_flag("backlog:main", "failed:" + main)
            self.store.set_flag("backlog:failed-at", str(time.time()))
            self.log("error", f"backlog command {'timed out after %.0fs' % secs if timed_out else 'failed'} "
                              f"on {main[:10]} (retried when main moves, or in 30 min): {out[-500:]}")
            return
        self.store.set_flag("backlog:main", main)
        # Each item is "<id>\t<description>" (or an object {"id", "task"} in a JSON list); a plain
        # line is its own id. The id is the item's identity: a description may change (a reworded
        # statement) without creating a second item while the first is still being worked on.
        out = out.strip()
        pairs: list[tuple[str, str]] = []
        try:
            for i in json.loads(out):
                if isinstance(i, str):
                    pairs.append((i, i))
                else:
                    iid = str(i.get("id") or json.dumps(i, sort_keys=True))
                    pairs.append((iid, str(i.get("task") or i.get("description") or iid)))
        except json.JSONDecodeError:
            for l in out.splitlines():
                if l.strip():
                    iid, _, desc = l.partition("\t")
                    pairs.append((iid.strip(), (desc or iid).strip()))
        now = time.time()
        current = {iid for iid, _ in pairs}
        for iid, desc in pairs:
            self.store.x("INSERT OR IGNORE INTO backlog (item, status, attempts, updated, note) VALUES (?, 'open', 0, ?, ?)",
                         (iid, now, desc))
            # the backlog command lists open work only: an item it lists again is open again
            # (a merged step that did not finish it, like a speed target still out of reach)
            self.store.x("UPDATE backlog SET status = 'open', attempts = 0 WHERE item = ? AND status = 'done'", (iid,))
            self.store.x("UPDATE backlog SET note = ? WHERE item = ? AND NOT item LIKE 'brief: %'", (desc, iid))
        for r in self.store.q("SELECT item FROM backlog WHERE status IN ('open','blocked')"):
            if r["item"] not in current and not r["item"].startswith("brief: "):
                self.store.x("UPDATE backlog SET status = 'done', updated = ? WHERE item = ?", (now, r["item"]))

    def take(self) -> str | None:
        with self._pick:
            self.refresh_backlog()
            briefs = self.store.take_briefs(self.name)
            if briefs:
                item = "brief: " + briefs[0][:200]
                self.store.x("INSERT OR REPLACE INTO backlog (item, status, attempts, updated, note) "
                             "VALUES (?, 'running', 0, ?, ?)", (item, time.time(), briefs[0]))
                for b in briefs[1:]:
                    self.store.x("INSERT INTO briefs (loop, text, status, ts) VALUES (?, ?, 'open', ?)",
                                 (self.name, b, time.time()))
                return item
            r = self.store.q("SELECT item FROM backlog WHERE status = 'open' ORDER BY updated LIMIT 1")
            if not r:
                return None
            self.store.x("UPDATE backlog SET status = 'running', updated = ? WHERE item = ?", (time.time(), r[0]["item"]))
            return r[0]["item"]

    def worker(self, i: int):
        if not self.wait_unpaused():
            return False
        item = self.take()
        if item is None:
            self.stop.wait(120)
            return
        self.run_item(item)

    def run_item(self, item: str, resume=None) -> None:
        note = self.store.q("SELECT note FROM backlog WHERE item = ?", (item,))
        note = note[0]["note"] if note else None
        task = note if item.startswith("brief: ") and note else f"Close this open item of the contract:\n\n{note or item}"
        if resume is not None:
            task = json.loads(resume["cycle"] or "{}").get("task") or task
        try:
            out = self.cycle(self.cfg.implement.role, task, max_attempts=self.cfg.implement.max_attempts,
                             item=item, resume=resume)
        except Exception:
            # a crash must not leave the item claimed by nobody
            self.store.x("UPDATE backlog SET status = 'open', updated = ? WHERE item = ? AND status = 'running'",
                         (time.time(), item))
            raise
        if out.status == "superseded":
            return
        status = {"merged": "done", "no-change": "open", "launch-error": "open"}.get(out.status, "blocked")
        if out.status == "no-change":
            # a brief is one-off guidance: an agent that found nothing to change has handled it.
            # A contract item the agent keeps finding nothing to do for is blocked, not retried forever.
            prev = self.store.q("SELECT attempts FROM backlog WHERE item = ?", (item,))
            if item.startswith("brief: "):
                status = "done"
            elif prev and prev[0]["attempts"] + 1 >= self.cfg.implement.max_attempts:
                status = "blocked"
        self.store.x("UPDATE backlog SET status = ?, attempts = attempts + 1, run_id = ?, updated = ?, note = ? "
                     "WHERE item = ?", (status, out.run_ids[-1] if out.run_ids else None, time.time(),
                                        out.summary[:2000] if status != "open" else note, item))
        self.log("item-" + status, f"{item[:120]}: {out.status} - {out.summary[:300]}",
                 out.run_ids[-1] if out.run_ids else None)

    def adopt(self, row) -> None:
        item = json.loads(row["cycle"] or "{}").get("item")
        if not item:
            raise ValueError("no backlog item recorded on the run")
        self.store.x("UPDATE backlog SET status = 'running', updated = ? WHERE item = ?", (time.time(), item))
        self.log("adopted", f"continuing {row['id']} on {item[:100]} after a daemon restart", row["id"])
        self.run_item(item, resume=row)


# ===================================================================== optimize

class OptimizeLoop(Loop):
    name = "optimize"

    def measure(self, root: Path) -> float | None:
        b = self.cfg.benchmark
        vals = []
        for _ in range(max(1, b.repeats)):
            code, out, _, to = run_cmd(b.command, root, b.timeout_minutes * 60, self.cfg.limits.nice)
            m = re.search(b.metric, out)
            if code != 0 or to or not m:
                self.log("bench-error", f"benchmark failed in {root.name}: {out[-400:]}")
                return None
            vals.append(float(m.group(1)))
        return statistics.median(vals)

    def baseline(self) -> float | None:
        main = sha(self.cfg.project.repo, self.cfg.project.main_branch)
        key = f"baseline:{main}"
        cached = self.store.flag(key)
        if cached:
            return float(cached)
        with self.f.main_view("bench") as view:
            v = self.measure(view)
        if v is not None:
            self.store.set_flag(key, str(v))
        return v

    def at_target(self, value: float) -> bool:
        t = self.cfg.benchmark.target
        if t is None:
            return False
        return value <= t if self.cfg.benchmark.direction == "lower" else value >= t

    def better(self, base: float, cand: float) -> float:
        gain = (base - cand) / base if self.cfg.benchmark.direction == "lower" else (cand - base) / base
        return gain * 100

    def worker(self, i: int):
        if not self.wait_unpaused():
            return False
        base = self.baseline()
        if base is None:
            self.stop.wait(600)
            return
        main = sha(self.cfg.project.repo, self.cfg.project.main_branch)
        if self.at_target(base):
            # the goal is met: no more experiments (each costs an agent) unless main moves past it again
            if self.store.flag("optimize:at-target") != main:
                self.store.set_flag("optimize:at-target", main)
                self.log("target-reached", f"benchmark {base} meets the target {self.cfg.benchmark.target} on "
                                           f"{main[:10]}; the optimize loop stops until main changes")
            self.stop.wait(300)
            return
        if self.store.flag("optimize:at-target"):
            self.store.set_flag("optimize:at-target", None)
            self.log("target-lost", f"benchmark {base} misses the target {self.cfg.benchmark.target} on "
                                    f"{main[:10]}; optimizing again")
        hist = self.store.q("SELECT idea, baseline, candidate, kept, reason FROM experiments ORDER BY id DESC LIMIT ?",
                            (self.cfg.optimize.history,))
        lines = [f"- {'KEPT' if h['kept'] else 'reverted'}: {h['idea']} ({h['baseline']} -> {h['candidate']}; {h['reason']})"
                 for h in hist] or ["- (no experiments yet)"]
        briefs = self.store.take_briefs(self.name)
        task = (f"Current benchmark on main: {base} ({self.cfg.benchmark.direction} is better; a change must improve it "
                f"by at least {self.cfg.benchmark.min_improvement_pct}%).\n\nHistory, newest first:\n" + "\n".join(lines)
                + ("\n\nGuidance from the coordinator:\n" + "\n".join(briefs) if briefs else ""))
        self.experiment(base, task)

    def experiment(self, base: float, task: str, resume=None) -> None:
        seen: dict = {}

        def accept(r: RunResult, wt: Path):
            cand = self.measure(wt)
            seen["cand"] = cand
            if cand is None:
                return False, "benchmark failed on the candidate"
            gain = self.better(base, cand)
            seen["gain"] = gain
            if gain < self.cfg.benchmark.min_improvement_pct:
                return False, f"not faster enough: {base} -> {cand} ({gain:+.2f}%)"
            return True, ""

        out = self.cycle(self.cfg.optimize.role, task, max_attempts=2, result_extra=', "idea": "<one line>"',
                         accept=accept, resume=resume, state={"baseline": base})
        if out.status == "superseded":
            return
        idea = (out.result or {}).get("idea") or out.summary[:200]
        reason = out.summary if not out.ok else f"{seen.get('gain', 0):+.2f}%"
        self.store.x("INSERT INTO experiments (run_id, idea, baseline, candidate, kept, reason, ts) VALUES (?,?,?,?,?,?,?)",
                     (out.run_ids[-1] if out.run_ids else None, idea, base, seen.get("cand"), int(out.ok),
                      reason[:500], time.time()))
        self.log("experiment-kept" if out.ok else "experiment-reverted", f"{idea[:150]}: {reason[:200]}",
                 out.run_ids[-1] if out.run_ids else None)

    def adopt(self, row) -> None:
        cyc = json.loads(row["cycle"] or "{}")
        if cyc.get("baseline") is None:
            raise ValueError("no baseline recorded on the run")
        self.log("adopted", f"continuing experiment {row['id']} after a daemon restart", row["id"])
        self.experiment(float(cyc["baseline"]), cyc.get("task") or row["task"], resume=row)


# ===================================================================== audit

SEVERITIES = ("critical", "high", "medium", "low")
FINDINGS_EXTRA = (', "findings": [{"title": "...", "severity": "critical|high|medium|low", '
                  '"description": "...", "reproducer": "..."}]')


class AuditLoop(Loop):
    name = "audit"

    def __init__(self, f: "Factory"):
        super().__init__(f)
        from ..agents import Slots
        self._fixers = Slots(self.cfg.audit.fixers)

    def waiting_for(self) -> str:
        """What `loops.audit.after` still waits for ("" when the audit may run)."""
        out = []
        after = self.cfg.audit.after
        if "implement" in after and self.store.q("SELECT 1 FROM backlog WHERE status IN ('open', 'running') "
                                                 "AND NOT item LIKE 'brief: %' LIMIT 1"):
            out.append("implement (open backlog items)")
        if "optimize" in after:
            main = sha(self.cfg.project.repo, self.cfg.project.main_branch)
            if self.store.flag("optimize:at-target") != main:
                out.append(f"optimize (benchmark.target {self.cfg.benchmark.target} not yet met on main)")
        return "; ".join(out)

    def worker(self, i: int):
        a = self.cfg.audit
        last = self.store.q("SELECT * FROM rounds ORDER BY n DESC LIMIT 1")
        last = last[0] if last else None
        if not self.wait_unpaused():
            return False
        if last is not None and last["status"] == "done" and a.confirm_each_round:
            # the human's answer about another round outlives a daemon restart
            ans = self.confirm(last["n"], json.loads(last["counts"] or "{}"))
            if ans is None:
                return False
            if not ans:
                self.log("audit-done", f"human said no more rounds after round {last['n']}")
                return False
        resume = last is not None and last["status"] != "done"
        n = last["n"] if resume else (last["n"] + 1 if last else 1)
        if n > a.max_rounds:
            self.log("audit-done", f"reached max_rounds={a.max_rounds}; the audit loop stops")
            return False
        if not resume:
            waiting = self.waiting_for()
            if waiting:
                if self.store.flag("audit:waiting") != waiting:
                    self.store.set_flag("audit:waiting", waiting)
                    self.log("waiting", f"audit waits for: {waiting}")
                self.stop.wait(120)
                return
            self.store.set_flag("audit:waiting", None)
        repo, main = self.cfg.project.repo, self.cfg.project.main_branch
        prev = self.store.q("SELECT base_commit FROM rounds WHERE n < ? ORDER BY n DESC LIMIT 1", (n,))
        prev_base = prev[0]["base_commit"] if prev else None
        new_ids = []
        if resume:
            base = last["base_commit"]
            new_ids = [r["id"] for r in self.store.q("SELECT id FROM findings WHERE round = ? AND status = 'new'", (n,))]
            judged = self.store.q("SELECT 1 FROM findings WHERE round = ? AND status != 'new' LIMIT 1", (n,))
            stage = "fix" if last["status"] == "fixing" else ("judge" if new_ids or judged else "audit")
            self.log("round-resume", f"audit round {n} resumes at its {stage} stage after a daemon restart")
        else:
            base = sha(repo, main)
            self.store.x("INSERT INTO rounds (n, started, base_commit, status) VALUES (?,?,?, 'auditing')",
                         (n, time.time(), base))
            self.log("round-start", f"audit round {n} on {base[:10]} with fresh auditors: {', '.join(a.flavors)}")
            stage = "audit"

        if stage == "audit":
            # 1. fresh auditors, in parallel; each in its own worktree of main. The coordinator's
            # briefs are for the round: every auditor gets all of them.
            briefs = self.store.take_briefs(self.name)
            def audit(flavor_role):
                flavor, role = flavor_role
                rid, wt, br = self.fresh_tree(role)
                try:
                    task = f"Audit round {n}, flavor: {flavor}. Main is at {base}."
                    if flavor == "regression":
                        since = prev_base or git(repo, "rev-list", "--max-parents=0", main).splitlines()[0]
                        changes = git(repo, "log", "--stat", "--format=%n%h %s", f"{since}..{main}", check=False)
                        task += f"\n\nChanges on main since the last round ({since[:10]}..{base[:10]}):\n{changes[-20000:]}"
                    if briefs:
                        task += "\n\nGuidance from the coordinator:\n" + "\n".join(briefs)
                    r = self.f.runner.run(role, task, loop=self.name, worktree=wt, branch=br, run_id=rid,
                                          result_extra=FINDINGS_EXTRA)
                    return flavor, r
                finally:
                    self.f.ws.remove(wt, delete_branch=br)

            with ThreadPoolExecutor(max_workers=len(a.flavors) or 1) as ex:
                results = list(ex.map(audit, a.flavors.items()))
            ids = []
            for flavor, r in results:
                for fd in (r.result.get("findings") or []):
                    sev = str(fd.get("severity", "medium")).lower()
                    fid = self.store.x("INSERT INTO findings (round, flavor, run_id, title, severity, description, "
                                       "reproducer, status, created) VALUES (?,?,?,?,?,?,?, 'new', ?)",
                                       (n, flavor, r.run_id, str(fd.get("title", ""))[:300],
                                        sev if sev in SEVERITIES else "medium", str(fd.get("description", "")),
                                        str(fd.get("reproducer", "")), time.time()))
                    ids.append(fid)
            self.log("findings", f"round {n}: {len(ids)} findings from {len(results)} auditors")
            new_ids = ids

        # 2. the judge: reachable through the public API?
        if stage in ("audit", "judge") and new_ids:
            self.judge(n, new_ids)
        # 3. fixers, in parallel, through the gate (a fix an adopted fixer carries on is not started twice)
        busy = {json.loads(r["cycle"] or "{}").get("finding") for r in self.store.q(
            "SELECT cycle FROM runs WHERE loop = 'audit' AND status IN ('running', 'queued') AND cycle IS NOT NULL")}
        to_fix = [r for r in self.store.q(
            "SELECT * FROM findings WHERE round = ? AND status = 'fix' ORDER BY "
            "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END", (n,))
            if r["id"] not in busy]
        self.store.x("UPDATE rounds SET status = 'fixing' WHERE n = ?", (n,))
        with ThreadPoolExecutor(max_workers=max(1, a.fixers)) as ex:
            list(ex.map(self.fix, to_fix))
        while not self.stop.is_set() and self._fixers.busy():
            self.stop.wait(15)      # adopted fix cycles (agent, rebase, gate) finish before the round is stamped
        if self.stop.is_set():
            return False
        # 4. document the unreachable ones and restamp the evidence, through the gate as well
        counts = self.restamp(n, base)
        crit = counts.get("reachable_critical", 0)
        self.store.x("UPDATE rounds SET ended = ?, counts = ?, status = 'done' WHERE n = ?",
                     (time.time(), json.dumps(counts), n))
        self.log("round-end", f"audit round {n}: {json.dumps(counts)}")
        # 5. one more round? (asked at the start of the next iteration when confirm_each_round)
        if not a.confirm_each_round and crit <= a.stop_when_critical_at_most:
            self.log("audit-done", f"round {n} found {crit} critical reachable findings: converged")
            return False

    def confirm(self, n: int, counts: dict) -> bool | None:
        """Ask once whether to run another round after round `n`, and wait for the answer.
        True/False is the answer; None means the loop is stopping."""
        key = f"audit:confirm:{n}"
        did = self.store.flag(key)
        if did is None:
            did = self.store.ask(f"Audit round {n} is merged: {counts.get('findings', 0)} findings, "
                                 f"{counts.get('fixed', 0)} fixed, {counts.get('documented', 0)} not reachable; "
                                 f"{counts.get('reachable_critical', 0)} critical found this round, "
                                 f"{counts.get('open_critical', 0)} of them still open. Run another round?",
                                 ["yes", "no"])
            self.store.set_flag(key, str(did))
        while not self.stop.is_set():
            ans = self.store.answer(int(did))
            if ans:
                return ans.strip().lower() in ("yes", "y")
            self.stop.wait(30)
        return None

    def judge(self, n: int, ids: list[int]) -> None:
        rows = self.store.q(f"SELECT * FROM findings WHERE id IN ({','.join('?' * len(ids))})", ids)
        listing = "\n\n".join(f"## Finding {r['id']} ({r['flavor']}, claimed {r['severity']}): {r['title']}\n\n"
                              f"{r['description']}\n\nReproducer:\n```\n{r['reproducer'][:6000]}\n```" for r in rows)
        rid, wt, br = self.fresh_tree(self.cfg.audit.judge)
        try:
            r = self.f.runner.run(self.cfg.audit.judge, f"Round {n} findings:\n\n{listing}", loop=self.name,
                                  worktree=wt, branch=br, run_id=rid,
                                  result_extra=', "verdicts": [{"id": 0, "reachable": true, "severity": "...", "reason": "...", '
                                               '"duplicate_of": null}]')
        finally:
            self.f.ws.remove(wt, delete_branch=br)
        verdicts = {int(v.get("id", -1)): v for v in (r.result.get("verdicts") or []) if str(v.get("id", "")).isdigit()
                    or isinstance(v.get("id"), int)}
        for row in rows:
            v = verdicts.get(row["id"])
            if v is None:   # unjudged: be safe, treat as reachable
                v = {"reachable": True, "reason": "the judge gave no verdict; treated as reachable"}
            sev = str(v.get("severity") or row["severity"]).lower()
            dup = v.get("duplicate_of")
            dup = int(dup) if str(dup).isdigit() and int(dup) != row["id"] and int(dup) in verdicts else None
            status = "documented" if not v.get("reachable") else ("duplicate" if dup is not None else "fix")
            if dup is not None:
                v["duplicate_of"] = dup
            self.store.x("UPDATE findings SET status = ?, verdict = ?, severity = ? WHERE id = ?",
                         (status, json.dumps(v), sev if sev in SEVERITIES else row["severity"], row["id"]))
        # a duplicate of a duplicate, or of a finding judged unreachable, is fixed on its own
        for row in self.store.q(f"SELECT * FROM findings WHERE id IN ({','.join('?' * len(ids))}) "
                                "AND status = 'duplicate'", ids):
            prim = self.store.q("SELECT status FROM findings WHERE id = ?",
                                (json.loads(row["verdict"])["duplicate_of"],))
            if not prim or prim[0]["status"] != "fix":
                self.store.x("UPDATE findings SET status = 'fix' WHERE id = ?", (row["id"],))

    def fix(self, row, resume=None) -> None:
        # at most audit.fixers at once, counting fixers adopted from a previous daemon
        self._fixers.acquire(force=resume is not None)
        try:
            self._fix(row, resume)
        finally:
            self._fixers.release()

    def _fix(self, row, resume=None) -> None:
        now = self.store.q("SELECT status FROM findings WHERE id = ?", (row["id"],))
        if resume is None and now and now[0]["status"] != "fix":
            return      # closed meanwhile (a duplicate, or fixed by hand)
        task = (f"Finding {row['id']} (audit round {row['round']}, {row['flavor']}, {row['severity']}): {row['title']}\n\n"
                f"{row['description']}\n\nReproducer:\n```\n{row['reproducer'][:8000]}\n```\n\n"
                f"Judge's verdict: {row['verdict']}")
        out = self.cycle(self.cfg.audit.fixer, task, max_attempts=3, resume=resume, state={"finding": row["id"]})
        if out.status == "superseded":
            return
        # a fixer that changed nothing found the finding already fixed or no longer reproducible on
        # main: its own status (it says why in its summary), neither fixed nor a failed fix
        status = "fixed" if out.ok else ("no-change" if out.status == "no-change" else "open")
        self.store.x("UPDATE findings SET status = ?, fix_run = ? WHERE id = ?",
                     (status, out.run_ids[-1] if out.run_ids else None, row["id"]))
        if out.ok:
            self.store.x("UPDATE findings SET status = 'fixed', fix_run = ? WHERE status = 'duplicate' AND "
                         "json_extract(verdict, '$.duplicate_of') = ?",
                         (out.run_ids[-1] if out.run_ids else None, row["id"]))
        self.log({"fixed": "fixed", "no-change": "fix-nochange"}.get(status, "fix-failed"),
                 f"finding {row['id']} {row['title'][:120]}: {out.status} - {out.summary[:300]}",
                 out.run_ids[-1] if out.run_ids else None)

    def adopt(self, run) -> None:
        """Only fixers are adopted (their cycle carries the finding); a round's auditors and judge
        run inside the round and are stopped by the restart."""
        fid = json.loads(run["cycle"] or "{}").get("finding")
        if fid is None:
            raise ValueError("not a fixer run")
        try:
            row = self.store.q("SELECT * FROM findings WHERE id = ?", (fid,))[0]
            self.log("adopted", f"continuing fixer {run['id']} on finding {fid} after a daemon restart", run["id"])
            self._fix(row, resume=run)
        finally:
            self._fixers.release()      # taken in adopt_in_thread

    def adopt_in_thread(self, row) -> None:
        # the adopted fix holds its place from now, so a resuming round waits for it
        if json.loads(row["cycle"] or "{}").get("finding") is not None:
            self._fixers.acquire(force=True)
        super().adopt_in_thread(row)

    def restamp(self, n: int, base: str) -> dict:
        rows = self.store.q("SELECT * FROM findings WHERE round = ?", (n,))
        counts: dict = {"findings": len(rows)}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            reachable = r["status"] in ("fix", "fixed", "open", "duplicate", "no-change")
            if reachable and r["severity"] == "critical":
                counts["reachable_critical"] = counts.get("reachable_critical", 0) + 1
                if r["status"] != "fixed":
                    counts["open_critical"] = counts.get("open_critical", 0) + 1
        rid, wt, br = self.fresh_tree("evidence")
        try:
            documented = [r for r in rows if r["status"] == "documented"]
            if documented:
                kl = wt / self.cfg.audit.known_limitations_file
                text = kl.read_text() if kl.exists() else "# Known limitations\n\nFindings judged not reachable through the public API.\n"
                for r in documented:
                    why = json.loads(r["verdict"] or "{}").get("reason", "")
                    text += f"\n## Round {n}, finding {r['id']} ({r['flavor']}): {r['title']}\n\n{r['description'][:3000]}\n\nWhy not reachable: {why}\n"
                kl.write_text(text)
            ev = wt / self.cfg.audit.evidence_file
            main = sha(self.cfg.project.repo, self.cfg.project.main_branch)
            g = self.store.q("SELECT * FROM gates WHERE status = 'green' ORDER BY id DESC LIMIT 1")
            stamp = (f"\n## Audit round {n}\n\n- main before the round: `{base}`\n- main after the fixes: `{main}`\n"
                     f"- last green gate: #{g[0]['id'] if g else '-'}\n- counts: `{json.dumps(counts)}`\n")
            for r in rows:
                stamp += f"- [{r['status']}] {r['flavor']}/{r['severity']}: {r['title'][:160]}\n"
            ev.write_text((ev.read_text() if ev.exists() else "# Audit evidence\n") + stamp)
            self.f.ws.commit_pending(wt, f"audit round {n}: evidence and known limitations")
            v = self.f.gate.submit(br, None, f"audit round {n} evidence", rebase=lambda: self.f.ws.rebase_on_main(wt))
            for _ in range(5):      # main moved meanwhile (a fix merged): rebase and resubmit
                if v.ok or v.stage not in ("stale", "race") or not self.f.ws.rebase_on_main(wt):
                    break
                v = self.f.gate.submit(br, None, f"audit round {n} evidence", rebase=lambda: self.f.ws.rebase_on_main(wt))
            counts["evidence_gate"] = "green" if v.ok else f"red: {v.reason}"
        finally:
            self.f.ws.remove(wt, delete_branch=br)
        return counts


LOOPS = {"implement": ImplementLoop, "optimize": OptimizeLoop, "audit": AuditLoop}
