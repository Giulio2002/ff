# formal-factory — manual

formal-factory (`ff`) runs AI agents that write a program **and its machine-checked proofs**
against a specification you freeze. A proof checker keeps score and a deterministic gate decides
what reaches `main`. Targets are written in **Bend 2** or **Lean 4**. The run goes through three
phases by itself, without waiting for you:

```
implement  ──(backlog empty)──▶  optimize  ──(benchmark.target met)──▶  audit  ──(converged)──▶  done
```

You watch it, talk to it through a coordinator chat or the HTTP API, and steer it when you want.

---

## 1. Install

```sh
git clone git@github.com:Giulio2002/formal-factory.git && cd formal-factory
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
  # remote: origin               # push main after every green gate
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

```sh
ff freeze --yes
```

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
2. `frozen.lock.json` is untouched. Only the gate writes it.
3. `workflow: generators`: regenerating must reproduce exactly the committed files.
4. Frozen statements are unchanged. A change must be recorded in `frozen_changes.yaml` with a reason
   and the name of a proof that the new statement implies the old one.
5. Every checked file passes within `file_timeout_seconds`, with nothing from `forbid`. Bend adds the
   `--verdict` kernel recheck; Lean adds an axiom audit (no hidden `sorry`).
6. `unit_tests`, `vectors` and `runtime_tests` pass.
7. Green: `main` moves to exactly that tree, and new statements are locked.

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
