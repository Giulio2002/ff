# formal-factory: instructions for the agent setting it up

You are an AI coding agent (Claude Code, Codex, ...). A user handed you this file because they want a
program built with **formal-factory** (`ff`): AI agents write the program **and machine-checked
proofs** that it meets a frozen specification, a proof checker keeps score, and a deterministic gate
decides what reaches the repository's aggregated branch. Targets are written in **Bend 2** or
**Lean 4**. A run goes through its phases by itself:

```
specify (optional) ──(human approves)──▶ implement ──▶ optimize ──(target met)──▶ audit ──(converged)──▶ done
```

Your job: understand what the user wants, set up the target repository and `factory.yaml`, start the
factory, and keep the user informed. This file is both your procedure (§0) and the manual (§1 on).

## 0. Procedure for the agent

### 0.1 Interview the user first

Do not guess these. Ask (in one batch where your interface allows it), and offer a recommendation
for each:

1. **What to build.** The program, its public interface, and what "correct" means. Ask for any spec,
   standard, reference implementation or test suite they already have.
2. **The specification.** Is there an existing formal spec to freeze, or should the agents draft one
   (`loops.specify`) for the user to review and approve? The spec is the one thing a human must sign
   off: nothing is built against it until they approve.
3. **Language.** Bend 2 or Lean 4.
4. **Trusted parts and FFI.** FFI is banned by default and the gate enforces it: no Lean
   `@[extern]`/`@[implemented_by]`, no native code linked from the lakefile, no Bend foreign imports.
   Ask whether anything may be foreign, trusted code instead of proved code (hash functions,
   cryptography, precompiles). Only what the user states goes in `ffi.allow`; each entry is a hole in
   the proof.
5. **Evidence beyond proofs.** Official test suites to pass, a reference implementation to test
   against (differential tests, fuzzing).
6. **Performance.** Is there a speed target? Which benchmark, against which reference, measured how
   (every case, total, geometric mean)? That becomes `benchmark.command` and `benchmark.target`.
7. **Repository.** GitHub `owner/name`, public or private, and the aggregated branch (`main` by
   default). ff creates and pushes it.
8. **Models.** Which provider per role: Claude (Claude Code), GPT (Codex), GLM (an
   Anthropic-compatible endpoint). Default: Claude for everything.
9. **Subscriptions for rotation.** How many Claude and Codex subscriptions they have. Long runs
   exhaust one account; ff rotates through several (§9). **The logins are interactive, so the user
   runs them** (or you run them while they complete the browser step). Ask them to run, once per
   subscription:
   ```
   ff add_login claude            # each Claude subscription (claude-1, claude-2, ...)
   ff add_login codex -- --device-auth   # each ChatGPT/Codex subscription
   ```
   With no account in the pool, agents use the machine's default `claude`/`codex` login. Once the
   pool has accounts, the default login is no longer used, so add the current one too.
10. **The web UI.** Do they want the read-only progress page (`--webui`)? On localhost only, or exposed
    to the internet? Behind a token (`--webui-token`)?
11. **Budget and attention.** Is there a spend limit they want reported against? How often should you
    report? Which decisions do they want to be asked about?

Confirm the plan back in a few lines before building anything.

### 0.2 Set it up

1. Install ff and the toolchain (§1). If you run as root, give Claude providers `IS_SANDBOX: "1"`.
2. Set up the target repository (§2): the spec (or a spec backlog for `loops.specify`), the contract
   (the laws that define "done"), a **fast** backlog command, the tests, the benchmark. Set
   `project.github` so that ff creates and pushes it (`ff repo`).
3. Write `factory.yaml` (§3) and run `ff validate`.
4. With an existing spec: show it to the user, and run `ff freeze --yes` only after they approve it.
   With `loops.specify`: the factory asks them when the draft is complete.
5. Start it in tmux: `ff run --api 127.0.0.1:8787 [--webui 0.0.0.0:8080]` (§5).

### 0.3 While it runs

- Check `ff status` and `ff events --since 15m` regularly, or stream `ff watch`.
- Relay open decisions (`ff status` shows them) to the user. Answer only the ones they delegated.
- When something is stuck or looping, read the run (`ff show`, `ff tail`). Steer it (`ff steer`,
  `ff brief`) rather than restarting it.
- Report progress in plain terms: phase, what merged, speed against the target, spend.

## 1. Install

```sh
git clone https://github.com/Giulio2002/ff.git && cd ff
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                 # Python 3.11+
```

You also need:

- the checker: a Bend 2 binary (`bend`) or a Lean 4 toolchain (`lake`);
- at least one agent CLI: Claude Code (`claude`) or Codex (`codex`), logged in;
- git.

Running as root? Claude Code refuses `bypassPermissions` as root unless it is told it is in a
sandbox. Add `IS_SANDBOX: "1"` to the provider's `env` (see §3).

## 2. Prepare the target repository

The factory works on a git repository (the *target*). Before starting, it should contain what only
a human should write:

| What | Example (Bend) | Why |
|---|---|---|
| The specification | `spec/*.bend` | what the program must do; frozen |
| The contract | `proofs/contract.bend`: the laws that must be proved | what "done" means; frozen |
| A backlog command | `tools/backlog.py`: prints one open item per line (`<id>\t<description>`) | the implement loop's work list (keep it fast: it runs on every change of main) |
| Tests | unit tests, official test vectors, runtime tests of the compiled library | run by the gate on every candidate |
| A benchmark (optional) | `tools/bench.py`: prints one number | what the optimize loop chases |

Give the factory **its own clone** of the target (`project.repo`). Agents never touch its checkout;
only the gate moves `main` there.

## 3. Write `factory.yaml`

```sh
ff init --language bend          # or lean: writes an annotated factory.yaml to edit
ff validate                      # loads it and reports mistakes
```

One file holds everything. The sections, in the order you will need them:

```yaml
project:
  name: modexp
  repo: ./bend-modexp            # the factory's clone (relative to this file)
  language: bend                 # bend | lean
  workflow: direct               # direct: agents edit code and proofs | generators: they edit generators
  main_branch: main              # the aggregated branch: every green gate lands here
  github: owner/my-project       # ff creates it (gh) if missing, wires the remote, pushes main_branch
  visibility: public             # public | private (when ff creates it)
  # state_dir: ~/.formal-factory/modexp   (default)

commands:                        # run by the gate, in a cold clone, in this order
  unit_tests: python3 tools/check_contract.py
  vectors: cd tools && python3 run_vectors.py
  runtime_tests: tools/geth_tests.sh
  # regenerate: python3 tools/generate_all.py   (workflow: generators only)

checker:
  bend:
    binary: /path/to/bend
    args: ["--check-only"]
    files: ["proofs/**/*.bend"]
    file_timeout_seconds: 60     # every file must check within this budget
    kernel_recheck: true         # also `bend --verdict` (the formal kernel) in the gate

spec:
  frozen: ["spec/*.bend", "proofs/contract.bend", "proofs/laws.bend"]

benchmark:                       # only for the optimize loop
  command: cd tools && python3 bench.py
  metric: '"ratio": ([0-9.]+)'   # regex; group 1 is the number
  direction: lower
  min_improvement_pct: 3         # a change must beat main by this much to be kept
  target: 3.0                    # the goal: optimizing stops once main meets it
  references:                    # the fastest baseline, for the optimizer to learn from
    - name: geth-modexp
      path: ~/go/pkg/mod/github.com/ethereum/go-ethereum@v1.17.7
      paths: ["core/vm/contracts.go"]
      notes: windowed Montgomery; CRT for even moduli

providers:                       # how agents are launched (see §8)
  claude:
    kind: claude
    model: claude-opus-5-5
    env: {IS_SANDBOX: "1"}       # only when running as root

roles:                           # every agent role: provider, timeout, subagents, prompt
  coordinator: {provider: claude}
  implementer: {provider: claude, timeout_minutes: 180, subagents: [explorer]}
  optimizer:   {provider: claude, timeout_minutes: 120}
  auditor_mutation:   {provider: claude}
  auditor_crash:      {provider: claude}
  auditor_regression: {provider: claude}
  judge:  {provider: claude}
  fixer:  {provider: claude}
  explorer: {provider: claude, subagent_only: true}

loops:
  # specify: {enabled: true, workers: 2, backlog_command: python3 tools/spec_backlog.py}
  #   agents draft the spec; implementation waits until the user approves (and so freezes) it
  implement: {enabled: true, workers: 2, max_attempts: 4, backlog_command: python3 tools/backlog.py}
  optimize:  {enabled: true, workers: 1}
  audit:
    enabled: true
    flavors: {mutation: auditor_mutation, crash: auditor_crash, regression: auditor_regression}
    fixers: 2
    converge_at: high            # stop once a round finds nothing reachable at high or worse
    max_rounds: 10

limits:
  max_parallel_agents: 4
  nice: 19                       # checker, tests and benchmarks run at low priority
```

Notes:
- `${VAR}` and `${VAR:-default}` are read from the environment, so keys stay out of the file.
- A role without `prompt:` uses the built-in one in `ff/roles/<role>.md`. `prompt:` can be inline text
  or a file path.
- `examples/factory.bend.yaml` and `examples/factory.lean.yaml` show every option with comments.
- The daemon re-reads `factory.yaml` when it changes. Commands, budgets, models, prompts and timeouts
  apply to the next run and gate. Worker counts need a restart.

## 4. Freeze the specification

With an existing specification, after the user has approved it:

```sh
ff freeze --yes
```

With `loops.specify`, skip this: agents draft the spec from the spec backlog, and when it is complete
the factory asks the user (a decision in `ff status`, the chat and the web UI) to approve it.
Approving freezes it and starts implementation. Answering anything else keeps it open: brief the
specify loop with what to change (`ff brief specify "..."`), and it asks again once that has landed.

This locks every frozen statement (`spec.frozen`) on `main`, in `frozen.lock.json`. After this, only
the gate writes the lock. If you, the human, change the spec later, re-freeze with
`ff freeze --yes --force`.

## 5. Run it

Run the daemon in tmux (or nohup), so it outlives your shell:

```sh
export FF_CONFIG=$PWD/factory.yaml        # or pass --config to every command
export FF_API_TOKEN=$(openssl rand -hex 16)
tmux new -d -s ff 'ff run --api 127.0.0.1:8787 2>&1 | tee -a factory.log'
```

`ff run --loops implement,optimize,audit` limits which loops run. Without `--loops`, every loop
enabled in the YAML runs.

Then follow it:

```sh
ff chat           # the coordinator: an open chat (Claude Code) with the factory's stage at the bottom
ff status         # one screen: main, spend, running agents, last gate, backlog, audit round, decisions
ff watch          # stream the important events as they happen
```

**Stopping and restarting is safe.** `Ctrl-C` (or `kill <pid>`) stops the daemon at once. Agents run
in processes of their own and keep going. The next `ff run` adopts them and continues where they
were: committing, rebasing, gating. That includes an audit round in the middle of its fixes. Restart
whenever you update ff or change worker counts.

## 6. What happens while it runs

**Specify** (optional). Like implement, but the items are parts of the specification, and the
phase ends with the user's approval (§4).

**Implement.** Each worker takes an open item from the backlog command, gives it to an implementer
agent in its own git worktree and branch, and sends the result to the gate. A red gate goes back to
the same agent with the gate's log, up to `max_attempts`. Then the item is `blocked`
(`ff backlog reopen <item>` puts it back). The backlog is listed again whenever `main` moves.

**Optimize.** Autoresearch with a veto. Each optimizer makes one change. The change is kept only if
the benchmark beats `main` by `min_improvement_pct` (median of `repeats` runs) **and** the gate is
green. Otherwise it is reverted. Each optimizer sees the history of experiments and the reference
implementations (`ff references`), and is told to learn from the fastest one. Once `main` meets
`benchmark.target`, optimizing stops. It resumes by itself if a later change pushes `main` back over
the target.

**Audit.** Each round works like this:
1. Fresh auditors (mutation, crash, regression) look for problems.
2. A judge decides, for each finding, whether a real caller can reach it. The judge also groups
   duplicates.
3. Fixers fix the reachable ones through the gate.
4. Unreachable findings go to `KNOWN_LIMITATIONS.md`, and the round is stamped in `EVIDENCE.md`.

The audit stops by itself after a round that finds nothing reachable at `converge_at` (default `high`)
or worse. Set `confirm_each_round: true` if you want to be asked before each further round.

**Phases.** The audit starts once the implement backlog is empty and the benchmark target is met.
Set `loops.audit.after` to change that; `[]` audits at once. The run is finished when `ff status`
shows the audit converged (`ff events` prints `audit-done`).

## 7. Steering

You rarely have to, but you can:

```sh
ff steer <run-id> "focus on the length prefix"     # message a running agent (it reads it at once)
ff steer <run-id> "..." --cascade                  # ... and every subagent it started
ff brief implement "prefer small commits"          # guidance for the next agents of a loop (or: all)
ff stop <run-id>                                   # stop an agent and its subagents
ff pause optimize | ff resume all                  # pause or resume loops
ff decide <id> yes                                 # answer a decision, if one is open
ff agent start fixer "make X faster"               # an ad-hoc agent outside the loops
```

The coordinator (`ff chat`) can do all of this for you. Ask it "what did we fix in the last 10
hours?", "why is the fixer taking so long?", or "tell the optimizer to try CRT".

## 8. The steering API

`ff run --api 127.0.0.1:8787` (or `ff serve` for the API alone). Every request needs
`Authorization: Bearer $FF_API_TOKEN`. If the variable is unset, a random token is printed at
start. Responses are JSON.

**Reading state**

| Request | Returns |
|---|---|
| `GET /status` | project, main, spend, running agents, last gate, backlog, audit round, open decisions, accounts, optimize target |
| `GET /events?since=<unix>&loop=<l>&limit=N` | the event log |
| `GET /runs?status=running` | runs: id, role, loop, parent, status, summary |
| `GET /runs/<id>` | one run: result, usage/cost, children, messages sent to it, transcript tail |
| `GET /runs/<id>/tree` | the run and all its subagents |
| `GET /findings` | audit findings: round, flavor, severity, status |
| `GET /decisions` | open questions for a human |
| `GET /accounts` | the subscription pool: ready or cooling down, runs, limit hits |

**Acting**

| Request | Body | Effect |
|---|---|---|
| `POST /agents` | `{"role", "task", "parent"?}` | start an agent in its own worktree; returns `{"run_id"}` |
| `POST /runs/<id>/steer` | `{"message", "cascade"?: false}` | message a running agent, or it and all its live subagents |
| `POST /runs/<id>/stop` | `{"cascade"?: true}` | stop it (and its subagents) |
| `POST /briefs` | `{"loop", "text"}` | guidance for the next agents of a loop |
| `POST /decisions/<id>` | `{"answer"}` | answer a decision |
| `POST /loops/<name>/pause` or `/resume` | | `name` may be `all` |

```sh
curl -s -H "Authorization: Bearer $FF_API_TOKEN" localhost:8787/status
curl -s -H "Authorization: Bearer $FF_API_TOKEN" -X POST localhost:8787/runs/<id>/steer \
     -d '{"message": "focus on the length prefix", "cascade": true}'
```

The intended pattern is that you steer one agent, and that agent steers its own subagents
(`ff subagent steer`). Or you pass `cascade` to reach all of them.

## 9. Agents, models and subscriptions

**Providers.** Every role names a provider:

| `kind` | Launched as | Use it for |
|---|---|---|
| `claude` | Claude Code, `claude -p` (stream-json) | Claude models |
| `codex` | Codex, `codex exec --json` | GPT models |
| `claude` + `env` | Claude Code pointed at an Anthropic-compatible endpoint (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`) | GLM and similar |
| `script` | any command | tests, other CLIs |

**Subagents.** A role lists the roles it may start, and its agents use them from inside their run:

```
ff subagent start explorer "where is the length prefix parsed?"   # -> run id
ff subagent steer <id> "..." | ff subagent wait <id> | ff subagent run <role> "<task>"
```

**Rotating subscriptions.** Several Claude or Codex logins can share the work. When one runs out of
credits, the agent's session moves to the next login and carries on:

```sh
ff add_login claude                # logs in a new account into ~/.formal-agents/claude/claude-1
ff add_login codex -- --device-auth
ff logins --check                  # who is ready, cooling down, how much each has run
ff account claude-1 disable|enable|cooldown 2h
```

`accounts.strategy` is `round_robin` (spread the work, the default) or `fill_first` (use one login
until it runs out).

## 10. The gate: what gets rejected

Every candidate goes through the same deterministic checks, in a fresh clone. Agents run the same
checks with `ff check` before they submit.

1. It must contain the current `main`. The gate rebases stale candidates itself; only a real conflict
   goes back to the agent.
2. `frozen.lock.json` is untouched (only the gate writes it), and so are the `spec.immutable`
   files: the harness that judges the agents (tests, benchmark, entry points). Only a human
   commits to those.
3. `workflow: generators`: regenerating must reproduce exactly the committed files.
4. Frozen statements are unchanged. A change must be recorded in `frozen_changes.yaml` with a reason
   and the name of a proof that the new statement implies the old one.
5. No FFI outside `ffi.allow` (banned by default; see below).
6. Every checked file passes within `file_timeout_seconds`, with nothing from `forbid`. Bend adds the
   `--verdict` kernel recheck; Lean adds an axiom audit (no hidden `sorry`).
7. `unit_tests`, `vectors` and `runtime_tests` pass.
8. Green: `main` moves to exactly that tree, and new statements are locked.

### FFI is banned unless the user allows it

Foreign code is code the proofs say nothing about, so a program could move its real work there and
still "check". The gate scans the whole candidate tree. It rejects:
- Lean `@[extern]` and `@[implemented_by]`;
- `extern_lib`, `moreLinkArgs`, `moreLeancArgs` and `precompileModules` in the lakefile;
- Bend foreign imports (`import "x.c"`).

These are allowed only in the files the user lists:

```yaml
ffi:
  allow: ["Spec/Trusted.lean", "lakefile.lean"]
  reason: precompiles are trusted C, as the user decided
```

`factory.yaml` lives outside the repository, so agents cannot widen the list.

## 11. Where things are

Everything about a run lives in `project.state_dir` (default `~/.formal-factory/<name>`):

| Path | Content |
|---|---|
| `factory.db` | the store: runs, events, backlog, gates, experiments, findings, decisions |
| `runs/<run-id>/` | `prompt.md`, `transcript.jsonl`, `result.json` of each agent |
| `gate/gate-<n>.log` | each gate's full log |
| `worktrees/` | the agents' working copies |
| `references/` | read-only copies of the benchmark's reference implementations |

Commands for reading it: `ff runs`, `ff show <id>`, `ff tail -f <id>`, `ff events --since 2h`,
`ff gates`, `ff experiments`, `ff findings`, `ff bill` (tokens and cost per role and model).

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Agents fail at once with "cannot be used with root" | Add `IS_SANDBOX: "1"` to the provider's `env`. The loop pauses after a launch failure; `ff resume <loop>`. |
| `backlog command timed out` | The backlog command is too slow. It should list open items in seconds; leave heavy checks to the gate. |
| A proof file fails "over budget" | It checks, but too slowly for `file_timeout_seconds`. The agent gets the gate log and must split or speed up the proof. |
| An agent seems stuck | `ff tail -f <id>` shows what it is doing. `ff steer <id> "..."` tells it something; `ff stop <id>` ends it, and the loop retries. |
| An item keeps failing | After `max_attempts` it is `blocked`. Read `ff show <run-id>`, then `ff brief implement "..."` and `ff backlog reopen <item>`. |
| Out of credits | `ff logins` shows cooldowns. Add another login with `ff add_login`; waiting runs pick it up. |
| You changed ff itself | Restart the daemon. Running agents are adopted, not lost. |

## 13. Command reference

| Command | Does |
|---|---|
| `ff init [--language bend\|lean]` / `ff validate` | write / check `factory.yaml` |
| `ff repo` | create and wire the repository (`project.github`), push the aggregated branch |
| `ff freeze --yes [--force]` | lock the frozen statements on main |
| `ff run [--loops ...] [--api host:port]` / `ff serve` | the daemon / the API alone |
| `ff chat` | the coordinator chat |
| `ff status [--json]`, `ff watch`, `ff events`, `ff digest` | where things are |
| `ff runs`, `ff show`, `ff tail [-f]` | agents and their transcripts |
| `ff steer [--cascade]`, `ff stop`, `ff brief`, `ff pause`, `ff resume`, `ff decide` | steering |
| `ff backlog [reopen <item\|all>]`, `ff gates`, `ff experiments`, `ff findings`, `ff bill`, `ff references` | per-loop details |
| `ff check`, `ff gate <branch>` | the gate's checks here / gate a branch now |
| `ff agent start\|wait` | ad-hoc agents |
| `ff add_login`, `ff logins`, `ff relogin`, `ff remove_login`, `ff account` | subscriptions |
| `ff subagent ...`, `ff inbox`, `ff note` | used by agents from inside their run |

## 14. Developing ff

```sh
pip install -e '.[test]'
FF_TEST_BEND=/path/to/bend pytest -q
```

The tests drive a toy Bend project with deterministic fake agents (`tests/fake_agent.py`,
`tests/fake_claude.py`). They cover the gate, every loop, adoption after restarts, steering,
subscription rotation and the phase hand-offs.
