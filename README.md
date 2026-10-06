# formal-factory

A software factory for **formal programs**: code that comes with machine-checked proofs that it
does what a frozen specification says, for every input. Agents produce code and proofs, a proof
checker keeps score, and a deterministic gate decides what reaches `main`. You talk to one
coordinator agent; everything else runs in the background.

It is the machinery described in
[Creating a "formal programs" factory and making a formal program of SSZ with it](https://x.com/GiulioRebuffo/status/2106866465622356201),
as a reusable Python package, for **Bend 2** or **Lean 4** targets.

```
            questions and decisions                     git push            green
   me  <---------------------------->  coordinator  ...........>  gate  -----------> main
                                        |      ^                    ^ red: back to the agent
                                 briefs |      | reports            |
                                        v      |                    |
        +--------------------------------------------------------------------------+
        |  implementers   optimizer   auditors (fresh each round)   judge   fixers  |
        |   (each worker may start, steer and wait for its own subagents)          |
        +--------------------------------------------------------------------------+
```

## The loops

**Implement.** Pick an open item (a missing law, an unimplemented type) from your backlog
command, change the code and its proofs, check, and make sure no frozen statement moved.
How agents change the program is your choice, `project.workflow`: `direct` (they edit code
and proofs) or `generators` (bend-ssz style: they edit generator scripts, `commands.regenerate`
writes the `generated` files, and the gate rejects any hand edit). Then the gate. A red gate goes back to the same agent with the gate's log.

**Optimize** (autoresearch with a veto). One change, benchmarked on one number. It's kept
only if the number improves by `min_improvement_pct` *and* every proof, unit test and
vector still passes; otherwise it's reverted. The history of experiments goes into the
next optimizer's prompt.
With `benchmark.target` set, the loop stops experimenting once main meets the target (it
re-measures whenever main moves, and resumes if main falls short again), and an audit loop with
`after: [implement, optimize]` starts by itself once the backlog is empty and the target is met.

The optimizer learns from the **fastest baseline**: `benchmark.references` names the reference
implementations the benchmark compares against (a local path or a git repository, its key files,
and notes on what makes it fast). The factory keeps a read-only copy (`ff references`) and every
optimizer prompt starts from it: study how the reference gets its speed, then port the idea through
the generators, proved. Any role's prompt can use the same `{references}` block.

The optimizer may change any code and any proof that is not frozen (helper laws included); frozen
statements stay as they are.

**Audit.** Each round gets *fresh* auditors that have never seen the code, in three flavors:
- **mutation:** plant plausible mistakes and see whether a proof complains;
- **crash:** hostile input through the public API;
- **regression:** what did the last fixes quietly weaken?

A **judge** asks one question per finding: can a real caller reach this through the public
API? If yes, a **fixer** adds a law or fixes the code (through the gate). If not, the
finding is documented in `KNOWN_LIMITATIONS.md`. The round ends by re-stamping
`EVIDENCE.md` through the gate. Then the factory asks you whether to run one more round,
or stops when critical findings reach zero.

## The gate

Deterministic Python, not an agent, so it can't be talked into a shortcut. For each
candidate branch:

1. The candidate must contain the current `main`. Stale candidates are rebased and
   resubmitted automatically; real conflicts go back to the agent.
2. Cold: a fresh clone, no caches.
3. The lock file is untouched. Only the gate writes it, and it is always read from `main`.
4. With `workflow: generators`: regenerate everything. The tree must be byte-identical to
   what was committed, so nobody hand-edits generated files.
5. Frozen statements are unchanged, or the change is recorded in `frozen_changes.yaml`
   with a reason **and the name of a proof that the new statement implies the old**. That
   proof is checked like everything else.
6. Nothing forbidden appears. Every file checks, each within its time budget. Plus
   language extras:
   - Bend: optional `--verdict` kernel recheck.
   - Lean: `#print axioms` on every frozen theorem, against an allowlist, so a hidden
     `sorry` fails.
7. Unit tests, test vectors and runtime tests pass.
8. Green: new statements are added to the lock, and `main` moves to exactly that tree
   (compare-and-swap); it's pushed if a remote is configured. A remote build server is
   supported (`gate.host: user@host` runs `ff gate-run` there), but not yet tested.

Agents run the same checks in their worktree with `ff check`.

### Frozen statements

What counts as a statement depends on the language:
- **Bend:** every top-level `law` block. In frozen spec files, also every `def`/`type`.
- **Lean:** every `theorem`/`lemma` up to its `:=`, so proofs may change but statements
  may not. Every other declaration is frozen in full.

`ff freeze --yes` is the human step that locks the initial spec on `main`. After that, only
the gate writes the lock.

## Agents: Claude, GPT, GLM

Every role picks a provider in `factory.yaml`:

| Provider kind | Launched as | Steering |
|---|---|---|
| `claude` (Claude Code) | `claude -p --input-format stream-json --output-format stream-json` | messages are written into the live session as new user turns |
| `codex` (GPT via Codex) | `codex exec --json` | `ff inbox` during the run; what is left is delivered by `codex exec resume <session>` |
| GLM | `kind: claude` with `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` env vars (an Anthropic-compatible endpoint) | as Claude |
| `script` | any command | `ff inbox` |

Loop agents run in processes of their own, and each run records its cycle (task, attempt,
backlog item, baseline). Restarting the daemon (new code, new settings) does not kill them: the
next daemon adopts every run still going and carries its cycle on (commit, gate, retry); only runs
whose process is gone are stopped and their items reopened.

Each run gets:
- its own git worktree and branch;
- a run directory with the prompt, the raw transcript and the result;
- token and cost accounting (`ff bill`);
- a timeout, and a stop flag that any process can set.

### Subagents

Codex has no native subagents, and a subagent may want a *different* model than its parent
(a Claude fixer asking a GLM attacker), so the factory provides subagents itself. A role
lists the roles it may use:

```yaml
roles:
  fixer:
    provider: claude
    subagents: [explorer, prover, attacker]
```

Inside its run, the worker uses:

```
ff subagent start attacker "try to break the fix in src/decode.bend"   # -> run id
ff subagent steer <id> "focus on the length prefix"
ff subagent wait <id>        # or: ff subagent run <role> "<task>" (start + wait)
ff subagent status | stop <id>
```

Subagents run in the parent's worktree (or `--own-worktree`), on their own provider. They're
depth-limited (`max_subagent_depth`) and can be steered by their parent or through the API.

## Rotating subscriptions

Keep several Claude and Codex subscriptions in one pool, `~/.formal-agents/`, shared by every factory
on the machine:

```sh
ff add_login claude              # runs `claude auth login` in its own CLAUDE_CONFIG_DIR -> account claude-1
ff add_login claude work-max     # named
ff add_login codex -- --device-auth   # `codex login --device-auth` in its own CODEX_HOME (headless servers)
ff logins [--check]              # who is logged in, cooling down, runs, limit hits, spend
ff relogin <name> | ff remove_login <name> [--delete]
ff account <name> disable|enable|cooldown 2h|clear
```

Each account is a separate config directory (`~/.formal-agents/claude/<name>`, `.../codex/<name>`),
so the CLIs keep their own credentials and sessions apart. Every Claude Code or Codex agent takes an
account from the pool:

- `round_robin` (default): the least recently used account, so work spreads over every subscription;
  `fill_first`: one subscription until it runs out, then the next, around the list.
- When the CLI reports that the account is out of credits, the account cools down until the reset
  time the message names (or `default_cooldown_minutes`). The agent's session file is copied to the
  next account and resumed there (`claude --resume`, `codex exec resume`), so the agent keeps its
  context and carries on. If every account is cooling down, the run waits for the first one back
  and the coordinator sees an `accounts-exhausted` event.
- `providers.<name>.accounts`: `auto` (default; the pool when it has accounts of that kind), `none`
  (the CLI's own login), or a list of names. A provider that brings its own credentials in `env`
  (GLM's `ANTHROPIC_AUTH_TOKEN`) never uses the pool.

`ff status`, `ff runs` (the `account` column) and `GET /accounts` show which subscription did what.

## The steering API

`ff run --api 127.0.0.1:8787` (or `ff serve`) starts the HTTP API. Every request needs
`Authorization: Bearer $FF_API_TOKEN` (a random token is printed if it's unset).

```
POST /agents                {"role": "fixer", "task": "..."}          -> {"run_id"}
POST /runs/<id>/steer       {"message": "...", "cascade": false}     the agent, or the agent and all its live subagents
POST /runs/<id>/stop        {"cascade": true}
GET  /runs/<id>             result, usage, children, messages, transcript tail
GET  /runs/<id>/tree        the run and its descendants
GET  /status | /events?since= | /runs?status=running | /findings | /decisions
POST /briefs {"loop","text"} | /decisions/<id> {"answer"} | /loops/<loop>/pause|resume
```

So you steer one agent, and that agent steers its subagents with `ff subagent steer`, or
you cascade the message to all of them.

## The coordinator

`ff chat` opens the coordinator role as an interactive session in the factory's state
directory. Its tools are the `ff` commands:
- `ff status`
- `ff events --since 10h`
- `ff runs`, `ff show`, `ff tail`
- `ff findings`, `ff gates`, `ff experiments`, `ff bill`
- `ff steer`, `ff brief`, `ff pause`/`ff resume`, `ff decide`

Ask it "what did we fix in the last 10 hours?" or "why is fixer A taking so long?".

## Configuration

One file configures everything: the target repo and language, the checker, the commands
(regenerate, unit tests, vectors, runtime tests), the frozen globs, the benchmark, the gate,
the providers, every role (provider, model, prompt, timeout, subagents, env, args), the
loops and the limits. See `examples/factory.bend.yaml` and `examples/factory.lean.yaml`.
`ff init --language lean` copies one; `ff validate` checks it.

Role prompts default to the built-in ones in `ff/roles/*.md`. Any role can override its
prompt inline or with a file. `${VAR}` in the YAML is read from the environment, so API keys
stay out of the file.

## Running it

```sh
pip install -e .            # Python 3.11+, pyyaml
ff init --language bend     # then edit factory.yaml
ff validate
ff freeze --yes             # lock the human-written spec on main
ff run --api 127.0.0.1:8787 # the loops, in the background (nohup/tmux)
ff chat                     # talk to the coordinator
```

`project.repo` should be the factory's own clone of the target. Agents never touch its
checkout; only the gate moves `main` there (and pushes it).

## Tests

```sh
pip install -e '.[test]'
FF_TEST_BEND=/path/to/bend pytest -q
```

The end-to-end tests run a toy Bend project with a deterministic fake agent (`tests/fake_agent.py`)
and cover:
- the gate refuses a weakened frozen law and a hand-edited generated file, and accepts the
  generator fix;
- a full audit round: fresh auditors, then the judge, then parallel fixers (including a
  rebase conflict), then evidence and known limitations merged through the gate, with the
  new laws locked;
- the API steering an agent that relays the message to its own subagent.

The real Claude Code and Codex paths were also checked by hand, each steered mid-run.
