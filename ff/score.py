"""The benchmark's score from a Markdown file (benchmark.score_file).

The file says, for people and agents, what the number means, and holds exactly one ```python block
that defines `score(result) -> float`: `result` is the last JSON object the benchmark command prints.
It is read from main, never from the tree being measured, so a candidate cannot change its own score."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

_BLOCK = re.compile(r"^```python[ \t]*\n(.*?)^```", re.S | re.M)


class ScoreError(Exception):
    pass


def score_code(text: str) -> str:
    blocks = _BLOCK.findall(text)
    if len(blocks) != 1:
        raise ScoreError(f"the score file needs exactly one ```python block defining score(result); it has {len(blocks)}")
    return blocks[0]


def score_fn(text: str):
    ns: dict = {}
    exec(compile(score_code(text), "<score_file>", "exec"), ns)
    fn = ns.get("score")
    if not callable(fn):
        raise ScoreError("the score file's python block does not define score(result)")
    return fn


def read_from(repo: Path, ref: str, path: str) -> str:
    p = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, text=True)
    if p.returncode != 0:
        raise ScoreError(f"cannot read {path} at {ref}: {p.stderr.strip()[:200]}")
    return p.stdout


def last_json(out: str) -> dict:
    """The last line of `out` that is a JSON object."""
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                v = json.loads(line)
            except ValueError:
                continue
            if isinstance(v, dict):
                return v
    raise ScoreError("the benchmark printed no JSON object")


def score(text: str, out: str) -> float:
    return float(score_fn(text)(last_json(out)))
