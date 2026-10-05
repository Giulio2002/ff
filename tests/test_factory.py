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
        "project": {"name": "toy", "repo": str(repo), "language": "bend", "state_dir": str(tmp / "state")},
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
    loop.worker(0)
    rows = {r["title"]: r["status"] for r in f.store.q("SELECT * FROM findings")}
    assert rows["mutation: missing law add_comm_spec"] == "fixed"
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
