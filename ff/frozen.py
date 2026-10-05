"""Frozen statements.

When a proof will not go through, the easy way out is to make the theorem say less. So every
statement in the frozen files is hashed into a lock file that only the gate writes, from main.
A candidate passes only if every locked statement is still there with the same hash, or the
change is recorded in the changes file together with the name of a proof that the new statement
implies the old one (checked by the checker like everything else). New statements are allowed:
adding laws only makes the contract stronger; the gate locks them when it promotes the tree.

What a "statement" is depends on the language:
- Bend: every top-level `law` block (the statement, its proof is a separate `def`), and in frozen
  spec files also every top-level `def`/`type` (the spec definitions themselves).
- Lean: every `theorem`/`lemma` up to its `:=` (proofs may change, statements may not), and every
  other declaration (`def`, `structure`, `inductive`, `abbrev`, ...) in full.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Statement:
    key: str      # "<relative path>::<name>"
    text: str     # normalised text that is hashed

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def _normalise(text: str, comment: str) -> str:
    out = []
    for line in text.splitlines():
        line = re.sub(rf"\s*{re.escape(comment)}.*$", "", line).rstrip()
        if line:
            out.append(re.sub(r"\s+", " ", line.strip()))
    return "\n".join(out)


def _blocks(lines: list[str], starts: re.Pattern) -> list[tuple[str, str, int]]:
    """Top-level blocks: a line matching `starts` at column 0 plus every following indented or
    blank line. Returns (keyword, name, first line index)."""
    found = []
    for i, line in enumerate(lines):
        m = starts.match(line)
        if m:
            found.append((m.group(1), m.group(2), i))
    return found


BEND_START = re.compile(r"^(law|def|type)\s+([A-Za-z_][\w.]*)")
LEAN_START = re.compile(
    r"^(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|noncomputable|partial|nonrec)\s+)*"
    r"(theorem|lemma|def|abbrev|structure|inductive|class|instance|axiom|opaque)\s+([^\s:({\[]+)")


def _block_text(lines: list[str], start: int) -> str:
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line and not line[0].isspace():
            break
        out.append(line)
    return "\n".join(out)


def bend_statements(rel: str, source: str) -> list[Statement]:
    lines = source.splitlines()
    out = []
    for kw, name, i in _blocks(lines, BEND_START):
        out.append(Statement(f"{rel}::{name}", _normalise(f"{kw}\n" + _block_text(lines, i), "#")))
    return out


def lean_statements(rel: str, source: str) -> list[Statement]:
    # drop block comments first; line comments are dropped by _normalise
    source = re.sub(r"/-.*?-/", "", source, flags=re.S)
    lines = source.splitlines()
    out = []
    for kw, name, i in _blocks(lines, LEAN_START):
        text = _block_text(lines, i)
        if kw in ("theorem", "lemma"):
            # the statement is everything up to the first top-level `:=`
            text = re.split(r":=", text, maxsplit=1)[0]
        out.append(Statement(f"{rel}::{name}", _normalise(f"{kw}\n{text}", "--")))
    return out


EXTRACTORS = {"bend": bend_statements, "lean": lean_statements}


def match_globs(root: Path, globs: list[str]) -> list[Path]:
    files = set()
    for g in globs:
        for p in root.glob(g):
            if p.is_file() and ".git" not in p.parts:
                files.add(p)
    return sorted(files)


def collect(root: Path, globs: list[str], language: str) -> dict[str, Statement]:
    out: dict[str, Statement] = {}
    for f in match_globs(root, globs):
        rel = f.relative_to(root).as_posix()
        for s in EXTRACTORS[language](rel, f.read_text(errors="replace")):
            out[s.key] = s
    return out


def lock_of(statements: dict[str, Statement]) -> dict[str, str]:
    return {k: s.digest for k, s in sorted(statements.items())}


def load_lock(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    return json.loads(text).get("statements", {})


def dump_lock(lock: dict[str, str]) -> str:
    return json.dumps({"comment": "written by the formal-factory gate; do not edit",
                       "statements": dict(sorted(lock.items()))}, indent=1) + "\n"


@dataclass
class FrozenReport:
    ok: bool
    problems: list[str]
    added: list[str]
    changed_ok: list[str]
    new_lock: dict[str, str]


def verify(root: Path, globs: list[str], language: str, lock: dict[str, str], changes_text: str | None,
           require_implication: bool, proof_names: set[str]) -> FrozenReport:
    """Compare the statements of the tree at `root` with `lock` (taken from main)."""
    current = collect(root, globs, language)
    changes = {}
    if changes_text:
        for e in (yaml.safe_load(changes_text) or {}).get("changes", []) or []:
            changes[(e.get("key"), e.get("old"), e.get("new"))] = e
    problems, changed_ok = [], []
    for key, old in lock.items():
        cur = current.get(key)
        new = cur.digest if cur else "deleted"
        if cur and new == old:
            continue
        entry = changes.get((key, old, new))
        if entry is None:
            what = "deleted" if cur is None else "changed"
            problems.append(f"frozen statement {key} was {what} without an entry in the changes file "
                            f"(key: {key}, old: {old}, new: {new})")
            continue
        if not str(entry.get("reason", "")).strip():
            problems.append(f"changes entry for {key} has no reason")
            continue
        if require_implication and new != "deleted":
            proof = str(entry.get("implication", "")).strip()
            if not proof:
                problems.append(f"changes entry for {key} must name `implication`: a proof that the new "
                                f"statement implies the old one")
                continue
            if proof not in proof_names:
                problems.append(f"changes entry for {key}: implication proof '{proof}' not found in the checked files")
                continue
        changed_ok.append(key)
    added = sorted(set(current) - set(lock))
    new_lock = dict(lock)
    for k in added + changed_ok:
        new_lock[k] = current[k].digest
    for k in changed_ok:
        if k not in current:
            new_lock.pop(k, None)
    return FrozenReport(ok=not problems, problems=problems, added=added, changed_ok=changed_ok, new_lock=new_lock)


def declared_names(root: Path, globs: list[str], language: str) -> set[str]:
    """Names of every declaration in the checked files (bare and qualified), for implication proofs."""
    names = set()
    for s in collect(root, globs, language).values():
        name = s.key.split("::", 1)[1]
        names.add(name)
        names.add(name.split(".")[-1])
    return names


def in_globs(rel: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, g) or Path(rel).match(g) for g in globs)
