# ff — formal-factory

AI agents build a program **and machine-checked proofs** that it meets a specification, in Lean 4 or
Bend 2, then make it fast and audit it. A proof checker keeps score; a deterministic gate decides what
reaches your repository.

## How to use it

Hand this repository to your coding agent (Claude Code, Codex, ...) and tell it what you want:

```
Read DOC_FOR_AGENT.md in https://github.com/Giulio2002/ff and set up formal-factory for me:

  I want: <the program — e.g. "an EVM in Lean 4">
  Spec:   <what it must do, or where the spec comes from — e.g. "ethereum/execution-specs, all forks
           up to Amsterdam; agents draft it and I approve it">
  Done when: <tests to pass, speed target — e.g. "passes all EEST fixtures; every evm-bench benchmark
              within 3x of geth">
  Trusted: <what may be assumed correct instead of proved — e.g. "precompiles and keccak via FFI">
```

The agent interviews you about what matters:
- how the spec is written and approved;
- what may be trusted;
- the benchmark;
- the GitHub repository;
- which models to use;
- your Claude/Codex subscriptions for rotation;
- whether you want the web UI, and whether to expose it.

Then it sets everything up and starts the factory. From then on:
- the web page (`--webui`) or `ff status` shows progress;
- the agent relays decisions that are yours, such as approving the spec;
- the repository fills up with green, proved commits.

`DOC_FOR_AGENT.md` is the full manual.
