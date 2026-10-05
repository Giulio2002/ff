"""Reference implementations: the fastest baseline the benchmark measures against.

The optimizer is told to study how the reference gets its speed (algorithm, representation, tricks)
and port the ideas, proved, through the generators. Git references are cloned (shallow, read-only by
convention) into <state>/references/<name>; local ones are used where they are.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Config, Reference


def location(cfg: Config, ref: Reference) -> Path:
    if ref.path:
        return Path(ref.path).expanduser()
    return cfg.project.state_dir / "references" / ref.name


def fetch(cfg: Config, ref: Reference, update: bool = False) -> Path:
    loc = location(cfg, ref)
    if ref.repo:
        if not (loc / ".git").exists():
            loc.parent.mkdir(parents=True, exist_ok=True)
            argv = ["git", "clone", "-q", "--depth", "1"] + (["--branch", ref.ref] if ref.ref else []) + [ref.repo, str(loc)]
            subprocess.run(argv, check=True, capture_output=True, text=True)
        elif update:
            subprocess.run(["git", "-C", str(loc), "pull", "-q", "--ff-only"], capture_output=True, text=True)
    return loc


def files(cfg: Config, ref: Reference, limit: int = 40) -> list[Path]:
    loc = location(cfg, ref)
    out: list[Path] = []
    for g in ref.paths or []:
        out += sorted(p for p in loc.glob(g) if p.is_file())
    return out[:limit]


def render(cfg: Config) -> str:
    """The prompt block that describes every reference (with local paths agents can read)."""
    refs = cfg.benchmark.references
    if not refs:
        return "(no reference implementation is configured for this benchmark)"
    blocks = []
    for ref in refs:
        try:
            loc = fetch(cfg, ref)
        except Exception as e:  # an unreachable reference must not stop an agent
            blocks.append(f"- {ref.name}: unavailable ({e})")
            continue
        fs = files(cfg, ref)
        lines = [f"- {ref.name}, read only, at {loc}" + (f" ({ref.repo} {ref.ref})" if ref.repo else "")]
        if ref.notes:
            lines.append("  " + " ".join(ref.notes.split()))
        for f in fs:
            lines.append(f"    {f}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
