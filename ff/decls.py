"""Top-level declarations of Bend and Lean files: what they are called and what they say.

The Lean axiom audit asks Lean about every theorem by its full name, and the web UI shows the
specification's and the contract's declarations one by one.
- Bend: every top-level `law`, `def` and `type`.
- Lean: every `theorem`/`lemma` (its statement: the text up to `:=`) and every other declaration
  (`def`, `structure`, `inductive`, `abbrev`, ...), named with the namespaces it is in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

BEND_START = re.compile(r"^(law|def|type)\s+([A-Za-z_][\w.]*)")
LEAN_START = re.compile(
    r"^(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|noncomputable|partial|nonrec)\s+)*"
    r"(theorem|lemma|def|abbrev|structure|inductive|class|instance|axiom|opaque)\s+([^\s:({\[]+)")


@dataclass(frozen=True)
class Decl:
    kind: str        # law, def, type, theorem, lemma, structure, ...
    name: str        # full name (Lean: with its namespaces)
    statement: str   # the declaration without its proof (theorems), or in full, whitespace-normalised
    block: str       # the declaration's full text, as written

    @property
    def short(self) -> str:
        return self.name.split(".")[-1]


def _block(lines: list[str], start: int) -> str:
    """A line at column 0 plus every following indented or blank line."""
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line and not line[0].isspace():
            break
        out.append(line)
    return "\n".join(out)


def _normalise(text: str, comment: str) -> str:
    out = []
    for line in text.splitlines():
        line = re.sub(rf"\s*{re.escape(comment)}.*$", "", line).rstrip()
        if line:
            out.append(re.sub(r"\s+", " ", line.strip()))
    return "\n".join(out)


def bend(source: str) -> list[Decl]:
    lines = source.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = BEND_START.match(line)
        if m:
            text = _block(lines, i)
            out.append(Decl(m.group(1), m.group(2), _normalise(text, "#"), text))
    return out


def lean(source: str) -> list[Decl]:
    source = re.sub(r"/-.*?-/", "", source, flags=re.S)
    lines = source.splitlines()
    stack: list[str] = []
    out = []
    for i, line in enumerate(lines):
        m = re.match(r"^(namespace|section)\b\s*([\w.]*)", line)
        if m:
            stack.append(m.group(2) if m.group(1) == "namespace" else "")
            continue
        if re.match(r"^end\b", line) and stack:
            stack.pop()
            continue
        m = LEAN_START.match(line)
        if not m:
            continue
        kw, name = m.group(1), m.group(2)
        text = _block(lines, i)
        stmt = re.split(r":=", text, maxsplit=1)[0] if kw in ("theorem", "lemma") else text
        full = name.removeprefix("_root_.") if name.startswith("_root_.") else ".".join([s for s in stack if s] + [name])
        out.append(Decl(kw, full, _normalise(stmt, "--"), text))
    return out


def of(language: str, source: str) -> list[Decl]:
    return (bend if language == "bend" else lean)(source)
