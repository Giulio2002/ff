"""Globs as Path.glob reads them: `**/` is zero or more directories, `*` and `?` stay inside one."""
from __future__ import annotations

import functools
import re
from pathlib import Path


@functools.lru_cache(maxsize=256)
def _regex(glob: str) -> re.Pattern:
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:[^/]+/)*", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile(out + r"\Z")


def in_globs(rel: str, globs: list[str]) -> bool:
    """Whether the repository-relative path `rel` matches one of `globs`."""
    return any(_regex(g).match(rel) for g in globs)


def match_globs(root: Path, globs: list[str]) -> list[Path]:
    """The files under `root` matching `globs` (never inside .git)."""
    files = set()
    for g in globs:
        for p in root.glob(g):
            if p.is_file() and ".git" not in p.parts:
                files.add(p)
    return sorted(files)
