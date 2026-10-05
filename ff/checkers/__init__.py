"""Proof checkers. A checker answers one question for a tree: does every file check, each within
its time budget, with no forbidden construct?"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Checker as CheckerConfig
from ..frozen import collect, match_globs


@dataclass
class FileResult:
    path: str
    ok: bool
    seconds: float
    output: str = ""
    timed_out: bool = False


@dataclass
class CheckReport:
    ok: bool
    files: list[FileResult] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bad = [f for f in self.files if not f.ok]
        slow = max(self.files, key=lambda f: f.seconds, default=None)
        s = f"{len(self.files) - len(bad)}/{len(self.files)} files check"
        if slow:
            s += f", slowest {slow.path} {slow.seconds:.1f}s"
        if self.problems:
            s += f", {len(self.problems)} problem(s)"
        return s

    def details(self, limit: int = 20) -> str:
        out = list(self.problems[:limit])
        for f in [f for f in self.files if not f.ok][:limit]:
            why = "timed out" if f.timed_out else "failed"
            out.append(f"{f.path} {why} after {f.seconds:.1f}s:\n{f.output[-3000:]}")
        return "\n\n".join(out)


def run_cmd(argv: list[str] | str, cwd: Path, timeout: float | None, nice: int = 0,
            env: dict[str, str] | None = None) -> tuple[int, str, float, bool]:
    """Run a command in its own process group; kill the whole group on timeout."""
    t0 = time.monotonic()
    shell = isinstance(argv, str)
    full_env = dict(os.environ, **(env or {}))
    p = subprocess.Popen(argv, cwd=cwd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", env=full_env, start_new_session=True,
                         preexec_fn=(lambda: os.nice(nice)) if nice else None)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out, time.monotonic() - t0, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = p.communicate()
        return -9, out or "", time.monotonic() - t0, True


class Base:
    language = ""

    def __init__(self, cfg: CheckerConfig, nice: int = 0):
        self.cfg = cfg
        self.nice = nice

    def files(self, root: Path) -> list[Path]:
        return match_globs(root, self.cfg.files)

    def scan_forbidden(self, root: Path, files: list[Path]) -> list[str]:
        pats = [re.compile(p, re.M) for p in self.cfg.forbid]
        problems = []
        for f in files:
            text = self.strip_comments(f.read_text(errors="replace"))
            for p in pats:
                m = p.search(text)
                if m:
                    line = text[:m.start()].count("\n") + 1
                    problems.append(f"forbidden construct /{p.pattern}/ in {f.relative_to(root)}:{line}")
        return problems

    def strip_comments(self, text: str) -> str:
        return text

    def prepare(self, root: Path) -> CheckReport | None:
        """Build step before per-file checks (lean: lake build). None = nothing to do."""
        return None

    def check_file(self, root: Path, f: Path, timeout: float) -> FileResult:
        raise NotImplementedError

    def extra(self, root: Path, frozen_globs: list[str]) -> list[str]:
        """Language-specific whole-tree checks (kernel recheck, axiom audit). Returns problems."""
        return []

    def check(self, root: Path, only: list[Path] | None = None, frozen_globs: list[str] | None = None,
              full: bool = True) -> CheckReport:
        files = only if only is not None else self.files(root)
        problems = self.scan_forbidden(root, files)
        pre = self.prepare(root)
        if pre is not None and not pre.ok:
            pre.problems = problems + pre.problems
            return pre
        budget = self.cfg.file_timeout_seconds
        # past the budget the file fails anyway when the budget is enforced; otherwise allow 10x
        timeout = budget * (1.0 if self.cfg.enforce_file_budget else 10.0) + 5
        with ThreadPoolExecutor(max_workers=max(1, self.cfg.jobs)) as ex:
            results = list(ex.map(lambda f: self.check_file(root, f, timeout), files))
        for r in results:
            r.path = str(Path(r.path).relative_to(root)) if Path(r.path).is_absolute() else r.path
            if r.ok and self.cfg.enforce_file_budget and r.seconds > budget:
                r.ok = False
                r.output += f"\nover the per-file budget: {r.seconds:.1f}s > {budget}s"
        if full and frozen_globs is not None:
            problems += self.extra(root, frozen_globs)
        ok = not problems and all(r.ok for r in results)
        return CheckReport(ok=ok, files=results, problems=problems)


class Bend(Base):
    language = "bend"

    def strip_comments(self, text: str) -> str:
        return re.sub(r"#.*", "", text)

    def check_file(self, root: Path, f: Path, timeout: float) -> FileResult:
        code, out, secs, to = run_cmd([self.cfg.binary, str(f.relative_to(root)), *self.cfg.args], root,
                                      timeout, self.nice, env={"BEND_NO_TELEMETRY": "1"})
        ok = code == 0 and not to and (not self.cfg.ok_marker or self.cfg.ok_marker in out)
        if "SOME PROOFS FAIL" in out:
            ok = False
        return FileResult(str(f), ok, secs, out, to)

    def extra(self, root: Path, frozen_globs: list[str]) -> list[str]:
        if not self.cfg.kernel_recheck:
            return []
        problems = []
        for f in self.files(root):
            code, out, _, to = run_cmd([self.cfg.binary, str(f.relative_to(root)), "--verdict"], root,
                                       self.cfg.file_timeout_seconds * 20, self.nice, env={"BEND_NO_TELEMETRY": "1"})
            if code != 0 or to or "SOME PROOFS FAIL" in out:
                problems.append(f"kernel recheck (--verdict) failed for {f.relative_to(root)}:\n{out[-2000:]}")
        return problems


class Lean(Base):
    language = "lean"

    def strip_comments(self, text: str) -> str:
        text = re.sub(r"/-.*?-/", "", text, flags=re.S)
        return re.sub(r"--.*", "", text)

    def prepare(self, root: Path) -> CheckReport | None:
        n = max(1, len(self.files(root)))
        code, out, secs, to = run_cmd(["lake", "build"], root, self.cfg.file_timeout_seconds * n + 600, self.nice)
        if code != 0 or to:
            return CheckReport(ok=False, files=[FileResult("lake build", False, secs, out, to)])
        return None

    def check_file(self, root: Path, f: Path, timeout: float) -> FileResult:
        code, out, secs, to = run_cmd([self.cfg.binary, *self.cfg.args, str(f.relative_to(root))], root,
                                      timeout, self.nice)
        bad = code != 0 or to or re.search(r"declaration uses 'sorry'|^.*error:", out, re.M)
        return FileResult(str(f), not bad, secs, out, to)

    def extra(self, root: Path, frozen_globs: list[str]) -> list[str]:
        """#print axioms for every frozen theorem: only the allowlist may appear (no sorryAx)."""
        theorems = [s.key for s in collect(root, frozen_globs, "lean").values()
                    if s.text.startswith(("theorem", "lemma"))]
        if not theorems:
            return []
        modules = sorted({k.split("::")[0][:-len(".lean")].replace("/", ".") for k in theorems})
        names = [k.split("::")[1] for k in theorems]
        probe = root / ".ff_axioms_probe.lean"
        probe.write_text("".join(f"import {m}\n" for m in modules) + "\n" +
                         "".join(f"#print axioms {n}\n" for n in names))
        try:
            code, out, _, to = run_cmd([self.cfg.binary, *self.cfg.args, probe.name], root,
                                       self.cfg.file_timeout_seconds * 5, self.nice)
        finally:
            probe.unlink(missing_ok=True)
        if code != 0 or to:
            return [f"axiom audit did not run:\n{out[-2000:]}"]
        problems = []
        allowed = set(self.cfg.allowed_axioms)
        for m in re.finditer(r"'([^']+)' depends on axioms: \[([^\]]*)\]", out):
            used = {a.strip() for a in m.group(2).split(",") if a.strip()}
            extra = used - allowed
            if extra:
                problems.append(f"theorem {m.group(1)} depends on axioms outside the allowlist: {sorted(extra)}")
        return problems


def make(language: str, cfg: CheckerConfig, nice: int = 0) -> Base:
    return {"bend": Bend, "lean": Lean}[language](cfg, nice)
