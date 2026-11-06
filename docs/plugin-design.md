# Plugin / capability design

How a new service — memory, compactor, graph, skill provider, grader — attaches
to the Compass harness. Derived from three surveys of the surrounding ecosystem
(see `docs/research/`), not from preference.

## The finding that decides everything

Across 14 extension ecosystems and ~30 capability projects, capabilities divide
by **where they sit**, and that determines how they can be attached:

| Position | Examples | Attach how | Deterministic? |
|---|---|---|---|
| **Proxy** (agent ↔ model HTTP) | kompact, headroom, any gateway | env var (`ANTHROPIC_BASE_URL=…`) | yes — fires on every request |
| **Sidecar / service** | Graphiti, cognee, LightRAG, LMCache-MP | config + a URL | yes, if reset per run |
| **MCP tool** | graphiti, repomix, claude-context, graft, MCP memory | one `mcpServers` entry | **no — fires only if the model chooses** |
| **In-process plugin** | OpenHands condensers, LangMem, aider RepoMap | agent code change | yes, but requires forking the agent |

Ranking for a benchmark: **proxy > sidecar > MCP tool > in-process plugin.**
A proxy compacts deterministically; an MCP tool is an *opportunistic* intervention
and cannot be attributed the same way. An in-process plugin means forking the
agent, which destroys the thing being measured.

## What this means for cupola

Cupola must **not** grow an in-agent plugin API. Everything cupola needs to
attach is out of process. Cupola's job is narrower and more useful:

> declare the capability set, pin it, prove each one actually activated, and
> reset it between runs.

Three attachment classes cover the ecosystem:

1. **`env`** — set environment on the system under test. Covers every proxy.
   Already supported: `System.env` and `RunnerConfig.extra_env`.
2. **`mcp`** — a stdio or HTTP MCP server, injected **per run as a `--patch`
   overlay** (verified: MCP entries are patch rows and `--patch` is
   repeatable). No agent fork, and the operator's live profile is never edited.
3. **`http`** — a service the harness calls directly for reset/snapshot, or that
   the agent reaches by URL.

A capability that only exists as agent code is **out of scope by design**. It is
recorded as `in_process: true` so a report can say the comparison is not
like-for-like, rather than silently hiding a fork.

## Capability manifest

One file per capability, in TOML to match `ProfileSpec`, with
`graphiti`-style precedence **lock > entry points > directory**.

```toml
schema_version = 1
id            = "graphiti-memory"          # stable; the lane id
kind          = "memory"                   # memory|compactor|graph|skill|grader|service
version       = "0.22.0"                    # exact, like a Terraform provider lock
description   = "Bi-temporal knowledge graph memory, exposed over MCP."
provides      = ["memory.retrieve", "memory.record"]   # asserted at load

[attach]
type      = "mcp"
transport = "stdio"                         # stdio | http
command   = ["npx", "-y", "graphiti-mcp"]
env       = { NEO4J_URI = "${GRAPHITI_NEO4J_URI}" }

[determinism]
writes_state = true                         # the agent can write back
resettable   = true                         # required when writes_state is true
[determinism.reset]
command = ["graphiti-admin", "clear-graph"]

[health]
command  = ["graphiti-mcp", "--selftest"]    # mandatory; see below
expected = "ready"
```

Fields justified by what the ecosystem got wrong:

| Field | Why it exists |
|---|---|
| `description` | The only field an agent must *read* to decide. Copy of `SKILL.md`'s description-first contract. |
| `provides` | Declared capabilities are asserted at load. A capability that declares something it cannot resolve **fails**, instead of loading and doing nothing. |
| `version` | Exact versions, plus a generated lock. Terraform's lesson: a range without a lock is not reproducible. |
| `attach` | Tiers 1–3 above. |
| `determinism.writes_state` + `resettable` | The reproducibility gate. |
| `health` | Mandatory, because every ecosystem surveyed has an instance of "loaded but inert". |

## Discovery

Layered, with explicit precedence so **both** paths work with no ambiguity:

1. `cupola.lock` (highest) — generated, exact versions and hashes.
2. entry points `[project.entry-points."cupola.capabilities"]` — installer path;
   `pip install` *is* registration (pytest / VS Code model).
3. `capabilities/<id>.toml` in the benchmark root — the cold-agent path: write
   one file, run one command, no packaging step.

An agent that has never seen this repo can attach a service by writing the file
in (3). A repo owner can ship a package for (2). Reproducibility comes from (1).

## Anti-silent-degradation

The surveys found the same failure in Claude Code (unknown manifest keys
silently stripped, plugin still loads), VS Code (extension activates and
registers nothing), and Codex (a valid server listed as "Tools: none"). Cupola
already solved this shape for lanes — a lane with no activation gate, or a
failing one, is `no_op` and **excluded from attribution**. Capabilities reuse
that machinery rather than inventing a second mechanism:

- `provides` must resolve at load, or the capability is rejected.
- `health` is mandatory; `cupola bench caps doctor` runs it.
- Unknown keys are a **hard error**, never stripped.
- `--dry-run` prints the *resolved* capability set before any run — extending
  the dry-run that already exists.
- A capability that loads but fails its gate is excluded from attribution and
  reported, exactly as an inert lane is today.

## Reproducibility gate

From the surveys, three sources of cross-run contamination:

- semantic caches returning a *similar* answer rather than the same computation
  (GPTCache, LightRAG's `ENABLE_LLM_CACHE` on by default, Graphiti's LLM cache);
- LLM extraction being non-deterministic **and concurrency-dependent** (Graphiti
  `SEMAPHORE_LIMIT`, cognee, LightRAG);
- prompt/KV state leaking between runs (LMCache, prompt caches).

So a capability that `writes_state` **must** declare `resettable` and a reset
command. If it cannot be reset, the run is marked `reproducible: false` and
`compare` returns `NON_COMPARABLE` — extending the verdict precedence that
already exists, rather than adding a parallel rule. The harness additionally
requires: a fresh working directory per run, semantic caches off, fixed
temperature, and pinned concurrency.

## Why not just adopt each project's own config

Because reproducibility is cupola's product, not the capability's. A plugin must
not be able to declare itself un-gated, un-resettable, or exempt from the
verdict rules. The safety counters and grader `limitations` stay core-owned.

## Sustainability claims

A capability that claims a carbon benefit is making a measurement claim, so the
manifest carries the claim and its evidence separately, and the claim is worth
nothing without the evidence:

```toml
[sustainability]
# What the capability says about itself. Assertions, not results.
claims = ["energy.reduces", "carbon.intensity_aware"]

# The arm that can disprove a claim. `metric` is a report metric key; the claim
# holds only if the delta goes the stated way with completion held.
[[sustainability.evidence]]
claim     = "energy.reduces"
baseline  = "reports/a.json"     # capability absent
candidate = "reports/b.json"     # capability present, same suite fingerprint
arm       = "minder"
metric    = "sci_gco2e_per_verified_task"
max_delta = 0.0
```

Rules, inherited from the lane discipline above rather than invented here:

- A claim **with no evidence is `unproven`** and is excluded from credit, exactly
  as an inert lane is excluded from attribution. It is not "probably fine".
- Evidence must come from `cupola bench compare`, so both arms share a suite
  fingerprint, at least `MIN_COMPARABLE_RUNS` runs, and the same carbon
  methodology (intensity source/basis/resolution and embodied method). A delta
  across two methodologies is marked `comparable: false` and does not count.
- Completion has to hold: a capability that emits less because it fails more has
  not reduced carbon, it has reduced work.
- `cupola sci --report <path>` prints the `principles` list, including
  `carbon_efficiency_proven`, which is `pass` only when such an A/B exists.

This is the concrete form of "does installing this plugin even help?" — see
[sci.md](sci.md) for the end-to-end recipe.

## Deliberate non-goals

- No in-agent plugin API and no agent fork.
- No plugin lifecycle, version-solving, or sandboxing.
- No new abstraction over `System.env` — tier 1 needs no code.
- No metric extensibility: interpreting `unsafe_executions` is not a plugin's call.

## Prerequisite: three closed seams

Capabilities cannot attach cleanly until these are registries instead of frozen
lists. All three are small and verified by test:

| Seam | Today | Change |
|---|---|---|
| Grader type | `GRADER_TYPES` tuple in `suites.py`; runner accepts what the validator rejects | validator consults the grader registry |
| Transport | `if process / elif http` in `harness.py` + `runner.py` | transport registry; `mcp` becomes a first-class transport |
| Lane source | hardcoded append sequence in `lanes.py` | `LaneSource` protocol; the six current detectors become built-ins |

**Acceptance test for all of it:** register a capability, a grader, and a lane
source from a *test-local* module and drive a full run with **zero edits outside
that module**. If it needs a core edit, the seam is not open.
