# Compass — model and toolchain benchmarking

Compass answers three questions that a tokens-per-second number cannot:

1. **Does the model finish real work?** Verified completion, not throughput.
2. **What did the installed toolchain contribute?** Every enhancement is
   tracked as a named lane, so before/after and plugin-by-plugin are ordinary
   operations.
3. **What did the run cost?** Wall time, tokens, model calls, and the workspace
   outcome — per verified task.

## Layout

```
benchmarks/
  systems.json                 what was measured
  <suite>/manifest.json        suite + per-task contracts
  <suite>/tasks/<id>/          kickoff.md, fixtures, corpus, graders
src/cupola/
  lanes.py      toolchain inventory, snapshots, diff, activation gates
  suites.py     manifest schema, validation, fingerprint, dry-run plan
  systems.py    named systems under test, with path anchoring
  harness.py    transport + argv construction (never a shell string)
  runner.py     stage → spawn → budget → grade → persist
  graders.py    the seven grader types and their stated limitations
  transcript.py dsh session-log accounting (stdlib zstd)
  report.py     metrics, limitations, lane-bound reports
  compare.py    verdict precedence and three attribution modes
tests/fixtures/lanes/       captured composed-tree and diagnostic fixtures
tests/fixtures/transcript/  a real, sanitised session log
tests/fixtures/stub_agent.py  a model-free agent for testing the runner
```

## Core ideas

### A lane is one toggleable enhancement

The toolchain is not one opaque hash. It is an inventory of lanes discovered
generically from six sources: profile bundles, package dependencies, patch
entries in the composed loader tree, skills, always-on ruleset files, and MCP
servers. Nothing is hand-registered — a registry would drift, a scan cannot.

`dsh --dump-config` is the extraction mechanism because it composes every patch
layer, so it already knows what is disabled, overridden and in what order.
**It rewrites `cordis.yml` in the profile directory**, so the probe must run
unconfined or against a copy of `DSH_HOME`.

### Declared is not observed

dsh hooks fail open and silently: a hook whose write target is outside the
session workspace loses data with no error. A lane can therefore be active in
the composed tree and do nothing at run time.

Left unchecked this shows up as "this plugin changes nothing" — a false null
result. So a lane that has no activation gate, or whose gate fails, is marked
`no_op` and **excluded from attribution**.

### Score an artifact the model did not author

| Grader | Decides | Cannot prove |
|---|---|---|
| `pytest` | the fixture's criteria, in a fresh workspace, with pre-run hashes so a rewritten test fails | anything the fixture author did not write |
| `command` | a declared argv exits 0 | anything beyond that command |
| `schema` | required keys, types, minimum item counts | content quality |
| `claims` | literal must/must-not assertions; citations resolve inside a supplied corpus | that an unusually phrased claim is caught |
| `checklist` | required sections, length, item counts | judgement |
| `recompute` | declared arithmetic, recomputed independently, within tolerance | model *design* quality |
| `perf` | nothing; records that throughput was measured | — |

Every verdict carries its limitation into the report.

### Comparison precedence

Inherited from the comparator already proven in `minder_op/benchmark.py`, first
match wins:

1. suite fingerprint mismatch → `NON_COMPARABLE`
2. any safety counter non-zero → `FAIL` (absolute, any sample size)
3. fewer than 5 comparable runs → `INSUFFICIENT_SAMPLE`
4. verified completion rate down more than 5 points → `FAIL`
5. otherwise → `PASS`

`INSUFFICIENT_SAMPLE` and `NON_COMPARABLE` exit `2`, not `0`: a run that proves
nothing is not a success.

### Attribution modes

- `total` — full lane set versus none. What the whole stack buys.
- `marginal` — each lane alone against the empty baseline. What one plugin buys,
  with no aliasing. **This is the mode that scales as plugins accumulate.**
- `interaction` — a declared group against each member alone, pairwise. Above
  the cap (6 lanes) it returns the estimated schedule cost instead of running a
  combinatorial blow-up.

Only `marginal` may be summed; `total` includes every lane at once.

## Cost model

Two different numbers, deliberately kept apart:

- `tokens_per_verified_task` — what a *successful* task cost, on average.
- `tokens_per_success` — total spend divided by wins: the price of one win once
  failures are paid for.

## Operator notes

- Long runs are operator-initiated; nothing here runs automatically.
- `CUPOLA_BENCH_STATE` relocates run state (default
  `~/.local/state/cupola/bench`).
- `CUPOLA_SYSTEMS_FILE` relocates `systems.json`.
- `CUPOLA_LANE_PROFILE` chooses which profile's lanes a run records when the
  system declares none (default `web`).
- Bundles that fail to resolve, and patch layers targeting entries that no
  longer exist, are reported as lane warnings rather than being ignored.

## Appendix: ponytail hook tier on this machine (verified 2026-09-26)

Recorded because the A/B depends on it and it is easy to get wrong:

| Fact | Detail |
|---|---|
| Wiring | hand-rendered, **not** upstream: `hooks/dsh-hooks.json` and `README-dsh.md` are untracked in the clone at `~/.local/share/ponytail` |
| Upstream support | Claude Code, codex, copilot, cursor, pi, gemini/agy, hermes, opencode, qoder, swival — **no dsh**, and the npm package exposes an opencode plugin, not a cordis one, so `dsh plugin add` cannot install it |
| Two tier per machine | (1) a hook loader in the profile patch; (2) an always-on ruleset block in `~/.dsh/AGENTS.md`. Both must be toggled separately or the A/B is half-blind |
| Toggle precondition | the live patch row has **no `id`**, and dsh patch targeting requires one (`patch: entry "ponytail-hooks-bridge" not found`). Add the id once, then toggle with overlays |
| What it does | injects prompt context only. Its hooks emit `systemMessage`/`additionalContext` and never a deny/block decision, so this measures *context on/off*, not enforcement |
| Ruleset version | sha256 of `~/.local/share/ponytail/skills/ponytail/SKILL.md`; the rendered text for level `full` was 5229 chars at `da4fb09c…` |
| Live-effect marker | `<workspace>/.ponytail/.ponytail-active`; absence means a silent no-op, which must be reported as such and not as "the plugin does nothing" |
| Sandbox gotcha | `dsh --dump-config` rewrites `cordis.yml` in the profile directory and dies with EROFS under `workspace-write`; the probe and the host must run unconfined |
| Host restart | hook config is read once at host start, so restart between arms |
