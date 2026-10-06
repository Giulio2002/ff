"""`ff`: the one command for the human, the coordinator agent and the workers."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import frozen
from .config import ConfigError, load

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _dur(s: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {s!r} (use 30m, 10h, 2d)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    d = time.time() - ts
    return f"{d:.0f}s" if d < 120 else f"{d / 60:.0f}m" if d < 7200 else f"{d / 3600:.1f}h"


def _factory(args):
    from .factory import Factory
    return Factory.from_path(args.config)


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ff", description="formal-programs factory")
    ap.add_argument("--config", default=os.environ.get("FF_CONFIG", "factory.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write an example factory.yaml")
    p.add_argument("--language", choices=["bend", "lean"], default="bend")
    p.add_argument("path", nargs="?", default="factory.yaml")
    sub.add_parser("validate", help="load and check the configuration")

    p = sub.add_parser("run", help="run the loops (and optionally the API) until interrupted")
    p.add_argument("--loops", default="", help="comma-separated subset of implement,optimize,audit")
    p.add_argument("--api", default="", help="also serve the steering API on [host:]port")
    p = sub.add_parser("serve", help="serve the steering API only")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)

    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("events")
    p.add_argument("--since", type=_dur, default=_dur("10h"))
    p.add_argument("--loop")
    p.add_argument("--limit", type=int, default=200)
    p = sub.add_parser("runs")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("show")
    p.add_argument("run")
    p = sub.add_parser("tail")
    p.add_argument("run")
    p.add_argument("-n", type=int, default=40)
    p.add_argument("-f", "--follow", action="store_true")
    p = sub.add_parser("steer", help="message a running agent (optionally all its live subagents)")
    p.add_argument("run")
    p.add_argument("text")
    p.add_argument("--cascade", action="store_true")
    p = sub.add_parser("stop")
    p.add_argument("run")
    p.add_argument("--no-cascade", action="store_true")
    p = sub.add_parser("brief", help="guidance for the next agents of a loop (or 'all')")
    p.add_argument("loop")
    p.add_argument("text")
    sub.add_parser("decisions")
    p = sub.add_parser("decide")
    p.add_argument("id", type=int)
    p.add_argument("answer")
    for name in ("pause", "resume"):
        p = sub.add_parser(name)
        p.add_argument("loop")
    p = sub.add_parser("findings")
    p.add_argument("--round", type=int)
    p = sub.add_parser("backlog", help="the implementation backlog; `reopen <item|all>` puts blocked items back")
    p.add_argument("action", nargs="?", choices=["reopen"])
    p.add_argument("item", nargs="?")
    p = sub.add_parser("references", help="fetch and list the benchmark's reference implementations")
    p.add_argument("--update", action="store_true")
    sub.add_parser("gates")
    sub.add_parser("experiments")
    sub.add_parser("bill", help="tokens and cost per role and model")

    p = sub.add_parser("gate", help="gate a branch now (green moves main)")
    p.add_argument("branch")
    p = sub.add_parser("gate-run", help="the gate, for a remote build server")
    p.add_argument("branch")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check", help="the gate's checks on the current worktree")
    p.add_argument("--files", nargs="*")
    p.add_argument("--no-tests", action="store_true")
    p.add_argument("--no-regenerate", action="store_true")
    p = sub.add_parser("freeze", help="lock the current frozen statements on main (human bootstrap)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true", help="replace an existing lock")

    p = sub.add_parser("note", help="(agents) a progress note for the coordinator")
    p.add_argument("text")
    sub.add_parser("inbox", help="(agents) messages sent to this run")
    p = sub.add_parser("agent", help="start/wait an ad-hoc agent")
    asub = p.add_subparsers(dest="action", required=True)
    q = asub.add_parser("start")
    q.add_argument("role")
    q.add_argument("task")
    q = asub.add_parser("wait")
    q.add_argument("run")
    q.add_argument("--timeout", type=float)

    p = sub.add_parser("subagent", help="(agents) start, steer and wait for subagents")
    ssub = p.add_subparsers(dest="action", required=True)
    for name in ("start", "run"):
        q = ssub.add_parser(name)
        q.add_argument("role")
        q.add_argument("task")
        q.add_argument("--own-worktree", action="store_true")
        q.add_argument("--timeout", type=float)
    for name in ("wait", "stop"):
        q = ssub.add_parser(name)
        q.add_argument("run")
        q.add_argument("--timeout", type=float)
    q = ssub.add_parser("steer")
    q.add_argument("run")
    q.add_argument("text")
    q.add_argument("--cascade", action="store_true")
    q = ssub.add_parser("status")
    q.add_argument("run", nargs="?")

    p = sub.add_parser("add_login", aliases=["add-login"],
                       help="add a Claude Code or Codex subscription to the rotation pool (~/.formal-agents)")
    p.add_argument("kind", choices=["claude", "codex"])
    p.add_argument("name", nargs="?", help="account name (default: <kind>-<n>)")
    p.add_argument("--max-parallel", type=int, default=0, help="at most this many agents at once on it (0 = no cap)")
    p.add_argument("login_args", nargs=argparse.REMAINDER,
                   help="passed to the CLI's login, after --: e.g. -- --device-auth (codex), -- --email me@x (claude)")
    p = sub.add_parser("relogin", help="run the login flow again for an existing account")
    p.add_argument("name")
    p.add_argument("login_args", nargs=argparse.REMAINDER)
    p = sub.add_parser("logins", help="the accounts in the rotation pool")
    p.add_argument("--check", action="store_true", help="ask each CLI whether the login is still valid")
    p = sub.add_parser("remove_login", aliases=["remove-login"])
    p.add_argument("name")
    p.add_argument("--delete", action="store_true", help="also delete its config directory (credentials)")
    p = sub.add_parser("account", help="enable, disable or cool down an account by hand")
    p.add_argument("name")
    p.add_argument("action", choices=["enable", "disable", "cooldown", "clear"])
    p.add_argument("duration", nargs="?", type=_dur, help="for cooldown, e.g. 2h")

    p = sub.add_parser("chat", help="talk to the coordinator: Claude Code, with the factory's stage at the bottom")
    p.add_argument("--print", dest="print_", metavar="MESSAGE", help="ask once and print the answer")
    sub.add_parser("statusline", help="one line: the stage the factory is at (Claude Code's status line)")
    p = sub.add_parser("digest", help="what happened since the last digest (a hook adds it to each chat message)")
    p.add_argument("--all", action="store_true", help="every event kind, not only the important ones")
    p = sub.add_parser("watch", help="stream the important events as they happen (for a Monitor)")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("_run-agent")
    p.add_argument("run")

    a = ap.parse_args(argv)
    try:
        return COMMANDS[a.cmd.replace("-", "_")](a) or 0
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2


# ===================================================================== commands

def cmd_init(a):
    src = EXAMPLES / f"factory.{a.language}.yaml"
    dst = Path(a.path)
    if dst.exists():
        print(f"{dst} exists; not overwriting", file=sys.stderr)
        return 1
    shutil.copy(src, dst)
    print(f"wrote {dst}; edit project.repo, the commands and the providers, then `ff validate`")


def cmd_validate(a):
    cfg = load(a.config)
    print(f"ok: {cfg.project.name} ({cfg.project.language}), repo {cfg.project.repo}, state {cfg.project.state_dir}")
    for r in cfg.roles.values():
        prov = cfg.provider_for(r)
        print(f"  role {r.name:<20} {prov.name}/{prov.kind} model={cfg.model_for(r)}"
              + (f" subagents={','.join(r.subagents)}" if r.subagents else "") + (" (subagent only)" if r.subagent_only else ""))
    for name, prov in cfg.providers.items():
        if prov.kind != "script" and not shutil.which(prov.binary):
            print(f"  warning: provider {name}: '{prov.binary}' is not on PATH")
    if not (cfg.project.repo / ".git").exists():
        print(f"  warning: {cfg.project.repo} is not a git repository")


def cmd_run(a):
    f = _factory(a)
    names = [n for n in a.loops.split(",") if n] or None
    srv = None
    if a.api:
        import threading
        from .api import serve
        host, _, port = a.api.rpartition(":")
        srv = serve(f, host or "127.0.0.1", int(port))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"steering API on http://{host or '127.0.0.1'}:{port}  token: {srv.token}", flush=True)
    f.start_loops(names)
    print(f"factory {f.cfg.project.name} running loops: {', '.join(f.loops) or '(none)'}; state in "
          f"{f.cfg.project.state_dir}", flush=True)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    try:
        while not stop and any(t.is_alive() for l in f.loops.values() for t in l.threads):
            f.maybe_reload()
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    f.stop_loops()
    if srv:
        srv.shutdown()
    print("factory stopped (running agents finish their current step; `ff stop` ends them)", flush=True)
    # do not wait for the loops' threads: they wait on agents that run in processes of their own and
    # that the next daemon adopts (an audit round's fixer pool would otherwise hold the exit for hours)
    os._exit(0)


def cmd_serve(a):
    from .api import serve
    f = _factory(a)
    srv = serve(f, a.host, a.port)
    print(f"steering API on http://{a.host}:{a.port}  token: {srv.token}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_status(a):
    f = _factory(a)
    s = f.status()
    if a.json:
        return _print_json(s)
    print(f"{s['project']} ({s['language']})  main {s['main']}  spent ${s['cost_usd']}"
          + (f"  PAUSED: {', '.join(s['paused'])}" if s["paused"] else ""))
    g = s["last_gate"]
    if g:
        print(f"last gate #{g['id']} {g['status']} {g['branch']} {_ago(g['ended'] or g['started'])} ago"
              + (f" ({g['reason']})" if g["status"] == "red" else ""))
    if s["backlog"]:
        print("backlog: " + ", ".join(f"{k} {v}" for k, v in s["backlog"].items()))
    if s.get("optimize_target"):
        t = s["optimize_target"]
        print(f"optimize target {t['target']}: " + ("reached on main, optimizer idle" if t["reached"] else "not met"))
    if s.get("audit_waiting"):
        print(f"audit waits for: {s['audit_waiting']}")
    if s["audit_round"]:
        r = s["audit_round"]
        print(f"audit round {r['n']}: {r['status']} {r['counts'] or ''}")
    print(f"{len(s['running'])} agent(s) running:")
    for r in s["running"]:
        print(f"  {r['id']:<36} {r['loop'] or '':<10} {_ago(r['started'])}  {(r['task'] or '').splitlines()[0][:70]}"
              + (f"  (child of {r['parent']})" if r["parent"] else ""))
    if s.get("accounts"):
        print("accounts: " + ", ".join(f"{x['name']} ({x['state']})" for x in s["accounts"]))
    for d in s["open_decisions"]:
        print(f"DECISION #{d['id']}: {d['question']} {d['options']}  ->  ff decide {d['id']} <answer>")


def cmd_events(a):
    f = _factory(a)
    for e in f.store.events(time.time() - a.since, a.loop, a.limit):
        msg = " ".join((e["message"] or "").split())   # one event, one line
        print(f"{time.strftime('%m-%d %H:%M', time.localtime(e['ts']))} {e['loop']:<9} {e['kind']:<16} {msg}"
              + (f"  [{e['run_id']}]" if e["run_id"] else ""))


def cmd_runs(a):
    f = _factory(a)
    rows = f.store.q("SELECT * FROM runs " + ("WHERE status = ? " if a.status else "") + "ORDER BY started DESC LIMIT ?",
                     ([a.status] if a.status else []) + [a.limit])
    for r in rows:
        u = json.loads(r["usage"] or "{}")
        dur = (r["ended"] or time.time()) - (r["started"] or time.time())
        print(f"{r['id']:<36} {r['status']:<8} {r['loop'] or '':<9} {r['provider']}/{r['model'] or '-'} "
              f"[{r['account'] or 'default'}] "
              f"{dur / 60:.0f}m ${u.get('cost_usd', 0)}  {(r['summary'] or r['task'] or '').splitlines()[0][:60] if (r['summary'] or r['task']) else ''}")


def cmd_show(a):
    v = _factory(a).run_view(a.run)
    if v is None:
        print("no such run", file=sys.stderr)
        return 1
    v.pop("transcript_tail", None)
    _print_json(v)


def _render(line: str) -> str:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return line.rstrip()
    t = ev.get("type")
    if t == "assistant":
        out = []
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") == "text":
                out.append("assistant: " + c["text"].strip()[:2000])
            elif c.get("type") == "tool_use":
                out.append(f"tool {c.get('name')}: {json.dumps(c.get('input'))[:300]}")
        return "\n".join(out)
    if t == "user":
        for c in ev.get("message", {}).get("content", []):
            if isinstance(c, dict) and c.get("type") == "tool_result":
                txt = c.get("content")
                txt = txt if isinstance(txt, str) else json.dumps(txt)
                return f"  -> {txt[:300]}"
            if isinstance(c, dict) and c.get("type") == "text":
                return "user: " + c["text"][:500]
        return ""
    if t == "result":
        return f"== turn done: {ev.get('subtype')} ${ev.get('total_cost_usd')}"
    if t in ("item.completed", "item.started"):
        it = ev.get("item", {})
        return f"{it.get('type')}: {(it.get('text') or it.get('command') or '')[:500]}"
    return ""


def cmd_tail(a):
    f = _factory(a)
    r = f.store.run(a.run)
    if r is None or not r["transcript"]:
        print("no transcript yet", file=sys.stderr)
        return 1
    path = Path(r["transcript"])
    lines = path.read_text(errors="replace").splitlines()[-a.n:]
    for l in lines:
        s = _render(l)
        if s:
            print(s)
    if a.follow:
        with path.open() as fh:
            fh.seek(0, 2)
            while True:
                l = fh.readline()
                if not l:
                    if f.store.run(a.run)["status"] in ("done", "blocked", "failed", "timeout", "stopped"):
                        return
                    time.sleep(1)
                    continue
                s = _render(l)
                if s:
                    print(s, flush=True)


def cmd_steer(a):
    f = _factory(a)
    if not f.store.run(a.run):
        print("no such run", file=sys.stderr)
        return 1
    sender = os.environ.get("FF_RUN_ID", "human")
    ids = f.runner.steer(a.run, a.text, sender=sender, cascade=a.cascade)
    if ids and ids[0] != a.run:
        print(f"{a.run} had finished: continued as {ids[0]} (same session, same worktree)")
    else:
        print("delivered to: " + ", ".join(ids))


def cmd_stop(a):
    print("stopping: " + ", ".join(_factory(a).runner.stop(a.run, not a.no_cascade)))


def cmd_brief(a):
    f = _factory(a)
    bid = f.store.x("INSERT INTO briefs (loop, text, status, ts) VALUES (?,?, 'open', ?)", (a.loop, a.text, time.time()))
    f.store.event(a.loop, "brief", f"brief #{bid}: {a.text[:200]}")
    print(f"brief #{bid} queued for {a.loop}")


def cmd_decisions(a):
    for d in _factory(a).store.q("SELECT * FROM decisions WHERE answer IS NULL"):
        print(f"#{d['id']} ({_ago(d['asked'])} ago) {d['question']} options: {d['options']}")


def cmd_decide(a):
    f = _factory(a)
    f.store.x("UPDATE decisions SET answer = ?, answered = ? WHERE id = ?", (a.answer, time.time(), a.id))
    f.store.event("factory", "decided", f"decision #{a.id}: {a.answer}")


def cmd_pause(a):
    f = _factory(a)
    f.store.set_flag(f"paused:{a.loop}", "1")
    f.store.event(a.loop, "pause", f"loop {a.loop} paused")


def cmd_resume(a):
    f = _factory(a)
    if a.loop == "all":   # every pause, the global one and each loop's
        f.store.x("DELETE FROM flags WHERE key LIKE 'paused:%'")
    f.store.set_flag(f"paused:{a.loop}", None)
    f.store.event(a.loop, "resume", f"loop {a.loop} resumed")


def cmd_findings(a):
    f = _factory(a)
    rows = f.store.q("SELECT * FROM findings " + ("WHERE round = ? " if a.round else "") + "ORDER BY id",
                     [a.round] if a.round else [])
    for r in rows:
        print(f"#{r['id']:<4} r{r['round']} {r['flavor']:<10} {r['severity']:<8} {r['status']:<10} {r['title'][:90]}")


def cmd_backlog(a):
    f = _factory(a)
    if a.action == "reopen":
        if a.item == "all":
            n = f.store.x("UPDATE backlog SET status = 'open', attempts = 0 WHERE status = 'blocked'")
        else:
            f.store.x("UPDATE backlog SET status = 'open', attempts = 0 WHERE item = ? OR item LIKE ?",
                      (a.item, f"%{a.item}%"))
        f.store.event("implement", "reopen", f"backlog reopened: {a.item}")
    for r in f.store.q("SELECT * FROM backlog ORDER BY status, updated"):
        print(f"{r['status']:<8} attempts={r['attempts']} {r['item'][:110]}" + (f"  [{r['run_id']}]" if r["run_id"] else ""))


def cmd_references(a):
    from .references import fetch, files
    cfg = load(a.config)
    if not cfg.benchmark.references:
        print("no references configured (benchmark.references)")
    for ref in cfg.benchmark.references:
        loc = fetch(cfg, ref, update=a.update)
        fs = files(cfg, ref)
        print(f"{ref.name}: {loc} ({len(fs)} key files)")
        for f in fs:
            print(f"  {f}")


def cmd_gates(a):
    for g in _factory(a).store.q("SELECT * FROM gates ORDER BY id DESC LIMIT 30"):
        dur = (g["ended"] or time.time()) - g["started"]
        print(f"#{g['id']:<4} {g['status']:<6} {dur / 60:5.1f}m {g['branch']:<40} {g['reason'] or ''}  {g['log'] or ''}")


def cmd_experiments(a):
    for e in _factory(a).store.q("SELECT * FROM experiments ORDER BY id DESC LIMIT 50"):
        print(f"#{e['id']:<4} {'KEPT' if e['kept'] else 'reverted':<8} {e['baseline']} -> {e['candidate']}  {e['idea'][:80]}  ({e['reason'][:60]})")


def cmd_bill(a):
    f = _factory(a)
    rows = f.store.q("SELECT role, provider, model, COUNT(*) n, "
                     "SUM(json_extract(usage,'$.cost_usd')) cost, SUM(json_extract(usage,'$.output_tokens')) out, "
                     "SUM(json_extract(usage,'$.cache_read_input_tokens')) cr, "
                     "SUM(json_extract(usage,'$.cache_creation_input_tokens')) cw "
                     "FROM runs GROUP BY role, provider, model ORDER BY cost DESC")
    total = 0
    print(f"{'role':<20} {'provider/model':<34} {'runs':>5} {'output':>10} {'cache rd':>12} {'cache wr':>12} {'cost':>10}")
    for r in rows:
        total += r["cost"] or 0
        print(f"{r['role']:<20} {(r['provider'] + '/' + (r['model'] or '-'))[:34]:<34} {r['n']:>5} {r['out'] or 0:>10} "
              f"{r['cr'] or 0:>12} {r['cw'] or 0:>12} ${r['cost'] or 0:>9.2f}")
    print(f"total ${total:.2f} (reported by the CLIs; codex reports tokens only)")


def cmd_gate(a):
    v = _factory(a).gate.submit(a.branch, None, "manual")
    print(("GREEN" if v.ok else f"RED at {v.stage}: {v.reason}") + "\n" + v.log[-4000:])
    return 0 if v.ok else 1


def cmd_gate_run(a):
    v = _factory(a).gate.submit(a.branch, None, "remote")
    if a.json:
        print(json.dumps({"ok": v.ok, "stage": v.stage, "reason": v.reason, "log": v.log[-20000:]}))
    else:
        print(("GREEN" if v.ok else f"RED at {v.stage}: {v.reason}") + "\n" + v.log[-4000:])
    return 0 if v.ok else 1


def cmd_check(a):
    from .gate import verify_tree
    from .git import git as _git
    cfg = load(a.config)
    root = Path(os.environ.get("FF_WORKTREE") or _git(Path.cwd(), "rev-parse", "--show-toplevel"))
    lock = frozen.load_lock(_git(root, "show", f"{cfg.project.main_branch}:{cfg.spec.lock_file}", check=False) or None)
    v = verify_tree(cfg, root, lock, regenerate=not a.no_regenerate, only=a.files, run_tests=not a.no_tests,
                    full=not a.files)
    print(("OK: the gate's checks pass on this tree" if v.ok else f"FAIL at {v.stage}: {v.reason}") + "\n\n" + v.log[-8000:])
    return 0 if v.ok else 1


def cmd_freeze(a):
    """Bootstrap: write the lock for the current frozen statements directly on main. This is the
    human freezing the spec; afterwards only the gate writes the lock."""
    from .git import git as _git
    cfg = load(a.config)
    repo, main = cfg.project.repo, cfg.project.main_branch
    existing = _git(repo, "show", f"{main}:{cfg.spec.lock_file}", check=False)
    if existing and not a.force:
        print(f"{cfg.spec.lock_file} already exists on {main}; use --force to replace it", file=sys.stderr)
        return 1
    from .factory import Factory
    f = Factory(cfg)
    with f.main_view("freeze") as view:
        stmts = frozen.collect(view, cfg.spec.frozen, cfg.project.language)
    print(f"{len(stmts)} statements in {', '.join(cfg.spec.frozen)}:")
    for k in sorted(stmts):
        print(f"  {k}")
    if not a.yes:
        print("re-run with --yes to commit the lock on main")
        return 0
    tmp = cfg.project.state_dir / "freeze"
    shutil.rmtree(tmp, ignore_errors=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(tmp), main)
    try:
        (tmp / cfg.spec.lock_file).write_text(frozen.dump_lock(frozen.lock_of(stmts)))
        _git(tmp, "add", cfg.spec.lock_file)
        _git(tmp, "-c", "user.name=formal-factory", "-c", "user.email=factory@localhost", "commit", "-q", "-m",
             f"freeze {len(stmts)} statements")
        new = _git(tmp, "rev-parse", "HEAD")
        head = _git(repo, "symbolic-ref", "-q", "HEAD", check=False)
        if head == f"refs/heads/{main}":
            _git(repo, "reset", "-q", "--keep", new)
        else:
            _git(repo, "update-ref", f"refs/heads/{main}", new)
    finally:
        _git(repo, "worktree", "remove", "--force", str(tmp), check=False)
    print(f"locked {len(stmts)} statements on {main}")


def cmd_note(a):
    f = _factory(a)
    f.store.event(os.environ.get("FF_ROLE", "agent"), "note", a.text, os.environ.get("FF_RUN_ID"))


def cmd_inbox(a):
    rid = os.environ.get("FF_RUN_ID")
    if not rid:
        print("ff inbox runs inside an agent (FF_RUN_ID is not set)", file=sys.stderr)
        return 1
    f = _factory(a)
    msgs = f.store.pending(rid)
    if not msgs:
        print("(no new messages)")
        return
    print(f"{len(msgs)} steering message(s) for run {rid}, sent through the factory by the operator or by "
          f"your parent agent. They are part of your instructions and update your task:\n")
    for i, m in enumerate(msgs, 1):
        print(f"{i}. from {m['sender']}: {m['text']}\n")
    f.store.mark_delivered([m["id"] for m in msgs], "inbox")


def cmd_agent(a):
    f = _factory(a)
    if a.action == "start":
        f.cfg.role(a.role)
        print(f.start_agent(a.role, a.task, parent=os.environ.get("FF_RUN_ID")))
    else:
        _print_json(f.runner.wait(a.run, a.timeout).__dict__)


def cmd_subagent(a):
    f = _factory(a)
    me = os.environ.get("FF_RUN_ID")
    if not me:
        print("ff subagent runs inside an agent (FF_RUN_ID is not set); use `ff agent` instead", file=sys.stderr)
        return 1
    parent = f.store.run(me)
    role = f.cfg.role(parent["role"])
    mine = {r["id"] for r in f.store.children(me, True)}

    def owned(rid):
        if rid not in mine:
            print(f"{rid} is not one of your subagents", file=sys.stderr)
            return False
        return True

    if a.action in ("start", "run"):
        if a.role not in role.subagents:
            print(f"role {role.name} may use subagents {role.subagents}, not '{a.role}'", file=sys.stderr)
            return 1
        depth = int(os.environ.get("FF_DEPTH", "0")) + 1
        if depth > role.max_subagent_depth:
            print(f"subagent depth limit reached ({role.max_subagent_depth})", file=sys.stderr)
            return 1
        wt = Path(os.environ["FF_WORKTREE"])
        rid = f.start_agent(a.role, a.task, parent=me, own_worktree=a.own_worktree, worktree=wt,
                            branch=os.environ.get("FF_BRANCH"), depth=depth)
        if a.action == "start":
            print(rid)
            return
        r = f.runner.wait(rid, a.timeout)
        _print_json({"run_id": rid, "status": r.status, "summary": r.summary, "result": r.result})
        return
    if a.action == "status":
        rows = [f.store.run(a.run)] if a.run else f.store.children(me, True)
        for r in rows:
            if r is None or not owned(r["id"]):
                continue
            print(f"{r['id']:<36} {r['role']:<16} {r['status']:<8} {(r['summary'] or r['task'] or '').splitlines()[0][:80]}")
        return
    if not owned(a.run):
        return 1
    if a.action == "wait":
        r = f.runner.wait(a.run, a.timeout)
        _print_json({"run_id": a.run, "status": r.status, "summary": r.summary, "result": r.result})
    elif a.action == "steer":
        ids = f.runner.steer(a.run, a.text, sender=me, cascade=a.cascade)
        if ids and ids[0] != a.run:
            print(f"{a.run} had finished: continued as {ids[0]} (same session, same worktree); "
                  f"wait for it with `ff subagent wait {ids[0]}`")
        else:
            print("delivered to: " + ", ".join(ids))
    elif a.action == "stop":
        print("stopping: " + ", ".join(f.runner.stop(a.run)))


IMPORTANT = ("gate-green", "gate-red", "agent-end", "launch-error", "error", "item-done", "item-blocked", "note",
             "decision", "accounts-exhausted", "account-switch", "limit", "experiment-kept", "experiment-reverted",
             "round-start", "round-end", "round-resume", "findings", "fixed", "fix-failed", "fix-nochange", "audit-done", "config-reloaded",
             "target-reached", "target-lost", "waiting")


def _event_line(e) -> str:
    msg = " ".join((e["message"] or "").split())
    return f"{time.strftime('%H:%M', time.localtime(e['ts']))} {e['kind']}: {msg[:400]}" + (
        f" [{e['run_id']}]" if e["run_id"] else "")


def cmd_statusline(a):
    """One short line for the bottom of the chat: contract items, agents, the gate, money, attention."""
    try:
        f = _factory(a)
    except Exception as e:  # the status line must never break the chat
        print(f"ff: {e}"[:120])
        return 0
    st = f.store
    parts = [f.cfg.project.name]
    now = time.time()
    for r in st.q("SELECT item, status, updated FROM backlog WHERE item NOT LIKE 'brief: %' ORDER BY item"):
        name = r["item"].split("\t")[0].removeprefix("law:")
        name = name if len(name) <= 24 else name[:22] + "…"
        mark = {"done": "✓", "running": f"⏳{_ago(r['updated'])}", "blocked": "✗", "open": "·"}.get(r["status"], r["status"])
        if r["status"] == "done" and now - (r["updated"] or 0) > 86400 * 3:
            continue
        parts.append(f"{name} {mark}")
    running = st.q("SELECT COUNT(*) n FROM runs WHERE status = 'running'")[0]["n"]
    parts.append(f"{running} agent{'s' if running != 1 else ''}")
    g = st.q("SELECT id, status FROM gates ORDER BY id DESC LIMIT 1")
    if g:
        parts.append(f"gate #{g[0]['id']} {dict(green='✓', red='✗').get(g[0]['status'], '…')}")
    exp = st.q("SELECT candidate FROM experiments WHERE kept = 1 ORDER BY id DESC LIMIT 1")
    if exp and exp[0]["candidate"] is not None:
        t = f.cfg.benchmark.target
        parts.append(f"bench {exp[0]['candidate']:g}" + (f"/{t:g}" if t is not None else "")
                     + (" ✓" if st.flag("optimize:at-target") else ""))
    rnd = st.q("SELECT n, status FROM rounds ORDER BY n DESC LIMIT 1")
    if rnd:
        parts.append(f"audit r{rnd[0]['n']} {rnd[0]['status']}")
    elif st.flag("audit:waiting"):
        parts.append("audit waiting")
    cost = st.q("SELECT SUM(json_extract(usage, '$.cost_usd')) c FROM runs")[0]["c"] or 0
    parts.append(f"${cost:.0f}")
    attention = st.q("SELECT COUNT(*) n FROM decisions WHERE answer IS NULL")[0]["n"]
    if attention:
        parts.append(f"⚠ {attention} decision{'s' if attention > 1 else ''}")
    paused = [r["key"].split(":", 1)[1] for r in st.q("SELECT key FROM flags WHERE key LIKE 'paused:%' AND value = '1'")]
    if paused:
        parts.append("PAUSED " + ",".join(paused))
    print(" · ".join(parts))


def cmd_digest(a):
    f = _factory(a)
    key = "digest:last"
    last = float(f.store.flag(key) or (time.time() - 3600))
    rows = [e for e in f.store.events(last, None, 500) if a.all or e["kind"] in IMPORTANT]
    f.store.set_flag(key, str(time.time()))
    if rows:
        print("Factory events since the last message:")
        for e in rows[-60:]:
            print("- " + _event_line(e))


def cmd_watch(a):
    f = _factory(a)
    last = (f.store.q("SELECT MAX(id) m FROM events")[0]["m"] or 0)
    while True:
        for e in f.store.q("SELECT * FROM events WHERE id > ? ORDER BY id", (last,)):
            last = e["id"]
            if a.all or e["kind"] in IMPORTANT:
                print(_event_line(e), flush=True)
        time.sleep(5)


def cmd_chat(a):
    """The coordinator: Claude Code (or Codex) in the factory's state directory, with the `ff` tools,
    the factory's stage in the status line, and the latest events added to every message."""
    from .agents import ff_bin
    from .prompts import _Safe, default_prompt
    cfg = load(a.config)
    role = cfg.role(cfg.coordinator)
    prov = cfg.provider_for(role)
    text = _Safe(project=cfg.project.name).format_text(role.prompt or default_prompt("coordinator"))
    env = dict(os.environ, **prov.env, **role.env, FF_CONFIG=str(cfg.path))
    env["PATH"] = str(ff_bin(cfg.project.state_dir)) + os.pathsep + env.get("PATH", "")
    model = cfg.model_for(role)
    opening = ("Give me a short status report (`ff status`, `ff events --since 2h`), then start watching the "
               "factory: run `ff watch` with your Monitor tool so its events reach you, and tell me when "
               "something needs my attention.")
    if prov.kind == "claude":
        # The coordinator's own tools are pre-approved: every `ff` command (status, steer, brief, ...),
        # reading the factory's state and the target, and editing the factory's YAML. Anything else
        # still asks the human.
        allow = ["Bash(ff:*)", "Bash(ff *)", f"Read({cfg.project.state_dir}/**)", f"Read({cfg.project.repo}/**)",
                 f"Read({cfg.path})", f"Edit({cfg.path})", "Grep", "Glob"]
        settings = {"statusLine": {"type": "command", "command": "ff statusline", "padding": 0},
                    "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "ff digest"}]}]},
                    "permissions": {"allow": allow}}
        argv = [prov.binary, "--append-system-prompt", text, "--settings", json.dumps(settings)]
        argv += (["--model", model] if model else []) + prov.args + role.args
        argv += ["-p", a.print_] if a.print_ else [opening]
    elif prov.kind == "codex":
        argv = [prov.binary] + (["exec"] if a.print_ else []) + (["-m", model] if model else []) + prov.args + role.args
        argv += [text + "\n\n" + (a.print_ or opening)]
    else:
        print("the coordinator needs a claude or codex provider", file=sys.stderr)
        return 1
    cfg.project.state_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.call(argv, cwd=cfg.project.state_dir, env=env,
                           stdin=subprocess.DEVNULL if a.print_ else None)


def _strip_dashes(args: list[str]) -> list[str]:
    return args[1:] if args and args[0] == "--" else args


def cmd_add_login(a):
    from .accounts import Pool, login
    pool = Pool()
    name = a.name
    if not name:
        n = len(pool.list(a.kind)) + 1
        while pool.get(f"{a.kind}-{n}"):
            n += 1
        name = f"{a.kind}-{n}"
    try:
        return login(pool, a.kind, name, _strip_dashes(a.login_args), a.max_parallel)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1


def cmd_relogin(a):
    import subprocess as sp
    from .accounts import Account, Pool
    pool = Pool()
    r = pool.get(a.name)
    if not r:
        print(f"no account named '{a.name}'", file=sys.stderr)
        return 1
    acct = Account(r["name"], r["kind"], Path(r["dir"]))
    argv = (["claude", "auth", "login"] if acct.kind == "claude" else ["codex", "login"]) + _strip_dashes(a.login_args)
    code = sp.call(argv, env=dict(os.environ, **acct.env()))
    st = pool.status(a.name)
    pool.set(a.name, email=st.get("email") or r["email"], plan=st.get("plan") or r["plan"], cooldown_until=0)
    print("logged in" if st.get("logged_in") else f"not logged in: {st.get('detail', '')}")
    return 0 if st.get("logged_in") else (code or 1)


def cmd_logins(a):
    from .accounts import Pool
    pool = Pool()
    rows = pool.list()
    if not rows:
        print(f"no accounts in {pool.root}; add one with `ff add_login claude` or `ff add_login codex`")
        return
    now = time.time()
    print(f"{'name':<16} {'kind':<7} {'email/plan':<34} {'state':<24} {'active':>6} {'runs':>5} {'limits':>6} {'cost':>9}")
    for r in rows:
        state = "disabled" if r["disabled"] else (
            f"cooling until {time.strftime('%m-%d %H:%M', time.localtime(r['cooldown_until']))}"
            if r["cooldown_until"] > now else "ready")
        who = " ".join(x for x in (r["email"], r["plan"]) if x) or "-"
        if a.check:
            st = pool.status(r["name"])
            if not st.get("logged_in"):
                state = "NOT LOGGED IN"
        print(f"{r['name']:<16} {r['kind']:<7} {who[:34]:<34} {state:<24} {r['active']:>6} {r['runs']:>5} "
              f"{r['limit_hits']:>6} ${r['cost_usd']:>8.2f}")


def cmd_remove_login(a):
    from .accounts import Pool
    try:
        Pool().remove(a.name, a.delete)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"removed {a.name}" + (" and its config directory" if a.delete else " (its config directory is kept)"))


def cmd_account(a):
    from .accounts import Pool
    pool = Pool()
    if not pool.get(a.name):
        print(f"no account named '{a.name}'", file=sys.stderr)
        return 1
    if a.action == "enable":
        pool.set(a.name, disabled=0)
    elif a.action == "disable":
        pool.set(a.name, disabled=1)
    elif a.action == "clear":
        pool.set(a.name, cooldown_until=0, cooldown_reason=None, active=0)
    else:
        pool.set(a.name, cooldown_until=time.time() + (a.duration or 3600), cooldown_reason="set by hand")
    print(f"{a.name}: {a.action}")


def cmd__run_agent(a):
    f = _factory(a)
    r = f.runner.execute_queued(a.run)
    # A detached run in a worktree of its own (ad-hoc agents, `--own-worktree` subagents): remove the
    # worktree when it ends; its branch stays for whoever merges it. A subagent sharing its parent's
    # worktree leaves it alone.
    row = f.store.run(a.run)
    parent = f.store.run(row["parent"]) if row and row["parent"] else None
    wt = Path(row["worktree"]) if row and row["worktree"] else None
    if (wt and wt.parent == f.ws.root and row["loop"] in ("adhoc", "subagent")
            and (parent is None or parent["worktree"] != row["worktree"])):
        f.ws.remove(wt)
    return 0 if r.status in ("done", "blocked") else 1


COMMANDS = {k[4:]: v for k, v in globals().items() if k.startswith("cmd_")}
