"""Prompt assembly: the factory preamble every agent gets, then its role prompt, then the task."""
from __future__ import annotations

from importlib import resources

from .config import Config, Role

PREAMBLE = """\
You are one agent inside a formal-programs factory for the project "{project}" ({language}).
A formal program is code that comes with machine-checked proofs that it does what a frozen
specification says, for every input. Machines produce the code and proofs; a checker decides.

Your working copy: {worktree} (a git worktree on branch {branch}). Work only there.

Rules of the factory (the gate enforces every one of them; breaking one wastes your run):
- Never hand-edit generated files ({generated}). Change the generators and regenerate with
  `{regenerate}`. A bug fixed in a generator is fixed for every output at once.
- Frozen statements (in {frozen}) must not change or be weakened. If one truly has to change,
  the new statement must be at least as strong: add an entry to `{changes_file}` with the key,
  old hash, new hash, a reason, and the name of a proof that the new statement implies the old.
  Never edit `{lock_file}`.
- Every file in {check_files} must pass the checker within {file_budget}s.
  Forbidden anywhere in them: {forbid}.
- Commit your work on your branch with clear messages. Do not push; do not touch main.
- Before you finish, run `ff check` in your worktree: it regenerates, checks the frozen
  statements and runs the checker the way the gate will (add `--files a b` to check only some).

Tools the factory gives you (shell commands):
- `ff check [--files ...]`            the gate's checks, locally
- `ff note "<text>"`                  a progress note for the coordinator (use it when you are stuck)
- `ff inbox`                          messages someone sent you while you run; check it between steps
{subagent_tools}
When you are done, write a JSON object to the file $FF_RESULT_FILE ({result_file}):
  {{"status": "done" | "blocked", "summary": "<what you did, in a few sentences>"{result_extra}}}
"""

SUBAGENT_TOOLS = """\
- `ff subagent start <role> "<task>"` start a subagent (returns its run id; it works in your tree
                                      unless you pass --own-worktree)
- `ff subagent run <role> "<task>"`   start one and wait for its result
- `ff subagent steer <id> "<text>"`   send a running subagent a message (redirect, narrow, stop early)
- `ff subagent wait <id>` / `ff subagent status [<id>]` / `ff subagent stop <id>`
  Subagent roles you may use: {subagents}
"""

STEERING = """
A human or another agent may steer you while you work. Messages arrive as new user turns or
in `ff inbox`; they take priority over this brief. When a message concerns work you delegated,
forward it to the right subagent with `ff subagent steer`.
"""


def default_prompt(role_name: str) -> str:
    for name in (role_name, role_name.split("_")[0]):
        try:
            return resources.files("ff.roles").joinpath(f"{name}.md").read_text()
        except (FileNotFoundError, OSError):
            continue
    return resources.files("ff.roles").joinpath("generic.md").read_text()


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def build(cfg: Config, role: Role, task: str, *, worktree: str, branch: str, result_file: str,
          result_extra: str = "", extra: dict | None = None) -> tuple[str, str]:
    """Returns (system prompt, first user message)."""
    vals = _Safe(
        project=cfg.project.name, language=cfg.project.language, worktree=worktree, branch=branch,
        role=role.name,
        generated=", ".join(cfg.generated) or "(none)", regenerate=cfg.commands.get("regenerate", "(none)"),
        frozen=", ".join(cfg.spec.frozen) or "(none)", changes_file=cfg.spec.changes_file,
        lock_file=cfg.spec.lock_file, check_files=", ".join(cfg.checker.files),
        file_budget=int(cfg.checker.file_timeout_seconds),
        forbid=", ".join(f"/{p}/" for p in cfg.checker.forbid) or "(nothing)",
        result_file=result_file, result_extra=result_extra,
        subagents=", ".join(role.subagents),
        subagent_tools="",
        unit_tests=cfg.commands.get("unit_tests", "(none)"),
        vectors=cfg.commands.get("vectors", "(none)"),
        runtime_tests=cfg.commands.get("runtime_tests", "(none)"),
        benchmark=cfg.benchmark.command, known_limitations=cfg.audit.known_limitations_file,
        **(extra or {}))
    if role.subagents:
        vals["subagent_tools"] = SUBAGENT_TOOLS.format_map(vals)
    system = PREAMBLE.format_map(vals) + STEERING
    role_text = (role.prompt or default_prompt(role.name)).format_map(vals)
    return system, role_text.rstrip() + "\n\n# Your task\n\n" + task.strip() + "\n"
