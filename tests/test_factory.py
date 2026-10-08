"""End-to-end tests on a toy Bend project with a deterministic fake agent (provider kind: script).

They need a Bend 2 binary: $FF_TEST_BEND, or `bend` on PATH; otherwise the checker tests are skipped.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ff import frozen  # noqa: E402
from ff.config import ConfigError, load  # noqa: E402
from ff.factory import Factory  # noqa: E402

BEND = os.environ.get("FF_TEST_BEND") or shutil.which("bend")
needs_bend = pytest.mark.skipif(not BEND, reason="no Bend 2 binary (set FF_TEST_BEND)")

GEN = textwrap.dedent('''\
    # The generator: writes the implementation and the proof file. Agents edit this, not its output.
    from pathlib import Path
    EXTRA = []
    Path("src/add.bend").write_text("import Base\\n\\ndef add(a: Nat, b: Nat) -> Nat:\\n  Nat.add(b,a)\\n")
    proofs = ["import Base", "import ./laws.bend as Laws", "", "def Laws.add_correct(a,b): {==}"]
    for name in EXTRA:
        proofs.append(f"def Laws.{name}(a,b): {{==}}")
    Path("proofs/proof.bend").write_text("\\n".join(proofs) + "\\n")
    ''')


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_project(tmp: Path, bug: bool = True) -> Path:
    repo = tmp / "toy"
    (repo / "spec").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "proofs").mkdir()
    (repo / "spec/add.bend").write_text("import Base\n\n# addition of naturals\ndef spec_add(a: Nat, b: Nat) -> Nat:\n  Nat.add(a,b)\n")
    (repo / "proofs/laws.bend").write_text(textwrap.dedent("""\
        import Base
        import ../src/add.bend as I
        import ../spec/add.bend as S

        law add_correct:
          for +a: Nat
          for +b: Nat
          {I.add(a,b) == S.spec_add(a,b) : Nat}
        """))
    gen = GEN if bug else GEN.replace("Nat.add(b,a)", "Nat.add(a,b)")
    (repo / "gen.py").write_text(gen)
    subprocess.run([sys.executable, "gen.py"], cwd=repo, check=True)
    (repo / "TODO.txt").write_text("add_correct does not check\n" if bug else "")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "toy")
    return repo


def write_config(tmp: Path, repo: Path, **loops) -> Path:
    fake = ROOT / "tests/fake_agent.py"
    cfg = {
        "project": {"name": "toy", "repo": str(repo), "language": "bend", "state_dir": str(tmp / "state"),
                    "workflow": "generators"},
        "commands": {"regenerate": f"{sys.executable} gen.py"},
        "generated": ["src/*.bend", "proofs/proof.bend"],
        "checker": {"bend": {"binary": BEND or "bend", "args": ["--check-only"], "files": ["proofs/proof.bend"],
                             "file_timeout_seconds": 120, "jobs": 2}},
        "spec": {"frozen": ["spec/*.bend", "proofs/laws.bend"]},
        "providers": {"fake": {"kind": "script", "command": [sys.executable, str(fake)]}},
        "roles": {r: {"provider": "fake", "timeout_minutes": 5} for r in
                  ["coordinator", "implementer", "auditor_mutation", "auditor_crash", "judge", "fixer", "echo"]},
        "loops": {"implement": {"enabled": False}, "optimize": {"enabled": False}, "audit": {"enabled": False},
                  **loops},
        "limits": {"max_parallel_agents": 4, "nice": 0},
    }
    cfg["roles"]["steerable"] = {"provider": "fake", "timeout_minutes": 5, "subagents": ["echo"]}
    cfg["roles"]["echo"]["subagent_only"] = True
    p = tmp / "factory.yaml"
    import yaml
    p.write_text(yaml.safe_dump(cfg))
    return p


# ---------------------------------------------------------------- unit tests (no Bend needed)

def test_frozen_statements_bend_and_lean():
    b = frozen.bend_statements("laws.bend", "import Base\n\nlaw foo:\n  for +a: Nat\n  {a == a : Nat}\n\ndef bar(x):\n  x\n")
    assert [s.key for s in b] == ["laws.bend::foo", "laws.bend::bar"]
    lean = "theorem t (n : Nat) : n + 0 = n := by\n  simp\n\ndef f (n : Nat) : Nat := n\n"
    l1 = frozen.lean_statements("A.lean", lean)
    l2 = frozen.lean_statements("A.lean", lean.replace("simp", "omega"))
    assert {s.key: s.digest for s in l1}["A.lean::t"] == {s.key: s.digest for s in l2}["A.lean::t"], \
        "changing a proof must not change the statement"
    l3 = frozen.lean_statements("A.lean", lean.replace("n + 0 = n", "n + 0 = n ∨ True"))
    assert {s.key: s.digest for s in l1}["A.lean::t"] != {s.key: s.digest for s in l3}["A.lean::t"]


def test_config_validation(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    p = write_config(tmp_path, repo)
    cfg = load(p)
    assert cfg.roles["steerable"].subagents == ["echo"]
    bad = p.read_text().replace("provider: fake", "provider: nope", 1)
    p.write_text(bad)
    with pytest.raises(ConfigError):
        load(p)


def test_examples_load(tmp_path, monkeypatch):
    for lang in ("bend", "lean"):
        cfg = load(ROOT / f"examples/factory.{lang}.yaml")
        assert cfg.project.language == lang
        assert cfg.providers["glm"].kind == "claude" and "ANTHROPIC_BASE_URL" in cfg.providers["glm"].env
        assert cfg.providers["gpt"].kind == "codex"
        assert "explorer" in cfg.roles["implementer"].subagents


# ---------------------------------------------------------------- end to end (Bend)

@needs_bend
def test_gate_refuses_cheats_and_accepts_the_generator_fix(tmp_path):
    repo = make_project(tmp_path)
    f = Factory.from_path(write_config(tmp_path, repo))
    # freeze the spec (the human step)
    from ff.cli import main as ff
    assert ff(["--config", str(f.cfg.path), "freeze", "--yes"]) == 0
    from ff.loops import ImplementLoop
    loop = ImplementLoop(f)
    weak = loop.cycle("implementer", "CHEAT-WEAKEN", max_attempts=1)
    assert not weak.ok
    reds = f.store.q("SELECT reason FROM gates WHERE status = 'red'")
    assert any("frozen" in r["reason"] for r in reds), [dict(r) for r in reds]
    hand = loop.cycle("implementer", "CHEAT-HANDEDIT", max_attempts=1)
    assert not hand.ok
    assert any("generated files differ" in r["reason"] for r in f.store.q("SELECT reason FROM gates WHERE status = 'red'"))
    honest = loop.cycle("implementer", "fix add", max_attempts=1)
    assert honest.ok, honest
    assert "Nat.add(a,b)" in (repo / "src/add.bend").read_text() or "Nat.add(a,b)" in git(repo, "show", "main:src/add.bend")


@needs_bend
def test_audit_round_judge_fix_and_evidence(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo, audit={"enabled": True, "flavors": {"mutation": "auditor_mutation",
                                                                        "crash": "auditor_crash"},
                                            "fixers": 2, "confirm_each_round": False, "max_rounds": 1})
    f = Factory.from_path(p)
    from ff.cli import main as ff
    assert ff(["--config", str(p), "freeze", "--yes"]) == 0
    from ff.loops import AuditLoop
    loop = AuditLoop(f)
    f.store.x("INSERT INTO briefs (loop, text, status, ts) VALUES ('audit', 'FOCUS-ON-THE-GLUE', 'open', 0)")
    loop.worker(0)
    for r in f.store.q("SELECT id FROM runs WHERE role LIKE 'auditor_%'"):
        assert "FOCUS-ON-THE-GLUE" in (f.runner.runs_dir / r["id"] / "prompt.md").read_text(), \
            "every auditor of the round gets the coordinator's brief"
    rows = {r["title"]: r["status"] for r in f.store.q("SELECT * FROM findings")}
    assert rows["mutation: missing law add_comm_spec"] == "fixed"
    assert rows["crash: missing law add_comm_spec"] == "fixed", "a duplicate is closed by its primary's fix"
    assert len(f.store.q("SELECT * FROM runs WHERE role = 'fixer'")) == 1, "duplicates get no fixer of their own"
    assert rows["mutation: hand-built object"] == "documented"
    main_files = git(repo, "ls-tree", "-r", "--name-only", "main")
    assert "EVIDENCE.md" in main_files and "KNOWN_LIMITATIONS.md" in main_files
    lock = json.loads(git(repo, "show", "main:frozen.lock.json"))["statements"]
    assert any("add_again_" in k for k in lock), "the fixers' new laws are locked by the gate"


# ---------------------------------------------------------------- steering (no Bend needed)

def test_api_steers_an_agent_and_the_agent_steers_its_subagent(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    f = Factory.from_path(p)
    from ff.api import serve
    srv = serve(f, "127.0.0.1", 0, token="t0k")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"authorization": "Bearer t0k", "content-type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=30))

    # 1. a single agent, steered through the API
    rid = call("POST", "/agents", {"role": "steerable", "task": "wait for a message"})["run_id"]
    time.sleep(2)
    call("POST", f"/runs/{rid}/steer", {"message": "hello agent"})
    r = f.runner.wait(rid, timeout=90, poll=0.5)
    assert r.status == "done" and "hello agent" in r.summary, r

    # 2. an agent that delegates: the API steers it, it steers its subagent
    rid = call("POST", "/agents", {"role": "steerable", "task": "DELEGATE to a subagent"})["run_id"]
    time.sleep(3)
    call("POST", f"/runs/{rid}/steer", {"message": "the plan changed"})
    r = f.runner.wait(rid, timeout=120, poll=0.5)
    assert r.status == "done", r
    assert "relay: api the plan changed" in r.result["child_summary"], r.result
    tree = call("GET", f"/runs/{rid}/tree")
    assert [t["role"] for t in tree] == ["steerable", "echo"]

    # auth is required
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(base + "/status", timeout=5)
    srv.shutdown()


# ---------------------------------------------------------------- subscription rotation

def test_limit_messages_and_reset_times():
    from ff.accounts import is_limit, parse_reset
    now = 1_800_000_000.0
    assert is_limit("Claude AI usage limit reached|1800003600")
    assert parse_reset("Claude AI usage limit reached|1800003600", now) == 1800003600
    assert is_limit("You've hit your usage limit. Upgrade to Pro or try again in 2 hours 13 minutes.")
    assert parse_reset("You've hit your usage limit. try again in 2 hours 13 minutes.", now) == now + 2 * 3600 + 13 * 60
    assert parse_reset("5-hour limit reached ∙ resets 3pm", now) is not None
    assert not is_limit("all 42 tests passed; the rate of change is fine")


def test_rotation_moves_the_session_to_the_next_account(tmp_path, monkeypatch):
    monkeypatch.setenv("FF_AGENTS_HOME", str(tmp_path / "agents"))
    from ff.accounts import Pool
    pool = Pool()
    a, b = pool.add("claude", "acct-a"), pool.add("claude", "acct-b")
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["providers"]["claude"] = {"kind": "claude", "binary": str(ROOT / "tests/fake_claude.py")}
    c["roles"]["worker"] = {"provider": "claude", "timeout_minutes": 2}
    c["accounts"] = {"strategy": "round_robin"}
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    wt, br = f.ws.create("rot")

    # round robin: two runs land on two different accounts
    r1 = f.runner.run("worker", "task one", loop="t", worktree=wt, branch=br)
    r2 = f.runner.run("worker", "task two", loop="t", worktree=wt, branch=br)
    assert {r1.result["account"], r2.result["account"]} == {"acct-a", "acct-b"}

    # the next account in turn (a) is out of credits: the run moves to b, with its session
    reset = int(time.time()) + 7200
    (a / "EXHAUSTED").write_text(str(reset))
    r3 = f.runner.run("worker", "task three", loop="t", worktree=wt, branch=br)
    assert r3.status == "done", r3
    assert r3.result["account"] == "acct-b" and r3.result["resumed"] is True, r3.result
    assert r3.usage.get("account_switches") == 1
    row = pool.get("acct-a")
    assert row["limit_hits"] == 1 and abs(row["cooldown_until"] - reset) < 2
    assert pool.get("acct-b")["active"] == 0 and row["active"] == 0
    assert any("continuing on acct-b with the same session" in e["message"] for e in f.store.events())

    # while a cools down, every run goes to b
    r4 = f.runner.run("worker", "task four", loop="t", worktree=wt, branch=br)
    assert r4.result["account"] == "acct-b"

    # a provider with its own credentials (GLM) never touches the pool
    c["providers"]["glm"] = {"kind": "claude", "binary": str(ROOT / "tests/fake_claude.py"),
                             "env": {"ANTHROPIC_BASE_URL": "http://x", "CLAUDE_CONFIG_DIR": str(tmp_path / "glmcfg")}}
    c["roles"]["glmworker"] = {"provider": "glm", "timeout_minutes": 2}
    p.write_text(yaml.safe_dump(c))
    f2 = Factory.from_path(p)
    r5 = f2.runner.run("glmworker", "task five", loop="t", worktree=wt, branch=br)
    assert r5.result["account"] == "glmcfg"


def test_an_agent_that_cannot_start_costs_no_attempt(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo, implement={"enabled": True, "max_attempts": 3,
                                                "backlog_command": "printf 'item one\\n'"})
    import yaml
    c = yaml.safe_load(p.read_text())
    c["providers"]["broken"] = {"kind": "script", "command": ["sh", "-c", "echo 'cannot be used with root' >&2; exit 1"]}
    c["roles"]["implementer"]["provider"] = "broken"
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    from ff.loops import ImplementLoop
    loop = ImplementLoop(f)
    loop.worker(0)
    runs = f.store.q("SELECT * FROM runs")
    assert len(runs) == 1, "a launch failure must not be retried in a loop"
    assert "cannot be used with root" in runs[0]["summary"]
    assert f.store.paused("implement")
    item = f.store.q("SELECT * FROM backlog")[0]
    assert item["status"] == "open"
    assert any(e["kind"] == "launch-error" for e in f.store.events())


def test_a_clean_exit_without_a_result_file_is_done(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["providers"]["talker"] = {"kind": "script", "command": ["sh", "-c", "cat >/dev/null; echo 'All layers proved; see the branch.'"]}
    c["roles"]["worker"] = {"provider": "talker", "timeout_minutes": 1}
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    wt, br = f.ws.create("talk")
    r = f.runner.run("worker", "do it", loop="t", worktree=wt, branch=br)
    assert r.status == "done" and "All layers proved" in r.summary, r


def test_backlog_ids_survive_a_reworded_description(tmp_path):
    repo = make_project(tmp_path, bug=False)
    out = tmp_path / "backlog.txt"
    out.write_text("law:gas\tprove gas (old wording)\n")
    p = write_config(tmp_path, repo, implement={"enabled": True, "backlog_command": f"cat {out}"})
    f = Factory.from_path(p)
    from ff.loops import ImplementLoop
    loop = ImplementLoop(f)
    assert loop.take() == "law:gas"
    out.write_text("law:gas\tprove gas (new wording)\nlaw:run\tprove run\n")
    f.store.set_flag("backlog:main", None)      # (the listing changed without main moving)
    assert loop.take() == "law:run", "the reworded item is the same item, still running"
    rows = {r["item"]: (r["status"], r["note"]) for r in f.store.q("SELECT * FROM backlog")}
    assert rows["law:gas"] == ("running", "prove gas (new wording)")


def test_config_hot_reload_and_statusline(tmp_path, capsys):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    f = Factory.from_path(p)
    assert f.cfg.checker.file_timeout_seconds == 120
    time.sleep(0.01)
    p.write_text(p.read_text().replace("file_timeout_seconds: 120", "file_timeout_seconds: 60"))
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert f.maybe_reload() and f.cfg.checker.file_timeout_seconds == 60
    assert f.gate.cfg.checker.file_timeout_seconds == 60, "the gate shares the reloaded config"
    from ff.cli import main as ff
    f.store.x("INSERT INTO backlog (item, status, attempts, updated) VALUES ('law:gas_correct', 'running', 0, ?)", (time.time(),))
    assert ff(["--config", str(p), "statusline"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("toy") and "gas_correct ⏳" in line and "\n" not in line, line


def test_references_reach_the_optimizer_prompt(tmp_path):
    repo = make_project(tmp_path, bug=False)
    ref = tmp_path / "fastlib"
    ref.mkdir()
    (ref / "montgomery.go").write_text("// windowed Montgomery\n")
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["benchmark"] = {"command": "echo 1", "references": [{"name": "fastlib", "path": str(ref), "paths": ["*.go"],
                                                         "notes": "Montgomery with 64-bit words"}]}
    c["roles"]["optimizer"] = {"provider": "fake"}
    p.write_text(yaml.safe_dump(c))
    from ff import prompts
    from ff.config import load
    cfg = load(p)
    system, user = prompts.build(cfg, cfg.role("optimizer"), "go", worktree="/w", branch="b", result_file="/r")
    assert "Montgomery with 64-bit words" in user and str(ref / "montgomery.go") in user


def test_a_restarted_daemon_stops_the_runs_it_left_behind(tmp_path):
    repo = make_project(tmp_path, bug=False)
    f = Factory.from_path(write_config(tmp_path, repo))
    p = subprocess.Popen(["sleep", "300"], start_new_session=True)
    f.store.run_start("implementer-old", loop="implement", role="implementer", status="running", pid=p.pid)
    f.store.x("INSERT INTO backlog (item, status, attempts, updated) VALUES ('law:x', 'running', 0, 0)")
    assert f.reconcile() == ([], ["implementer-old"])   # no cycle recorded: nothing to adopt
    assert p.wait(timeout=10) != 0
    assert f.store.run("implementer-old")["status"] == "stopped"
    assert f.store.q("SELECT status FROM backlog")[0]["status"] == "open"


def test_direct_workflow_has_no_generator_rules(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["project"]["workflow"] = "direct"
    c["commands"].pop("regenerate")
    c.pop("generated")
    p.write_text(yaml.safe_dump(c))
    from ff import prompts
    cfg = load(p)
    system, user = prompts.build(cfg, cfg.role("implementer"), "x", worktree="/w", branch="b", result_file="/r")
    assert "generat" not in (system + user).lower(), [l for l in (system + user).splitlines() if "generat" in l.lower()]
    c["generated"] = ["src/*"]
    p.write_text(yaml.safe_dump(c))
    with pytest.raises(ConfigError):
        load(p)


def test_resume_all_clears_every_pause(tmp_path, capsys):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    from ff.cli import main as ff
    ff(["--config", str(p), "pause", "implement"])
    ff(["--config", str(p), "statusline"])
    assert "PAUSED implement" in capsys.readouterr().out
    ff(["--config", str(p), "resume", "all"])
    f = Factory.from_path(p)
    assert not f.store.paused("implement")


def test_prompts_with_braces_do_not_crash(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["roles"]["implementer"]["prompt"] = "Use CALC{...} and RING{a == b}; {a == a : Nat}; {} {0}; budget {file_budget}s"
    p.write_text(yaml.safe_dump(c))
    from ff import prompts
    cfg = load(p)
    system, user = prompts.build(cfg, cfg.role("implementer"), "t", worktree="/w", branch="b", result_file="/r")
    assert "CALC{...}" in user and "{a == a : Nat}" in user and "budget 120s" in user and "{} {0}" in user
    assert '"status": "done"' in system


@needs_bend
def test_a_restarted_daemon_adopts_a_run_that_is_still_going(tmp_path):
    """A detached implementer outlives its daemon; the next daemon picks up its cycle and gates it."""
    repo = make_project(tmp_path)
    p = write_config(tmp_path, repo, implement={"enabled": True, "max_attempts": 2,
                                                "backlog_command": "cat TODO.txt"})
    import yaml
    c = yaml.safe_load(p.read_text())
    # an implementer that takes a few seconds, so the first daemon is gone before it finishes
    c["providers"]["slow"] = {"kind": "script", "command": ["sh", "-c", f"sleep 6; exec {sys.executable} {ROOT / 'tests/fake_agent.py'}"]}
    c["roles"]["implementer"]["provider"] = "slow"
    p.write_text(yaml.safe_dump(c))
    from ff.cli import main as ff
    assert ff(["--config", str(p), "freeze", "--yes"]) == 0
    f1 = Factory.from_path(p)
    from ff.loops import ImplementLoop
    loop1 = ImplementLoop(f1)
    item = loop1.take()
    t = threading.Thread(target=loop1.run_item, args=(item,), daemon=True)
    t.start()
    for _ in range(50):          # wait until the detached agent is running
        rows = f1.store.q("SELECT * FROM runs WHERE status = 'running'")
        if rows:
            break
        time.sleep(0.2)
    assert rows, "the agent never started"
    # "restart": a new daemon on the same state, while the agent still runs
    f2 = Factory.from_path(p)
    loop2 = ImplementLoop(f2)
    adopted, stopped = f2.reconcile({"implement": loop2})
    assert adopted == [rows[0]["id"]] and not stopped
    for _ in range(200):
        if f2.store.q("SELECT status FROM backlog WHERE item = ?", (item,))[0]["status"] == "done":
            break
        time.sleep(0.3)
    assert f2.store.q("SELECT status FROM backlog WHERE item = ?", (item,))[0]["status"] == "done"
    assert any(e["kind"] == "adopted" for e in f2.store.events())
    assert "Nat.add(a,b)" in git(repo, "show", "main:src/add.bend")


def test_steering_a_finished_run_resumes_its_session(tmp_path, monkeypatch):
    monkeypatch.setenv("FF_AGENTS_HOME", str(tmp_path / "agents"))
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["providers"]["claude"] = {"kind": "claude", "binary": str(ROOT / "tests/fake_claude.py"),
                                "env": {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}}
    c["roles"]["worker"] = {"provider": "claude", "timeout_minutes": 2}
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    wt, br = f.ws.create("cont")
    r = f.runner.run("worker", "first task", loop="adhoc", worktree=wt, branch=br)
    assert r.status == "done" and r.result["resumed"] is False
    new = f.runner.steer(r.run_id, "one more thing", sender="human")
    assert len(new) == 1 and new[0] != r.run_id
    r2 = f.runner.wait(new[0], timeout=60, poll=0.3)
    assert r2.status == "done", r2
    assert r2.result["resumed"] is True and "one more thing" in r2.result["first_message"], r2.result


def test_a_session_stays_open_while_the_agent_has_background_work(tmp_path, monkeypatch):
    monkeypatch.setenv("FF_AGENTS_HOME", str(tmp_path / "agents"))
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["providers"]["claude"] = {"kind": "claude", "binary": str(ROOT / "tests/fake_claude.py"),
                                "env": {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}}
    c["roles"]["worker"] = {"provider": "claude", "timeout_minutes": 2}
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    wt, br = f.ws.create("bg")
    r = f.runner.run("worker", "BACKGROUND-WAKE", loop="adhoc", worktree=wt, branch=br)
    assert r.status == "done" and r.summary == "woke up and finished", r


def test_a_brief_with_nothing_to_change_is_done_not_retried(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo, implement={"enabled": True, "max_attempts": 2,
                                                "backlog_command": "printf 'item one\\n'"})
    import yaml
    c = yaml.safe_load(p.read_text())
    c["providers"]["idle"] = {"kind": "script", "command": ["sh", "-c", "cat >/dev/null; echo 'Nothing to change.'"]}
    c["roles"]["implementer"]["provider"] = "idle"
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    from ff.loops import ImplementLoop
    loop = ImplementLoop(f)
    f.store.x("INSERT INTO briefs (loop, text, status, ts) VALUES ('implement', 'FYI: a new test runs', 'open', 0)")
    loop.worker(0)
    rows = {r["item"]: r["status"] for r in f.store.q("SELECT * FROM backlog")}
    assert rows["brief: FYI: a new test runs"] == "done", rows
    loop.worker(0)
    loop.worker(0)
    rows = {r["item"]: r["status"] for r in f.store.q("SELECT * FROM backlog")}
    assert rows["item one"] == "blocked", "an item that keeps yielding no change must stop being retried"


def test_optimize_stops_at_its_target_and_audit_starts_after(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["benchmark"] = {"command": "echo 2.5", "target": 3, "repeats": 1}
    c["roles"]["optimizer"] = {"provider": "fake"}
    c["loops"]["optimize"] = {"enabled": True}
    c["loops"]["audit"] = {"enabled": False, "after": ["implement", "optimize"]}
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    from ff.loops import AuditLoop, OptimizeLoop
    audit = AuditLoop(f)
    assert "optimize" in audit.waiting_for()
    opt = OptimizeLoop(f)
    opt.stop.wait = lambda t=None: False          # do not sleep in the test
    opt.worker(0)
    assert not f.store.q("SELECT * FROM runs"), "no experiment once the target is met"
    assert any(e["kind"] == "target-reached" for e in f.store.events())
    assert audit.waiting_for() == ""
    assert f.status()["optimize_target"] == {"target": 3, "reached": True}
    # main moves: the audit waits until the optimizer has measured the new main
    (repo / "NOTE.txt").write_text("x")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "move main")
    assert "optimize" in audit.waiting_for()
    c["loops"]["audit"]["after"] = ["optimize"]
    c["benchmark"].pop("target")
    p.write_text(yaml.safe_dump(c))
    from ff.config import ConfigError, load
    with pytest.raises(ConfigError):
        load(p)


@needs_bend
def test_a_restart_resumes_the_audit_round_and_remembers_the_answer(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo, audit={"enabled": True, "flavors": {"mutation": "auditor_mutation"},
                                            "fixers": 1, "confirm_each_round": True, "max_rounds": 3})
    f = Factory.from_path(p)
    from ff.cli import main as ff
    assert ff(["--config", str(p), "freeze", "--yes"]) == 0
    # a previous daemon died in round 1's fixing stage with one finding still to fix
    main = git(repo, "rev-parse", "main")
    f.store.x("INSERT INTO rounds (n, started, base_commit, status) VALUES (1, ?, ?, 'fixing')", (time.time(), main))
    f.store.x("INSERT INTO findings (round, flavor, run_id, title, severity, description, reproducer, status, "
              "created, verdict) VALUES (1, 'mutation', 'x', 'mutation: missing law add_comm_spec', 'critical', "
              "'d', 'r', 'fix', ?, '{}')", (time.time(),))
    from ff.loops import AuditLoop
    loop = AuditLoop(f)
    loop.worker(0)
    assert f.store.q("SELECT status FROM rounds WHERE n = 1")[0]["status"] == "done"
    assert f.store.q("SELECT status FROM findings")[0]["status"] == "fixed"
    assert not f.store.q("SELECT 1 FROM rounds WHERE n = 2"), "the round is resumed, not replaced"
    assert not f.store.q("SELECT 1 FROM runs WHERE role LIKE 'auditor_%'"), "no new auditors for a resumed round"
    # the next iteration asks about another round; the human says no
    t = threading.Thread(target=loop.worker, args=(0,), daemon=True)
    t.start()
    for _ in range(100):
        d = f.store.q("SELECT id FROM decisions WHERE answer IS NULL")
        if d:
            break
        time.sleep(0.1)
    f.store.x("UPDATE decisions SET answer = 'no', answered = ? WHERE id = ?", (time.time(), d[0]["id"]))
    t.join(60)
    assert not t.is_alive()
    q = f.store.q("SELECT question FROM decisions")[0]["question"]
    assert "1 critical found this round, 0 of them still open" in q, q
    # a restarted daemon does not ask again and runs no further round
    loop2 = AuditLoop(Factory.from_path(p))
    assert loop2.worker(0) is False
    assert len(f.store.q("SELECT * FROM decisions")) == 1
    assert not f.store.q("SELECT 1 FROM rounds WHERE n = 2")


def test_slots_count_adopted_agents():
    from ff.agents import Slots
    s = Slots(2)
    s.acquire(force=True)
    s.acquire(force=True)
    s.acquire(force=True)          # three adopted agents over a limit of two
    got = []
    t = threading.Thread(target=lambda: (s.acquire(), got.append(1)), daemon=True)
    t.start()
    time.sleep(0.3)
    assert not got, "a new agent waits while adopted ones fill the limit"
    s.release()
    s.release()
    t.join(5)
    assert got


@needs_bend
def test_a_resumed_round_waits_for_its_adopted_fix_cycles(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo, audit={"enabled": True, "flavors": {"mutation": "auditor_mutation"},
                                            "fixers": 2, "confirm_each_round": True, "max_rounds": 3})
    f = Factory.from_path(p)
    from ff.cli import main as ff
    assert ff(["--config", str(p), "freeze", "--yes"]) == 0
    main = git(repo, "rev-parse", "main")
    f.store.x("INSERT INTO rounds (n, started, base_commit, status) VALUES (1, ?, ?, 'fixing')", (time.time(), main))
    from ff.loops import AuditLoop
    loop = AuditLoop(f)
    loop.stop.wait = lambda t=None: time.sleep(0.1) or False
    loop._fixers.acquire(force=True)      # an adopted fix cycle: its agent is done, its gate is not
    t = threading.Thread(target=loop.worker, args=(0,), daemon=True)
    t.start()
    time.sleep(1.5)
    assert f.store.q("SELECT status FROM rounds WHERE n = 1")[0]["status"] == "fixing", \
        "the round is not stamped while a fix cycle is still in flight"
    loop._fixers.release()
    t.join(120)
    assert f.store.q("SELECT status FROM rounds WHERE n = 1")[0]["status"] == "done"


@needs_bend
def test_the_evidence_commit_rebases_when_main_moved(tmp_path, monkeypatch):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo, audit={"enabled": True, "flavors": {"mutation": "auditor_mutation"}})
    f = Factory.from_path(p)
    from ff.cli import main as ff
    assert ff(["--config", str(p), "freeze", "--yes"]) == 0
    from ff.loops import AuditLoop
    loop = AuditLoop(f)
    real = f.gate.submit
    moved = []

    def submit(branch, *a, **k):      # a fix lands on main just before the evidence is gated
        if not moved:
            moved.append(1)
            wt, br = f.ws.create("other-fix")
            (wt / "NOTE.txt").write_text("a fix\n")
            f.ws.commit_pending(wt, "a fix")
            assert real(br).ok
        return real(branch, *a, **k)
    monkeypatch.setattr(f.gate, "submit", submit)
    counts = loop.restamp(1, git(repo, "rev-parse", "main"))
    assert counts["evidence_gate"] == "green", counts
    assert "EVIDENCE.md" in git(repo, "ls-tree", "-r", "--name-only", "main")


@needs_bend
def test_the_gate_rebases_a_stale_candidate_under_its_lock(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    f = Factory.from_path(p)
    from ff.cli import main as ff
    assert ff(["--config", str(p), "freeze", "--yes"]) == 0
    trees = []
    for name in ("one", "two"):      # two fixes made on the same main
        wt, br = f.ws.create(name)
        (wt / f"{name}.txt").write_text(name)
        f.ws.commit_pending(wt, name)
        trees.append((wt, br))
    assert f.gate.submit(trees[0][1]).ok
    wt, br = trees[1]
    assert f.gate.submit(br).stage == "stale"
    v = f.gate.submit(br, rebase=lambda: f.ws.rebase_on_main(wt))
    assert v.ok, (v.stage, v.reason)
    files = git(repo, "ls-tree", "-r", "--name-only", "main")
    assert "one.txt" in files and "two.txt" in files


@needs_bend
def test_a_restart_adopts_a_cycle_whose_agent_is_done_but_not_gated(tmp_path, monkeypatch):
    repo = make_project(tmp_path)
    p = write_config(tmp_path, repo, implement={"enabled": True, "max_attempts": 2,
                                                "backlog_command": "cat TODO.txt"})
    from ff.cli import main as ff
    assert ff(["--config", str(p), "freeze", "--yes"]) == 0
    f1 = Factory.from_path(p)
    from ff.loops import ImplementLoop
    loop1 = ImplementLoop(f1)
    at_gate = threading.Event()
    monkeypatch.setattr(f1.gate, "submit", lambda *a, **k: (at_gate.set(), threading.Event().wait())[1])
    item = loop1.take()
    threading.Thread(target=loop1.run_item, args=(item,), daemon=True).start()
    assert at_gate.wait(120), "the agent never reached the gate"
    run = f1.store.q("SELECT * FROM runs")[0]
    assert run["status"] == "done"
    # the daemon dies here (its thread never returns); the next one must finish the cycle
    f2 = Factory.from_path(p)
    loop2 = ImplementLoop(f2)
    adopted, _ = f2.reconcile({"implement": loop2})
    assert adopted == [run["id"]]
    for _ in range(300):
        if f2.store.q("SELECT status FROM backlog WHERE item = ?", (item,))[0]["status"] == "done":
            break
        time.sleep(0.3)
    assert f2.store.q("SELECT status FROM backlog WHERE item = ?", (item,))[0]["status"] == "done"
    assert "Nat.add(a,b)" in git(repo, "show", "main:src/add.bend")
    assert not f2.store.q("SELECT 1 FROM flags WHERE key LIKE 'cycle-open:%' AND value = '1'")


def test_the_backlog_is_listed_again_only_when_main_moves(tmp_path):
    repo = make_project(tmp_path, bug=False)
    count = tmp_path / "count"
    p = write_config(tmp_path, repo, implement={"enabled": True,
                                                "backlog_command": f"echo x >> {count}; printf 'item one\\n'"})
    f = Factory.from_path(p)
    from ff.loops import ImplementLoop
    loop = ImplementLoop(f)
    for _ in range(3):
        loop.refresh_backlog()
    assert count.read_text().count("x") == 1, "an unchanged main is not listed again"
    (repo / "NOTE.txt").write_text("x")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "move main")
    loop.refresh_backlog()
    assert count.read_text().count("x") == 2


def test_a_finished_agent_is_not_held_open_by_a_job_it_left_behind(tmp_path, monkeypatch):
    monkeypatch.setenv("FF_AGENTS_HOME", str(tmp_path / "agents"))
    import ff.agents
    monkeypatch.setattr(ff.agents, "RESULT_GRACE_SECONDS", 2)
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["providers"]["claude"] = {"kind": "claude", "binary": str(ROOT / "tests/fake_claude.py"),
                                "env": {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}}
    c["roles"]["worker"] = {"provider": "claude", "timeout_minutes": 2}
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    wt, br = f.ws.create("left")
    t0 = time.time()
    r = f.runner.run("worker", "LEFTOVER-JOB", loop="adhoc", worktree=wt, branch=br)
    assert r.status == "done" and time.time() - t0 < 60, (r, time.time() - t0)
    pid = int((f.runner.runs_dir / r.run_id / "leftover.pid").read_text())
    time.sleep(0.5)
    alive = os.path.exists(f"/proc/{pid}") and open(f"/proc/{pid}/stat").read().split(")")[-1].split()[0] != "Z"
    assert not alive, "the leftover job is ended"
    assert any(e["kind"] == "leftover-jobs" for e in f.store.events())


def test_the_audit_stops_by_itself_when_a_round_finds_nothing_that_matters(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo, audit={"enabled": True, "flavors": {"mutation": "auditor_mutation"},
                                            "converge_at": "high"})
    f = Factory.from_path(p)
    from ff.loops import AuditLoop
    loop = AuditLoop(f)
    now = time.time()
    f.store.x("INSERT INTO rounds (n, started, base_commit, status) VALUES (1, ?, 'x', 'done')", (now,))
    for sev, status in (("critical", "documented"), ("medium", "fixed"), ("low", "fixed")):
        f.store.x("INSERT INTO findings (round, flavor, run_id, title, severity, description, reproducer, status, "
                  "created) VALUES (1, 'mutation', 'r', 't', ?, 'd', 'r', ?, ?)", (sev, status, now))
    did = f.store.ask("Audit round 1 is merged: ... Run another round?", ["yes", "no"])   # left by an older daemon
    assert loop.converged(1)[0], "an unreachable critical and fixed medium/low findings: converged"
    assert loop.worker(0) is False
    assert any(e["kind"] == "audit-done" for e in f.store.events())
    assert f.store.answer(did).startswith("no"), "the factory answers the pending question itself"
    assert not f.store.q("SELECT 1 FROM rounds WHERE n = 2")
    f.store.x("INSERT INTO findings (round, flavor, run_id, title, severity, description, reproducer, status, "
              "created) VALUES (1, 'crash', 'r', 't', 'high', 'd', 'r', 'fixed', ?)", (now,))
    assert not loop.converged(1)[0], "a reachable high finding (even fixed) means another round"


def test_phases_follow_each_other_without_configuration(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    from ff.config import load
    c = yaml.safe_load(p.read_text())
    c["benchmark"] = {"command": "echo 1", "target": 3}
    c["roles"]["optimizer"] = {"provider": "fake"}
    c["loops"] = {"implement": {"enabled": True, "backlog_command": "true"}, "optimize": {"enabled": True},
                  "audit": {"enabled": True, "flavors": {"mutation": "auditor_mutation"}}}
    p.write_text(yaml.safe_dump(c))
    assert load(p).audit.after == ["implement", "optimize"]
    c["benchmark"].pop("target")          # an optimize loop without a target never finishes: not waited for
    p.write_text(yaml.safe_dump(c))
    assert load(p).audit.after == ["implement"]
    c["loops"]["audit"]["after"] = []     # explicit: audit at once
    p.write_text(yaml.safe_dump(c))
    assert load(p).audit.after == []


def test_the_web_page_shows_progress_and_honours_its_token(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    f = Factory.from_path(p)
    f.store.x("INSERT INTO backlog (item, status, attempts, updated, note) VALUES ('law:add', 'done', 1, 0, 'n')")
    f.store.event("gate", "gate-green", "gate #1 GREEN for ff/x")
    from ff import webui
    st = webui.state(f.cfg, f.store)
    assert st["backlog"][0]["item"] == "law:add" and st["events"][0]["label"] == "merged"
    json.dumps(st)
    srv = webui.serve(f.cfg, f.store, "127.0.0.1:0", "s3cret")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import urllib.error
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/state.json")
        raise AssertionError("served without the token")
    except urllib.error.HTTPError as e:
        assert e.code == 401
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/?token=s3cret")
    assert b"Formal Factory" in page.read()
    cookie = page.headers["set-cookie"].split(";")[0]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/state.json", headers={"cookie": cookie})
    assert json.loads(urllib.request.urlopen(req).read())["project"] == f.cfg.project.name
    srv.shutdown()


@needs_bend
def test_agents_draft_the_spec_a_human_approves_it_then_implementation_starts(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["providers"]["drafter"] = {"kind": "script", "command": ["sh", "-c",
        "cat >/dev/null; mkdir -p spec && echo 'the add rule' > spec/notes.md && git add -A && "
        "git -c user.name=a -c user.email=a@x commit -qm 'spec: notes' && echo drafted"]}
    c["roles"]["specifier"] = {"provider": "drafter", "timeout_minutes": 1}
    c["loops"]["specify"] = {"enabled": True, "workers": 1,
                             "backlog_command": "test -f spec/notes.md || printf 'spec:notes\\twrite the notes\\n'"}
    c["loops"]["implement"] = {"enabled": True, "backlog_command": "printf 'law:x\\n'"}
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    from ff.loops import ImplementLoop, SpecifyLoop
    impl = ImplementLoop(f)
    impl.stop.wait = lambda t=None: False
    impl.worker(0)
    assert not f.store.q("SELECT 1 FROM runs WHERE role = 'implementer'"), "nothing is built before approval"
    spec = SpecifyLoop(f)
    spec.stop.wait = lambda t=None: False
    spec.worker(0)                                   # drafts spec/notes.md through the gate
    assert "spec/notes.md" in git(repo, "ls-tree", "-r", "--name-only", "main")
    assert "frozen.lock.json" not in git(repo, "ls-tree", "-r", "--name-only", "main"), \
        "nothing is locked while the specification is a draft"
    assert f.store.q("SELECT status FROM backlog WHERE item = 'spec:notes'")[0]["status"] == "done"
    spec.worker(0)                                   # backlog empty: asks the human
    d = f.store.q("SELECT id, question FROM decisions WHERE answer IS NULL")
    assert d and "specification is drafted" in d[0]["question"]
    f.store.x("UPDATE decisions SET answer = 'approve' WHERE id = ?", (d[0]["id"],))
    spec.worker(0)
    assert f.store.flag("spec:approved")
    assert "frozen.lock.json" in git(repo, "ls-tree", "-r", "--name-only", "main")
    assert spec.worker(0) is False, "the specify loop is finished once approved"
    impl.take()
    assert f.store.q("SELECT loop FROM backlog WHERE item = 'law:x'")[0]["loop"] == "implement"


def test_the_factory_creates_and_wires_its_repository(tmp_path, monkeypatch):
    calls = []
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    real_run = subprocess.run

    def fake_run(argv, *a, **k):                     # gh: the repository does not exist, then is created
        if argv[:2] == ["gh", "repo"]:
            calls.append(argv[2])
            exists = "create" in calls[:-1]
            return subprocess.CompletedProcess(argv, 1 if argv[2] == "view" and not exists else 0, "", "")
        return real_run(argv, *a, **k)
    monkeypatch.setattr(subprocess, "run", fake_run)
    repo = tmp_path / "new-target"
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["project"].update(github="someone/new-target", visibility="public")
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    # the GitHub URL is replaced by the local bare repository for the push
    real_git_remote = f"https://github.com/someone/new-target.git"
    subprocess.run(["git", "config", "--global", f"url.{bare}.insteadOf", real_git_remote], check=False,
                   env=dict(os.environ, HOME=str(tmp_path)))
    monkeypatch.setenv("HOME", str(tmp_path))
    did = f.ensure_repo()
    assert calls == ["view", "create"]
    assert (repo / ".git").exists()
    assert git(repo, "config", "--get", "remote.origin.url") == real_git_remote
    assert any("pushed" in d for d in did)
    assert git(bare, "rev-parse", "main")
    assert f.ensure_repo() == ["main pushed to someone/new-target"], "idempotent: nothing created twice"
    assert calls.count("create") == 1


def test_ffi_is_banned_unless_the_user_allows_it(tmp_path):
    from ff.ffi import violations
    (tmp_path / "Evm").mkdir()
    (tmp_path / "Evm/Fast.lean").write_text('@[extern "lean_fast_interp"]\nopaque run : Nat → Nat\n')
    (tmp_path / "Spec").mkdir()
    (tmp_path / "Spec/Trusted.lean").write_text('@[extern "lean_keccak"] opaque keccak : Nat → Nat\n')
    (tmp_path / "lakefile.lean").write_text('extern_lib ffi pkg := pure default\n')
    (tmp_path / "ok.lean").write_text('-- mentions extern in a comment only: def externFoo := 1\ndef x := 1\n')
    v = violations(tmp_path, "lean", [])
    assert any(s.startswith("Evm/Fast.lean:1") for s in v) and any(s.startswith("lakefile.lean:1") for s in v)
    assert any(s.startswith("Spec/Trusted.lean") for s in v) and not any(s.startswith("ok.lean") for s in v)
    v = violations(tmp_path, "lean", ["Spec/Trusted.lean", "lakefile.lean"])
    assert [s.split(":")[0] for s in v] == ["Evm/Fast.lean"], "allowed files are allowed, nothing else"
    (tmp_path / "e.bend").write_text('def X.exchange(a: u8) -> IO(u8):\n  import "./x.c"\n')
    assert violations(tmp_path, "bend", [])[0].startswith("e.bend:2")


@needs_bend
def test_the_gate_refuses_ffi(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    f = Factory.from_path(p)
    from ff.cli import main as ff
    assert ff(["--config", str(p), "freeze", "--yes"]) == 0
    wt, br = f.ws.create("sneaky")
    (wt / "ffi.bend").write_text('def Fast.add(a: u32) -> IO(u32):\n  import "./fast.c"\n')
    f.ws.commit_pending(wt, "fast path in C")
    v = f.gate.submit(br)
    assert not v.ok and v.stage == "ffi" and "ffi.bend:2" in v.log


@needs_bend
def test_the_gate_refuses_changes_to_the_harness(tmp_path):
    repo = make_project(tmp_path, bug=False)
    (repo / "tools").mkdir(exist_ok=True)
    (repo / "tools/bench.py").write_text("print(1)\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "harness")
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["spec"]["immutable"] = ["tools/**"]
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    from ff.cli import main as ff
    assert ff(["--config", str(p), "freeze", "--yes"]) == 0
    wt, br = f.ws.create("faster-bench")
    (wt / "tools/bench.py").write_text("print(0.1)\n")
    f.ws.commit_pending(wt, "a faster benchmark")
    v = f.gate.submit(br)
    assert not v.ok and v.stage == "immutable" and "tools/bench.py" in v.log


def test_optimize_waits_for_the_implementation(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["benchmark"] = {"command": "echo 9", "target": 3, "repeats": 1}
    c["roles"]["optimizer"] = {"provider": "fake"}
    c["loops"] = {"implement": {"enabled": True, "backlog_command": "printf 'law:x\\n'"}, "optimize": {"enabled": True}}
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    from ff.loops import ImplementLoop, OptimizeLoop
    early = OptimizeLoop(f)                          # before implement has listed anything
    early.stop.wait = lambda t=None: False
    early.worker(0)
    assert not f.store.q("SELECT 1 FROM runs") and f.store.flag("baseline:" + git(repo, "rev-parse", "main")) is None
    ImplementLoop(f).refresh_backlog()
    opt = OptimizeLoop(f)
    opt.stop.wait = lambda t=None: False
    opt.worker(0)
    assert not f.store.q("SELECT 1 FROM runs"), "no benchmark, no optimizer while items are open"
    assert any(e["kind"] == "waiting" and "implement" in e["message"] for e in f.store.events())
    assert f.store.flag("baseline:" + git(repo, "rev-parse", "main")) is None


@needs_bend
def test_auto_approval_freezes_the_spec_without_asking(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["roles"]["specifier"] = {"provider": "fake", "timeout_minutes": 1}
    c["loops"]["specify"] = {"enabled": True, "approval": "auto", "backlog_command": "true"}
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    from ff.loops import SpecifyLoop
    spec = SpecifyLoop(f)
    spec.stop.wait = lambda t=None: False
    spec.worker(0)
    spec.ask_for_approval()                       # a second worker arriving late: no second freeze
    assert f.store.flag("spec:approved") and not f.store.q("SELECT 1 FROM decisions")
    assert len([e for e in f.store.events() if e["kind"] == "frozen"]) == 1
    assert "frozen.lock.json" in git(repo, "ls-tree", "-r", "--name-only", "main")


def test_lean_statements_carry_their_namespace():
    from ff.frozen import lean_statements
    src = "namespace A.B\ntheorem t : 1 = 1 := rfl\nsection\ndef d := 1\nend\nend A.B\ntheorem _root_.u : True := trivial\ntheorem v : 2 = 2 := rfl\n"
    keys = [s.key for s in lean_statements("X.lean", src)]
    assert keys == ["X.lean::A.B.t", "X.lean::A.B.d", "X.lean::u", "X.lean::v"], keys


def test_a_kept_experiment_s_measurement_is_main_s_baseline(tmp_path):
    repo = make_project(tmp_path, bug=False)
    p = write_config(tmp_path, repo)
    import yaml
    c = yaml.safe_load(p.read_text())
    c["benchmark"] = {"command": "echo 1", "repeats": 1}
    c["roles"]["optimizer"] = {"provider": "fake"}
    c["loops"]["optimize"] = {"enabled": True}
    p.write_text(yaml.safe_dump(c))
    f = Factory.from_path(p)
    from ff.loops import OptimizeLoop
    opt = OptimizeLoop(f)
    measured = git(repo, "rev-parse", "main")
    opt.reuse_measurement(measured, 2.5)
    assert f.store.flag(f"baseline:{measured}") == "2.5"
    (repo / "NOTE.txt").write_text("x")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "other code")
    opt.reuse_measurement(measured, 2.0)
    assert f.store.flag("baseline:" + git(repo, "rev-parse", "main")) is None, "different code is measured again"
