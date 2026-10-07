"""FFI is banned unless the user allows it.

Foreign code (C behind a Lean `@[extern]`, a Bend foreign import, native objects linked from the
lakefile) is code the proofs say nothing about: a program could move its real work there and still
"check". So the gate scans the whole candidate tree and refuses any FFI outside the files the user
listed in factory.yaml:

    ffi:
      allow: ["Spec/Trusted.lean", "lakefile.lean"]   # say why, in a comment
      reason: precompiles via FFI, as the user decided

factory.yaml lives outside the repository and only the user edits it, so agents cannot widen it.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

SKIP_DIRS = {".git", ".lake", "build", "node_modules", "__pycache__", ".ff_axioms_probe"}

# (file glob, pattern, what it is)
MARKERS = {
    "lean": [
        ("*.lean", r"@\[\s*(?:[\w.]+\s*,\s*)*(extern|implemented_by)\b", "a Lean @[extern]/@[implemented_by] (C or other code behind a Lean name)"),
        ("lakefile.lean", r"\b(extern_lib|moreLinkArgs|moreLeancArgs|precompileModules)\b", "native code linked from the lakefile"),
        ("lakefile.toml", r"^\s*(moreLinkArgs|moreLeancArgs|precompileModules)\s*=|\[\[\s*extern_lib", "native code linked from the lakefile"),
    ],
    "bend": [
        ("*.bend", r"\bimport\s+\"[^\"]+\"", "a Bend foreign import (C code behind a Bend function)"),
    ],
}


def _walk(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and not (SKIP_DIRS & set(p.relative_to(root).parts)):
            yield p


def violations(root: Path, language: str, allow: list[str]) -> list[str]:
    """Every FFI use in the tree outside the allowed globs, as 'path:line: what'."""
    out = []
    for p in _walk(root):
        rel = str(p.relative_to(root))
        rules = [(rx, what) for glob, rx, what in MARKERS.get(language, [])
                 if fnmatch.fnmatch(p.name, glob)]
        if not rules or any(fnmatch.fnmatch(rel, a) for a in allow):
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for rx, what in rules:
            for m in re.finditer(rx, text, re.M):
                line = text.count("\n", 0, m.start()) + 1
                out.append(f"{rel}:{line}: {what}")
    return out
