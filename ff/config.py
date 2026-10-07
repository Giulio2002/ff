"""factory.yaml: one file configures the target, the checker, every provider and every agent role.

Strings may reference environment variables as ${NAME} or ${NAME:-default}; they are
expanded at load time, so API keys never have to live in the file.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

LANGUAGES = ("bend", "lean")
PROVIDER_KINDS = ("claude", "codex", "script")
FLAVORS = ("mutation", "crash", "regression")


class ConfigError(ValueError):
    pass


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _take(d: dict, key: str, default: Any = None, *, required: bool = False, where: str = "") -> Any:
    if key in d:
        return d[key]
    if required:
        raise ConfigError(f"{where}: missing required key '{key}'")
    return default


def _no_extra(d: dict, allowed: set[str], where: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise ConfigError(f"{where}: unknown key(s) {sorted(extra)} (allowed: {sorted(allowed)})")


@dataclass
class Project:
    name: str
    repo: Path                 # the target git repository (the factory never edits its main checkout)
    main_branch: str = "main"
    remote: str | None = None  # push main here after every green gate when set
    language: str = "bend"
    state_dir: Path = Path()   # runs, worktrees, transcripts, the sqlite store
    # How agents change the program: "direct" (edit code and proofs) or "generators" (edit generator
    # scripts; commands.regenerate writes the `generated` files, which nobody edits by hand).
    workflow: str = "direct"
    # A GitHub repository ("owner/name") the factory manages: created with `gh` if it does not exist,
    # wired as `remote` (default origin), and main_branch (the aggregated branch every green gate
    # lands on) pushed to it after each merge.
    github: str | None = None
    visibility: str = "private"   # public | private, when the factory creates the repository


@dataclass
class Provider:
    """How to launch one agent CLI. kind=claude: Claude Code (`claude -p`); kind=codex: Codex
    (`codex exec`); kind=script: any command (tests, or a CLI the factory does not know)."""
    name: str
    kind: str
    binary: str
    model: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)
    permission_mode: str = "bypassPermissions"   # claude: agents run unattended in their worktree
    sandbox: str = "danger-full-access"          # codex: the worktree is the sandbox boundary
    command: list[str] = field(default_factory=list)  # script: argv; {prompt_file} {workdir} {result_file}
    # Subscription rotation (claude/codex): "auto" = use the ~/.formal-agents pool of this kind when it has
    # accounts (never for a provider that brings its own credentials in env, like GLM); "none" = the CLI's
    # default login; or a list of account names to rotate among.
    accounts: Any = "auto"


@dataclass
class Role:
    name: str
    provider: str
    model: str | None = None          # overrides the provider's model
    prompt: str = ""                  # template text (a path is read relative to the config file)
    timeout_minutes: float = 120
    subagents: list[str] = field(default_factory=list)
    subagent_only: bool = False       # never scheduled by a loop, only called by other agents
    max_subagent_depth: int = 2
    env: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)


@dataclass
class Checker:
    binary: str = ""
    args: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)       # globs the checker must accept, all of them
    file_timeout_seconds: float = 120                    # the per-file budget ("each file under 2 min")
    enforce_file_budget: bool = True
    jobs: int = 8
    ok_marker: str = ""                                  # bend: "ALL PROOFS CHECK"
    kernel_recheck: bool = False                         # bend: also run --verdict in the gate
    forbid: list[str] = field(default_factory=list)      # regexes rejected anywhere in checked files
    allowed_axioms: list[str] = field(default_factory=list)  # lean: #print axioms allowlist


@dataclass
class Spec:
    frozen: list[str] = field(default_factory=list)      # globs whose statements are frozen
    lock_file: str = "frozen.lock.json"                  # in the target repo, written only by the gate
    changes_file: str = "frozen_changes.yaml"            # every allowed change to a frozen statement
    require_implication_proof: bool = True               # a change must name a checked "new -> old" proof


@dataclass
class Reference:
    """The fastest baseline the benchmark compares against, for agents to learn from: a local
    directory (`path`) or a git repository (`repo`, `ref`) that the factory clones read-only into
    <state>/references/<name>. `paths` are the key files (globs, relative to it); `notes` say what
    makes it fast."""
    name: str
    path: str = ""
    repo: str = ""
    ref: str = ""
    paths: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class Benchmark:
    command: str = ""
    metric: str = r"([0-9.]+)"     # regex; group 1 is the number
    direction: str = "lower"       # lower or higher is better
    min_improvement_pct: float = 1.0
    target: float | None = None    # the goal: once main meets it the optimize loop stops (and audit may start)
    unit: str = ""                 # how the number reads, for people (the web UI): "×", " ns", ...
    repeats: int = 3
    timeout_minutes: float = 30
    references: list[Reference] = field(default_factory=list)


@dataclass
class Ffi:
    """FFI is banned unless the user allows it here: the files (globs) where it may appear."""
    allow: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class Gate:
    host: str = "local"            # or user@host: the gate runs `ff gate-run` there over ssh
    remote_config: str = ""        # path of factory.yaml on that host
    cold: bool = True              # fresh clone, no caches
    regenerate: bool = True
    keep_failed_trees: int = 5


@dataclass
class ImplementLoop:
    enabled: bool = True
    role: str = "implementer"
    workers: int = 2
    max_attempts: int = 3
    backlog_command: str = ""      # prints the open items (one per line, or a JSON list)


@dataclass
class SpecifyLoop:
    """Agents draft the frozen specification itself, item by item from its own backlog (a missing
    opcode, an unwritten rule). When that backlog is empty the human is asked to review and approve
    it; approving freezes it, and only then does the implement loop start."""
    enabled: bool = False
    role: str = "specifier"
    workers: int = 2
    max_attempts: int = 3
    backlog_command: str = ""


@dataclass
class OptimizeLoop:
    enabled: bool = False
    role: str = "optimizer"
    workers: int = 1
    history: int = 20              # past experiments shown to the next optimizer


@dataclass
class AuditLoop:
    enabled: bool = True
    flavors: dict[str, str] = field(default_factory=dict)   # flavor -> role
    judge: str = "judge"
    fixer: str = "fixer"
    fixers: int = 2
    max_rounds: int = 10
    # start once these loops are finished: implement (backlog empty) and/or optimize (benchmark.target
    # met on main). Unset: the phases follow each other by themselves, so the audit waits for every
    # enabled loop that can finish (optimize only finishes when benchmark.target is set). [] = at once.
    after: list[str] | None = None
    # the audit stops by itself once a round finds nothing reachable at `converge_at` or worse
    # (critical | high | medium | low): the auditors have run out of findings that matter
    converge_at: str = "high"
    stop_when_critical_at_most: int = 0   # deprecated (converge_at decides); kept so old configs load
    confirm_each_round: bool = False      # also ask the human before each further round
    known_limitations_file: str = "KNOWN_LIMITATIONS.md"
    evidence_file: str = "EVIDENCE.md"


@dataclass
class Accounts:
    strategy: str = "round_robin"      # round_robin: least recently used first; fill_first: use one until it runs out
    default_cooldown_minutes: float = 60  # when a limit message names no reset time


@dataclass
class Limits:
    max_parallel_agents: int = 6
    nice: int = 19
    detached_agents: bool = True   # loop agents in processes of their own: a daemon restart adopts them


@dataclass
class Config:
    path: Path
    project: Project
    commands: dict[str, str]
    generated: list[str]
    checker: Checker
    spec: Spec
    benchmark: Benchmark
    gate: Gate
    providers: dict[str, Provider]
    roles: dict[str, Role]
    implement: ImplementLoop
    optimize: OptimizeLoop
    audit: AuditLoop
    limits: Limits
    accounts: Accounts = field(default_factory=Accounts)
    specify: SpecifyLoop = field(default_factory=SpecifyLoop)
    ffi: Ffi = field(default_factory=Ffi)
    coordinator: str = "coordinator"

    def role(self, name: str) -> Role:
        if name not in self.roles:
            raise ConfigError(f"no role named '{name}'")
        return self.roles[name]

    def provider_for(self, role: Role) -> Provider:
        return self.providers[role.provider]

    def model_for(self, role: Role) -> str | None:
        return role.model or self.providers[role.provider].model


DEFAULT_CHECKERS = {
    "bend": dict(binary="bend", args=["--check-only"], ok_marker="ALL PROOFS CHECK",
                 forbid=[r"@unsafe"]),
    "lean": dict(binary="lake", args=["env", "lean"],
                 forbid=[r"\bsorry\b", r"\badmit\b", r"^\s*axiom\s", r"implemented_by",
                         r"\bunsafe\b", r"debug\.skipKernelTC", r"\bnative_decide\b"],
                 allowed_axioms=["propext", "Classical.choice", "Quot.sound"]),
}


def _read_prompt(text_or_path: str, base: Path) -> str:
    if not text_or_path:
        return ""
    p = (base / text_or_path)
    if "\n" not in text_or_path and len(text_or_path) < 300 and p.is_file():
        return p.read_text()
    return text_or_path


def load(path: str | os.PathLike) -> Config:
    path = Path(path).resolve()
    raw = _expand(yaml.safe_load(path.read_text()) or {})
    base = path.parent
    top = {"project", "commands", "generated", "checker", "spec", "benchmark", "gate", "providers",
           "roles", "loops", "limits", "coordinator", "accounts", "ffi"}
    _no_extra(raw, top, "factory.yaml")

    p = _take(raw, "project", required=True, where="factory.yaml")
    _no_extra(p, {"name", "repo", "main_branch", "remote", "language", "state_dir", "workflow", "github",
                  "visibility"}, "project")
    language = _take(p, "language", "bend")
    if language not in LANGUAGES:
        raise ConfigError(f"project.language must be one of {LANGUAGES}, got '{language}'")
    name = _take(p, "name", required=True, where="project")
    repo = (base / _take(p, "repo", required=True, where="project")).resolve()
    state = Path(_take(p, "state_dir", f"~/.formal-factory/{name}")).expanduser()
    if not state.is_absolute():
        state = (base / state).resolve()
    workflow = _take(p, "workflow", "direct")
    if workflow not in ("direct", "generators"):
        raise ConfigError("project.workflow must be 'direct' or 'generators'")
    github = _take(p, "github")
    if github is not None and not re.fullmatch(r"[\w.-]+/[\w.-]+", str(github)):
        raise ConfigError(f"project.github must be 'owner/name', got '{github}'")
    visibility = _take(p, "visibility", "private")
    if visibility not in ("public", "private"):
        raise ConfigError("project.visibility must be 'public' or 'private'")
    project = Project(name=name, repo=repo, main_branch=_take(p, "main_branch", "main"),
                      remote=_take(p, "remote", "origin" if github else None), language=language,
                      state_dir=state, workflow=workflow, github=github, visibility=visibility)

    ck = dict(DEFAULT_CHECKERS[language])
    user_ck = _take(raw, "checker", {}) or {}
    # `checker:` may be flat or keyed by language (so one file can carry both)
    if language in user_ck and isinstance(user_ck[language], dict):
        user_ck = user_ck[language]
    user_ck = {k: v for k, v in user_ck.items() if k not in LANGUAGES}
    _no_extra(user_ck, set(Checker.__dataclass_fields__), "checker")
    ck.update(user_ck)
    checker = Checker(**ck)
    if not checker.files:
        raise ConfigError("checker.files: list the globs of every file the checker must accept")

    spec_raw = _take(raw, "spec", {}) or {}
    _no_extra(spec_raw, set(Spec.__dataclass_fields__), "spec")
    spec = Spec(**spec_raw)

    bench_raw = dict(_take(raw, "benchmark", {}) or {})
    _no_extra(bench_raw, set(Benchmark.__dataclass_fields__), "benchmark")
    refs = []
    for i, r in enumerate(bench_raw.pop("references", None) or []):
        _no_extra(r, set(Reference.__dataclass_fields__), f"benchmark.references[{i}]")
        if "name" not in r or not (r.get("path") or r.get("repo")):
            raise ConfigError(f"benchmark.references[{i}] needs a name and a path or a repo")
        ref = Reference(**r)
        if ref.path and not os.path.isabs(os.path.expanduser(ref.path)):
            ref.path = str((base / ref.path).resolve())
        refs.append(ref)
    benchmark = Benchmark(**bench_raw, references=refs)
    if benchmark.direction not in ("lower", "higher"):
        raise ConfigError("benchmark.direction must be 'lower' or 'higher'")

    gate_raw = _take(raw, "gate", {}) or {}
    _no_extra(gate_raw, set(Gate.__dataclass_fields__), "gate")
    gate = Gate(**gate_raw)
    ffi_raw = _take(raw, "ffi", {}) or {}
    _no_extra(ffi_raw, set(Ffi.__dataclass_fields__), "ffi")
    ffi = Ffi(**ffi_raw)

    providers = {}
    for pname, pr in (_take(raw, "providers", required=True, where="factory.yaml") or {}).items():
        where = f"providers.{pname}"
        _no_extra(pr, set(Provider.__dataclass_fields__) - {"name"}, where)
        kind = _take(pr, "kind", required=True, where=where)
        if kind not in PROVIDER_KINDS:
            raise ConfigError(f"{where}.kind must be one of {PROVIDER_KINDS}")
        binary = _take(pr, "binary", {"claude": "claude", "codex": "codex", "script": ""}[kind])
        prov = Provider(name=pname, kind=kind, binary=binary,
                        **{k: v for k, v in pr.items() if k not in ("kind", "binary")})
        prov.env = {k: str(v) for k, v in prov.env.items()}
        if kind == "script" and not prov.command:
            raise ConfigError(f"{where}: a script provider needs `command`")
        providers[pname] = prov

    roles = {}
    for rname, rr in (_take(raw, "roles", required=True, where="factory.yaml") or {}).items():
        where = f"roles.{rname}"
        _no_extra(rr, set(Role.__dataclass_fields__) - {"name"}, where)
        role = Role(name=rname, **rr)
        if role.provider not in providers:
            raise ConfigError(f"{where}.provider '{role.provider}' is not defined under providers")
        role.prompt = _read_prompt(role.prompt, base)
        role.env = {k: str(v) for k, v in role.env.items()}
        roles[rname] = role
    for role in roles.values():
        for s in role.subagents:
            if s not in roles:
                raise ConfigError(f"roles.{role.name}.subagents: no role named '{s}'")

    loops = _take(raw, "loops", {}) or {}
    _no_extra(loops, {"specify", "implement", "optimize", "audit"}, "loops")

    def mk(cls, key):
        d = loops.get(key, {}) or {}
        _no_extra(d, set(cls.__dataclass_fields__), f"loops.{key}")
        return cls(**d)

    implement, optimize, audit = mk(ImplementLoop, "implement"), mk(OptimizeLoop, "optimize"), mk(AuditLoop, "audit")
    specify = mk(SpecifyLoop, "specify")
    for flavor in audit.flavors:
        if flavor not in FLAVORS:
            raise ConfigError(f"loops.audit.flavors: unknown flavor '{flavor}' (known: {FLAVORS})")
    used = []
    if specify.enabled:
        used.append(specify.role)
        if not specify.backlog_command:
            raise ConfigError("loops.specify is enabled but has no backlog_command")
    if implement.enabled:
        used.append(implement.role)
    if optimize.enabled:
        used.append(optimize.role)
        if not benchmark.command:
            raise ConfigError("loops.optimize is enabled but benchmark.command is empty")
    if audit.enabled:
        used += list(audit.flavors.values()) + [audit.judge, audit.fixer]
    if audit.converge_at not in ("critical", "high", "medium", "low"):
        raise ConfigError(f"loops.audit.converge_at: '{audit.converge_at}' (critical, high, medium or low)")
    if audit.after is None:
        audit.after = ([n for n, l in (("implement", implement), ("optimize", optimize)) if l.enabled
                        and (n != "optimize" or benchmark.target is not None)])
    for dep in audit.after:
        if dep not in ("implement", "optimize"):
            raise ConfigError(f"loops.audit.after: unknown loop '{dep}' (implement, optimize)")
    if "optimize" in audit.after and not optimize.enabled:
        raise ConfigError("loops.audit.after names optimize, which is not enabled")
    if "optimize" in audit.after and benchmark.target is None:
        raise ConfigError("loops.audit.after names optimize, which only finishes when benchmark.target is set")
    coordinator = _take(raw, "coordinator", "coordinator")
    for r in used + [coordinator]:
        if r not in roles:
            raise ConfigError(f"a loop or the coordinator uses role '{r}', which is not under roles")

    lim_raw = _take(raw, "limits", {}) or {}
    _no_extra(lim_raw, set(Limits.__dataclass_fields__), "limits")

    acc_raw = _take(raw, "accounts", {}) or {}
    _no_extra(acc_raw, set(Accounts.__dataclass_fields__), "accounts")
    accounts = Accounts(**acc_raw)
    if accounts.strategy not in ("round_robin", "fill_first"):
        raise ConfigError("accounts.strategy must be round_robin or fill_first")
    for prov in providers.values():
        if not (prov.accounts in ("auto", "none", None, False) or isinstance(prov.accounts, list)):
            raise ConfigError(f"providers.{prov.name}.accounts must be auto, none or a list of account names")

    commands = _take(raw, "commands", {}) or {}
    _no_extra(commands, {"regenerate", "unit_tests", "vectors", "runtime_tests"}, "commands")
    generated = _take(raw, "generated", []) or []
    if workflow == "generators" and not (commands.get("regenerate") and generated):
        raise ConfigError("project.workflow is 'generators': set commands.regenerate and the `generated` globs")
    if workflow == "direct" and (commands.get("regenerate") or generated):
        raise ConfigError("commands.regenerate / generated are set but project.workflow is 'direct'; "
                          "set workflow: generators to use them")

    return Config(path=path, project=project, commands=commands,
                  generated=generated, checker=checker, spec=spec,
                  benchmark=benchmark, gate=gate, providers=providers, roles=roles, ffi=ffi,
                  implement=implement, optimize=optimize, audit=audit, limits=Limits(**lim_raw), specify=specify,
                  accounts=accounts, coordinator=coordinator)
