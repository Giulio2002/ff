"""A read-only web page that shows a factory's progress in plain language.

`ff run --webui 0.0.0.0:8080` (alongside the loops) or `ff webui --listen 0.0.0.0:8080` (on its own,
next to a running daemon). It serves one page and one JSON document (`/state.json`), both built from
the store. Nothing here can steer the factory, and it never serves transcripts, prompts, logs,
reproducers or credentials, so it is safe to expose. `--webui-token` (or FF_WEBUI_TOKEN) puts it
behind a password: open the page once as /?token=<token> and the browser keeps it.

Details (the text of the specification's and the contract's declarations, and each running agent's
last few actions) are shown only
when the code is not secret from the page's viewers: the GitHub repository is public, the page has a
token, or `--webui-details` says so.
"""
from __future__ import annotations

import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import decls
from .git import git
from .globs import in_globs as _in_globs
from .store import Store

PAGE = Path(__file__).with_name("webui.html")
SEVERITIES = ("critical", "high", "medium", "low")

# event kinds worth showing to a human, and how to say them
SHOWN = {
    "gate-green": "merged", "gate-red": "rejected by the gate", "experiment-kept": "speed-up kept",
    "experiment-reverted": "experiment reverted", "target-reached": "speed target met",
    "target-lost": "speed target missed", "round-start": "audit round started",
    "round-end": "audit round finished", "findings": "auditors reported", "fixed": "finding fixed",
    "fix-failed": "fix failed", "fix-nochange": "nothing to fix", "audit-done": "audit converged",
    "item-done": "item done", "item-blocked": "item blocked", "decision": "waiting for a human",
    "decided": "decided", "accounts-exhausted": "out of credits", "launch-error": "agent could not start",
    "error": "error", "loop-start": "loop started", "round-resume": "audit round resumed",
    "agent-end": "agent finished", "spec-approved": "specification approved",
    "spec-declined": "specification sent back", "push-failed": "push failed", "agent-start": "agent started",
}
ROLE_WORDS = {
    "implementer": "Implementing", "optimizer": "Optimizing", "auditor_mutation": "Auditing (mutations)",
    "auditor_crash": "Auditing (hostile inputs)", "auditor_regression": "Auditing (regressions)",
    "judge": "Judging findings", "fixer": "Fixing", "coordinator": "Coordinating", "specifier": "Writing the spec",
}


def _first_line(s: str | None, n: int = 160) -> str:
    s = (s or "").strip().splitlines()[0] if (s or "").strip() else ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _cost(usage: str | None) -> float:
    try:
        return float(json.loads(usage or "{}").get("cost_usd") or 0)
    except (ValueError, TypeError):
        return 0.0


# ------------------------------------------------------------------------------------ helpers

_cache: dict = {}
_cache_lock = threading.Lock()


def _cached(key, ttl: float, fn):
    """fn() at most once per `ttl` seconds per key (the page polls every few seconds)."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _part(item: str | None) -> str:
    """The part of the program an item is about: `spec-gas` and `impl-gas` are both about `gas`."""
    if not item:
        return ""
    if item.startswith("brief: "):
        return "brief"
    return item.split("-", 1)[1] if "-" in item else item


def _short_role(role: str) -> str:
    return role.replace("auditor_", "auditor·").replace("_", " ")


def _blocks(text: str, language: str) -> dict[str, str]:
    """Every top-level declaration's full text (statement and proof) by its last name component."""
    return {d.short: d.block for d in decls.of(language, text)}


def _show(repo, rev: str, path: str) -> str:
    return git(repo, "show", f"{rev}:{path}", check=False) or ""


def _statements(cfg, main: str) -> list[dict]:
    """The contract, as declarations on main: everything in the specification (spec.files), and the
    laws and theorems stated in the harness (spec.immutable: e.g. LAWS.bend), in file order."""
    def build():
        repo, lang = cfg.project.repo, cfg.project.language
        ext = ".lean" if lang == "lean" else ".bend"
        out = []
        for f in sorted((git(repo, "ls-tree", "-r", "--name-only", main, check=False) or "").splitlines()):
            spec = _in_globs(f, cfg.spec.files)
            if not f.endswith(ext) or not (spec or _in_globs(f, cfg.spec.immutable)):
                continue
            for d in decls.of(lang, _show(repo, main, f)):
                if spec or d.kind in ("law", "theorem", "lemma"):
                    out.append(dict(key=f"{f}::{d.name}", file=f, name=d.name, kind=d.kind, spec=spec,
                                    text=d.statement))
        return out
    return _cached(("statements", main), 3600, build)


def _touched(cfg, worktree: str, main: str) -> set[tuple[str, str]]:
    """(file, name) of every declaration a worktree changes or adds relative to main (committed or not)."""
    def build():
        from pathlib import Path
        wt = Path(worktree)
        if not wt.is_dir():
            return set()
        globs = cfg.checker.files + cfg.spec.files + cfg.spec.immutable
        changed = set((git(wt, "diff", "--name-only", main, check=False) or "").splitlines())
        changed |= set((git(wt, "ls-files", "--others", "--exclude-standard", check=False) or "").splitlines())
        names = set()
        for f in changed:
            if not f or not _in_globs(f, globs) or not (wt / f).is_file():
                continue
            new = _blocks((wt / f).read_text(errors="replace"), cfg.project.language)
            old = _blocks(_show(cfg.project.repo, main, f), cfg.project.language)
            names |= {(f, n) for n, t in new.items() if old.get(n) != t}
        return names
    return _cached(("touched", worktree, main), 20, build)


class _Transcript:
    """Incremental reader of a Claude Code stream-json transcript: tokens so far and the last actions."""

    def __init__(self):
        self.offset, self.usage, self.actions = 0, {}, []

    def read(self, path: str) -> "_Transcript":
        try:
            with open(path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return self
        end = chunk.rfind(b"\n")
        if end < 0:
            return self
        self.offset += end + 1
        for raw in chunk[:end].splitlines():
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if ev.get("type") != "assistant":
                continue
            msg = ev.get("message") or {}
            u = msg.get("usage") or {}
            if msg.get("id") and u:
                self.usage[msg["id"]] = (int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
                                         + int(u.get("cache_creation_input_tokens") or 0))
            for c in msg.get("content") or []:
                a = _action(c)
                if a:
                    self.actions = (self.actions + [a])[-3:]
        return self

    @property
    def tokens(self) -> int:
        return sum(self.usage.values())


_transcripts: dict[str, _Transcript] = {}


def _action(c: dict) -> str:
    if c.get("type") == "text":
        t = _first_line(c.get("text"), 110)
        return f"… {t}" if t else ""
    if c.get("type") != "tool_use":
        return ""
    name, inp = c.get("name") or "", c.get("input") or {}
    if name == "Bash":
        return "$ " + _first_line(inp.get("command"), 110)
    path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
    path = re.sub(r"^.*/worktrees/[^/]+/", "", str(path))
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "✎ " + path
    if name == "Read":
        return "… reading " + path
    if name in ("Grep", "Glob"):
        return "⌕ " + _first_line(inp.get("pattern"), 80)
    return "· " + name


def _live(run_id: str, transcript: str | None) -> _Transcript:
    t = _transcripts.setdefault(run_id, _Transcript())
    return t.read(transcript) if transcript else t


def state(cfg, store: Store, details: bool = False) -> dict:
    """Everything the page shows, in plain terms."""
    now = time.time()
    repo, main_branch = cfg.project.repo, cfg.project.main_branch
    main = git(repo, "rev-parse", main_branch, check=False)
    bench = cfg.benchmark

    backlog = [dict(item=r["item"], status=r["status"], attempts=r["attempts"], updated=r["updated"],
                    loop=r["loop"] or "implement")
               for r in store.q("SELECT * FROM backlog WHERE item NOT LIKE 'brief: %' ORDER BY item")]
    rounds = []
    for r in store.q("SELECT * FROM rounds ORDER BY n"):
        fs = store.q("SELECT id, flavor, severity, status, title FROM findings WHERE round = ? ORDER BY "
                     "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, id",
                     (r["n"],))
        rounds.append(dict(n=r["n"], status=r["status"], started=r["started"], ended=r["ended"],
                           findings=[dict(f) for f in fs]))
    converged = store.flag("audit:converged")
    at_target = store.flag("optimize:at-target") == main

    # the phase, as the loops see it
    spec_open = cfg.specify.enabled and not store.flag("spec:approved")
    open_items = [b for b in backlog if b["status"] in ("open", "running") and b["loop"] == "implement"]
    if converged:
        phase = "done"
    elif spec_open:
        phase = "specify"
    elif rounds and rounds[-1]["status"] != "done":
        phase = "audit"
    elif cfg.implement.enabled and open_items:
        phase = "implement"
    elif cfg.optimize.enabled and bench.target is not None and not at_target:
        phase = "optimize"
    elif cfg.audit.enabled:
        phase = "audit"
    else:
        phase = "idle"
    phases = [p for p, on in (("specify", cfg.specify.enabled), ("implement", cfg.implement.enabled),
                              ("optimize", cfg.optimize.enabled),
                              ("audit", cfg.audit.enabled)) if on] + ["done"]

    # the benchmark over time: experiments, plus the measurements of main logged at the target check
    series = []
    for e in store.q("SELECT ts, baseline, candidate, kept, idea FROM experiments ORDER BY ts"):
        if e["baseline"] is not None:
            series.append(dict(ts=e["ts"] - 1, value=e["baseline"], kind="main"))
        if e["kept"] and e["candidate"] is not None:
            series.append(dict(ts=e["ts"], value=e["candidate"], kind="kept", note=_first_line(e["idea"], 120)))
    for e in store.q("SELECT ts, message FROM events WHERE kind IN ('target-reached', 'target-lost') ORDER BY ts"):
        m = re.search(r"benchmark ([0-9.]+)", e["message"] or "")
        if m:
            series.append(dict(ts=e["ts"], value=float(m.group(1)), kind="main"))
    # main's latest measurement (the optimizer's baseline), even before any experiment is kept
    base = store.flag(f"baseline:{main}") if main else None
    if base:
        t = store.q("SELECT MAX(ts) t FROM events WHERE kind IN ('agent-start', 'experiment-kept', 'gate-green')")[0]["t"]
        series.append(dict(ts=t or now, value=float(base), kind="main"))
    series.sort(key=lambda p: p["ts"])
    experiments = store.q("SELECT COUNT(*) n, SUM(kept) k FROM experiments")[0]

    running = []
    for r in store.q("SELECT id, role, loop, parent, started, task FROM runs WHERE status IN ('running', 'queued') "
                     "ORDER BY started"):
        note = store.q("SELECT message FROM events WHERE run_id = ? AND kind = 'note' ORDER BY id DESC LIMIT 1",
                       (r["id"],))
        running.append(dict(id=r["id"], role=r["role"], doing=ROLE_WORDS.get(r["role"], r["role"].replace("_", " ")),
                            loop=r["loop"], subagent=bool(r["parent"]), since=r["started"],
                            task=_first_line(r["task"]), note=_first_line(note[0]["message"], 240) if note else ""))

    gates = [dict(id=g["id"], status=g["status"], reason=_first_line(g["reason"], 140),
                  started=g["started"], ended=g["ended"], who=(g["branch"] or "").removeprefix("ff/"))
             for g in store.q("SELECT id, branch, status, reason, started, ended FROM gates ORDER BY id DESC LIMIT 60")]
    gate_counts = {r["status"]: r["n"] for r in store.q("SELECT status, COUNT(*) n FROM gates GROUP BY status")}

    events = []
    for e in store.q("SELECT ts, loop, kind, run_id, message FROM events WHERE kind IN (%s) ORDER BY id DESC LIMIT 80"
                     % ",".join("?" * len(SHOWN)), tuple(SHOWN)):
        events.append(dict(ts=e["ts"], kind=e["kind"], label=SHOWN[e["kind"]], loop=e["loop"],
                           text=_first_line(e["message"], 300)))

    cost_by_role: dict[str, float] = {}
    total = 0.0
    for r in store.q("SELECT role, usage FROM runs"):
        c = _cost(r["usage"])
        total += c
        cost_by_role[r["role"]] = cost_by_role.get(r["role"], 0) + c
    first = store.q("SELECT MIN(started) t FROM runs")[0]["t"]

    # ------------------------------------------------------------------ the redesign's extra views
    show_details = bool(details or cfg.project.visibility == "public")
    runs = store.q("SELECT id, role, loop, parent, status, started, ended, usage, transcript, worktree, cycle, attempt "
                   "FROM runs ORDER BY started")
    items_of = {}
    for r in runs:
        try:
            items_of[r["id"]] = json.loads(r["cycle"] or "{}").get("item")
        except ValueError:
            items_of[r["id"]] = None

    def lane(r) -> str:
        part = _part(items_of.get(r["id"]))
        return _short_role(r["role"]) + ("·" + part if part else "")

    # the contract's declarations: settled on main (the specification once approved; a law or theorem
    # on main is proved, main is always green), being changed by a running agent, or named by its
    # last red gate
    approved = not cfg.specify.enabled or bool(store.flag("spec:approved"))
    statements = _statements(cfg, main) if main else []
    # a statement is being worked on when its own declaration changes, or (a law or theorem) when a
    # declaration of its name changes anywhere: its proof may live in another file
    touched = {r["id"]: _touched(cfg, r["worktree"], main) for r in runs
               if r["status"] in ("running", "queued") and r["worktree"]}
    proving_by: dict[str, list[str]] = {}
    for st in statements:
        short = st["name"].split(".")[-1]
        for rid, fn in touched.items():
            if (st["file"], short) in fn or (st["kind"] in ("law", "theorem", "lemma") and any(n == short for _, n in fn)):
                proving_by.setdefault(st["key"], []).append(rid)
    failed_names = set()
    live = {r["id"] for r in runs if r["status"] in ("running", "queued")}
    for g in store.q("SELECT g.run_id, g.status, g.log FROM gates g WHERE g.id IN (SELECT MAX(id) FROM gates GROUP BY run_id)"):
        if g["run_id"] in live and g["status"] == "red" and g["log"]:
            try:
                text = open(g["log"], errors="replace").read()[-20000:]
            except OSError:
                continue
            failed_names |= {st["name"].split(".")[-1] for st in statements
                             if re.search(r"\b" + re.escape(st["name"].split(".")[-1]) + r"\b", text)}
    contract = []
    for st in statements:
        short = st["name"].split(".")[-1]
        stt = ("failed" if short in failed_names else "proving" if st["key"] in proving_by
               else "proven" if approved or not st["spec"] else "open")
        contract.append(dict(file=st["file"], name=st["name"], kind=st["kind"], state=stt, spec=st["spec"],
                             text=st["text"][:1500] if show_details else ""))
    laws = [c for c in contract if c["kind"] in ("law", "theorem", "lemma")]

    # running agents: their item, attempt, tokens so far, what they touch
    by_id = {r["id"]: r for r in runs}
    attempts = {b["item"]: b["attempts"] for b in backlog}
    for a in running:
        r = by_id.get(a["id"])
        item = items_of.get(a["id"])
        t = _live(a["id"], r["transcript"] if r else None)
        a.update(item=item or "", part=_part(item), lane=lane(r) if r else a["role"],
                 attempt=(attempts.get(item, 0) or 0) + 1 if item else (r["attempt"] if r else None),
                 tokens=t.tokens, cost=_cost(r["usage"]) if r and r["usage"] else None,
                 touching=sum(1 for ids in proving_by.values() if a["id"] in ids),
                 activity=t.actions if show_details else [])

    # the agents' timeline: one lane per role and part
    t_start = runs[0]["started"] if runs else now
    timeline = [dict(id=r["id"], lane=lane(r), loop=r["loop"] or "", role=r["role"], status=r["status"],
                     start=r["started"], end=r["ended"] if r["status"] not in ("running", "queued") else None,
                     subagent=bool(r["parent"]))
                for r in runs if r["started"]]
    merges = [dict(ts=g["ended"], run=g["run_id"]) for g in
              store.q("SELECT run_id, ended FROM gates WHERE status = 'green' AND ended IS NOT NULL")]
    gate_spans = [dict(start=g["started"], end=g["ended"], status=g["status"]) for g in
                  store.q("SELECT started, ended, status FROM gates ORDER BY id")]

    # gate history: duration, and whether a refused change was tried again
    later = {}
    for g in store.q("SELECT id, run_id, status FROM gates ORDER BY id"):
        later.setdefault(g["run_id"], []).append((g["id"], g["status"]))
    for g in gates:
        row = store.q("SELECT run_id FROM gates WHERE id = ?", (g["id"],))
        rid = row[0]["run_id"] if row else None
        tries = later.get(rid, [])
        g["retried"] = any(i > g["id"] for i, _ in tries)
        g["merged_later"] = any(i > g["id"] and s == "green" for i, s in tries)
    durs = [g["end"] - g["start"] for g in gate_spans if g["end"] and g["start"]]

    # the phases: when each started and ended
    def ev_ts(kind, first=True):
        r = store.q(f"SELECT {'MIN' if first else 'MAX'}(ts) t FROM events WHERE kind = ?", (kind,))
        return r[0]["t"]
    approved_at = store.flag("spec:approved")
    try:
        approved_at = float(approved_at) if approved_at else None
    except ValueError:
        approved_at = ev_ts("spec-approved")
    spec_runs = [r["started"] for r in runs if r["loop"] == "specify" and r["started"]]
    impl_items = [b for b in backlog if b["loop"] == "implement"]
    impl_open = [b for b in impl_items if b["status"] in ("open", "running", "blocked")]
    impl_start = approved_at if cfg.specify.enabled else (min([r["started"] for r in runs if r["loop"] == "implement"]
                                                           or [None]) if runs else None)
    impl_end = (max(b["updated"] for b in impl_items) if impl_items and not impl_open and store.flag("backlog:main")
                else None)
    if impl_end is None and impl_start and not impl_open and store.flag("backlog:main") and not impl_items:
        impl_end = impl_start
    opt_end = None
    if at_target:
        r = store.q("SELECT MAX(ts) t FROM events WHERE kind = 'target-reached'")
        opt_end = r[0]["t"]
    times = {"specify": [min(spec_runs) if spec_runs else None, approved_at],
             "implement": [impl_start, impl_end],
             "optimize": [impl_end, opt_end],
             "audit": [rounds[0]["started"] if rounds else None, ev_ts("audit-done", False) if converged else None],
             "done": [ev_ts("audit-done", False) if converged else None, None]}

    # spend over time: finished runs, cumulative
    spent = sorted((r["ended"], _cost(r["usage"])) for r in runs if r["ended"] and _cost(r["usage"]))
    burn, acc = [], 0.0
    for ts, c in spent:
        acc += c
        burn.append(dict(ts=ts, total=round(acc, 4)))
    by_lane: dict[str, float] = {}
    for r in runs:
        c = _cost(r["usage"])
        if c:
            by_lane[lane(r)] = by_lane.get(lane(r), 0) + c

    events_full = []
    for e in store.q("SELECT ts, loop, kind, run_id, message FROM events WHERE kind IN (%s) ORDER BY id DESC LIMIT 120"
                     % ",".join("?" * len(SHOWN)), tuple(SHOWN)):
        msg = (e["message"] or "").strip()
        events_full.append(dict(ts=e["ts"], kind=e["kind"], label=SHOWN[e["kind"]], loop=e["loop"],
                                text=_first_line(msg, 300), full=msg[:2000], role=(by_id.get(e["run_id"]) or {"role": ""})["role"]
                                if e["run_id"] in by_id else ""))

    open_items = [b for b in backlog if b["status"] in ("open", "running", "blocked")]
    return dict(
        project=cfg.project.name, language=cfg.project.language,
        github=cfg.project.github if cfg.project.visibility == "public" else None, main=(main or "")[:10], now=now,
        started=first, phase=phase, phases=phases,
        decisions=[dict(question=d["question"]) for d in store.q("SELECT question FROM decisions WHERE answer IS NULL")],
        paused=[r["key"].split(":", 1)[1] for r in store.q("SELECT key FROM flags WHERE key LIKE 'paused:%' AND value = '1'")],
        backlog=backlog,
        benchmark=dict(target=bench.target, direction=bench.direction, series=series, unit=bench.unit,
                       current=series[-1]["value"] if series else None, at_target=at_target,
                       experiments=experiments["n"] or 0, kept=experiments["k"] or 0,
                       references=[r.name for r in bench.references]),
        running=running, gates=gates, gate_counts=gate_counts,
        audit=dict(rounds=rounds, converged=converged, converge_at=cfg.audit.converge_at,
                   waiting=store.flag("audit:waiting") or ""),
        events=events_full, cost=dict(total=round(total, 2), by_role={k: round(v, 2) for k, v in
                                                                  sorted(cost_by_role.items(), key=lambda kv: -kv[1])},
                                    by_lane={k: round(v, 2) for k, v in sorted(by_lane.items(), key=lambda kv: -kv[1])},
                                    burn=burn, budget=cfg.limits.budget_usd),
        details=show_details, versus=bench.versus, contract=contract, laws=len(laws),
        laws_proven=sum(1 for c in laws if c["state"] == "proven"),
        proving=sum(1 for c in contract if c["state"] == "proving"),
        timeline=timeline, t_start=t_start, merges=merges, gate_spans=gate_spans,
        gate_avg=(sum(durs) / len(durs)) if durs else None, times=times,
        approval=cfg.specify.approval if cfg.specify.enabled else None, spec_approved=approved,
        criteria=dict(backlog_empty=not [b for b in impl_open if not b["item"].startswith("brief: ")]
                      and bool(store.flag("backlog:main")), at_target=at_target),
        audit_after=list(cfg.audit.after or []), max_rounds=cfg.audit.max_rounds,
    )


def make_handler(cfg, store: Store, token: str | None, details: bool = False):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-content-type-options", "nosniff")
            self.send_header("referrer-policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, q: dict) -> tuple[bool, dict]:
            if not token:
                return True, {}
            if hmac.compare_digest(q.get("token", ""), token):
                return True, {"set-cookie": f"ff_webui={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"}
            cookies = dict(c.strip().split("=", 1) for c in (self.headers.get("cookie") or "").split(";") if "=" in c)
            return hmac.compare_digest(cookies.get("ff_webui", ""), token), {}

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            ok, extra = self._authorized(q)
            if not ok:
                return self._send(401, b"this page needs its token: open it as /?token=<token>",
                                  "text/plain; charset=utf-8")
            if u.path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8", extra)
            if u.path == "/state.json":
                try:
                    body = json.dumps(state(cfg, store, details or bool(token)), default=str).encode()
                except Exception as e:  # the page must keep working while the factory changes underneath
                    return self._send(500, json.dumps({"error": repr(e)}).encode(), "application/json")
                return self._send(200, body, "application/json", extra)
            return self._send(404, b"not found", "text/plain")

    return H


def serve(cfg, store: Store, listen: str, token: str | None = None, details: bool = False) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    return ThreadingHTTPServer((host or "127.0.0.1", int(port)), make_handler(cfg, store, token, details))
