"""A read-only web page that shows a factory's progress in plain language.

`ff run --webui 0.0.0.0:8080` (alongside the loops) or `ff webui --listen 0.0.0.0:8080` (on its own,
next to a running daemon). It serves one page and one JSON document (`/state.json`), both built from
the store. Nothing here can steer the factory, and it never serves transcripts, prompts, logs,
reproducers or credentials, so it is safe to expose. `--webui-token` (or FF_WEBUI_TOKEN) puts it
behind a password: open the page once as /?token=<token> and the browser keeps it.
"""
from __future__ import annotations

import hmac
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .git import git
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
    "agent-end": "agent finished", "spec-approved": "specification approved", "frozen": "specification frozen",
    "spec-declined": "specification sent back", "push-failed": "push failed",
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


def state(cfg, store: Store) -> dict:
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
        events=events, cost=dict(total=round(total, 2), by_role={k: round(v, 2) for k, v in
                                                                  sorted(cost_by_role.items(), key=lambda kv: -kv[1])}),
    )


def make_handler(cfg, store: Store, token: str | None):
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
                    body = json.dumps(state(cfg, store), default=str).encode()
                except Exception as e:  # the page must keep working while the factory changes underneath
                    return self._send(500, json.dumps({"error": repr(e)}).encode(), "application/json")
                return self._send(200, body, "application/json", extra)
            return self._send(404, b"not found", "text/plain")

    return H


def serve(cfg, store: Store, listen: str, token: str | None = None) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    return ThreadingHTTPServer((host or "127.0.0.1", int(port)), make_handler(cfg, store, token))
