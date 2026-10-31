# CUPOLA — revised PRD and agentic implementation plan

Version 6.0 • 15 September 2026 • Supersedes the supplied v5 design

Status: ready for a bounded prototype; hardware capability and release performance remain unverified. This revision corrects the specification, not an existing implementation. No source repository was supplied and this environment has no /dev/dri GPU device.

## 1. Decision and product purpose

Build Cupola as a reproducible local inference supervisor for a single developer using an RX 7900 XTX workstation. Its job is to launch a tested model configuration, explain whether it fits, keep lifecycle operations predictable, and help the developer recover from backend failures without guessing flags.

The initial user, operator, and likely buyer are the same person. The trigger is starting or recovering a local coding session. The current alternative is a pinned llama-server command plus a service unit and a configuration file. Cupola must demonstrate less setup and recovery effort than that alternative before adding a broad platform.

Observed: the supplied PRD describes memory, template, lifecycle, and compatibility risks but provides no incident logs, benchmark runs, implementation, or user studies. Inferred: reproducible profiles and lifecycle diagnostics are the highest-value first intervention. Hypothesis: these reduce interruption time enough to justify another tool. Unknown: actual hardware configuration, installed OS/kernel/driver, RAM, storage, model artifacts, agent client versions, workload sizes, and frequency of failures.

Preserve the longer-term vision through gated extensions: Stencil for artifact compatibility, Compass for local evaluations, Sentinel for telemetry, then optional heterogeneous inference. Remove invented benchmark results and unsupported guarantees from product messaging.

Primary outcome: interruption minutes per developer-hour, measured as elapsed time from a runtime-caused blocked task to successful resumption, divided by observed developer-hours[cite: 1]. Record incident type and avoid double counting overlapping incidents[cite: 1]. Guardrails: task completion, context integrity, privacy, and no increased loss of work[cite: 1]. Establish a baseline before choosing an improvement target[cite: 1].

## 2. Findings and corrections

Severity describes implementation consequence, not presentation quality[cite: 1]. These are design-review findings; proposed fixes still require implementation evidence[cite: 1].

| ID | Severity | Original problem | Correction and verification |
| --- | --- | --- | --- |
| F01 | Blocker | Read the first 16 KB of GGUF and compute exact memory[cite: 1] | GGUF has variable metadata and tensor descriptors[cite: 1]. Use a bounded parser over the required metadata region, validate file ranges and resource limits, then calibrate with backend observations[cite: 1]. Test metadata exceeding 16 KB and truncated inputs[cite: 1]. [S2][cite: 1] |
| F02 | Blocker | Universal hybrid attention formula and fixed 1:4 interval[cite: 1] | Use architecture-specific support and recurrent-state accounting; do not infer memory from a model name[cite: 1]. Unknown architectures are unsupported in strict mode until measured[cite: 1]. |
| F03 | Major | Zero host RAM and zero PCIe traffic conflated with GPU layer placement[cite: 1] | Require no deliberate CPU layer placement for approved strict profiles[cite: 1]. Permit normal host metadata, staging, tokenization and OS page cache[cite: 1]. Report actual allocation evidence; a layer flag cannot prove absence of all transfers[cite: 1]. |
| F04 | Major | Sysfs VRAM usage equated to compositor usage with fixed reserve[cite: 1] | Device-wide usage includes other consumers[cite: 1]. Identify the GPU by PCI identity; sample total/free memory and use a configurable reserve calibrated under desktop load[cite: 1]. Treat preflight as a race-prone estimate[cite: 1]. |
| F05 | Blocker | Scrub freed GPU pointers through hipMemset in the supervisor[cite: 1] | Invalid lifecycle: clearing requires a valid allocation owned by the responsible process[cite: 1]. Delete this requirement[cite: 1]. Backend-owned clearing before free could be future work, but is not proof that all device memory is sanitized[cite: 1]. HIP exposes distinct free and memset operations[cite: 1]. [S3][cite: 1] |
| F06 | Blocker | Regex filter neutralizes prompt injection[cite: 1] | Remove the security claim[cite: 1]. Enforce permissions, tool execution isolation, network policy and budgets in the agent runner[cite: 1]. Cupola bounds inference requests and never executes model tool calls[cite: 1]. |
| F07 | Major | Process groups guarantee <500 ms memory reclamation; zombies hold VRAM[cite: 1] | Process exit, child reaping and driver reclamation differ[cite: 1]. Escaped descendants and stuck driver work need explicit handling[cite: 1]. Report failure when cleanup cannot be confirmed; never kill unrelated GPU processes[cite: 1]. |
| F08 | Blocker | Universal Jinja template and naive reasoning-tag extraction[cite: 1] | Remove supplied template[cite: 1]. It assumes one token format, omits tool definitions, cannot safely serialize arbitrary JSON, assumes nonempty messages, and mishandles null assistant content and call correlation[cite: 1]. Pin model-specific assets and test multi-turn tool traces[cite: 1]. [S4][cite: 1] |
| F09 | Major | Example loads one model with another model family's template[cite: 1] | Bind weights, tokenizer, template and parser as a tested set[cite: 1]. No generic template fallback advertised as equivalent[cite: 1]. |
| F10 | Major | Grammar guarantees successful tools and all valid JSON[cite: 1] | Constrained decoding is scoped to supported schemas and complete generation[cite: 1]. Truncation, unsupported schemas and semantic mistakes remain possible[cite: 1]. Validate arguments after assembly and before execution in the client[cite: 1]. |
| F11 | Major | Context shifting semantically discards terminal logs and preserves state[cite: 1] | The runtime does not know which facts are essential[cite: 1]. Agent owns compaction and complete tool-call/result pairs[cite: 1]. Reject overflow with an actionable error; never silently trim requests[cite: 1]. |
| F12 | Major | Slot 0 is a permanent system-prompt region[cite: 1] | Do not conflate request slots with retained prompt tokens[cite: 1]. Remove the mechanism[cite: 1]. Use supported backend cache behavior only after tests[cite: 1]. |
| F13 | Major | Sub-300 ms cache restoration and <5 s hot swap guaranteed[cite: 1] | Measure by artifact size, disk, cache state and transfer path[cite: 1]. mmap alone does not establish direct NVMe-to-VRAM transfer or eliminate loading costs[cite: 1]. |
| F14 | Major | MTP speedup and every named head/model assumed supported[cite: 1] | Exact backend/model/quant/device combination must pass a capability probe and paired benchmark[cite: 1]. Default speculative decoding off[cite: 1]. |
| F15 | Major | Fixed Wave32 policy, universal flash attention/batch tuning[cite: 1] | Leave kernel execution to supported backend implementations[cite: 1]. Tune only measured configurations, with memory and task-quality regressions checked[cite: 1]. |
| F16 | Major | CQLPI and 0.85 threshold establish frontier parity[cite: 1] | Delete the score and example results[cite: 1]. Different benchmarks, harnesses, dates and budgets are incomparable; a speed/cost multiplier cannot establish quality parity[cite: 1]. |
| F17 | Major | Fixed 35/45 tok/s and 1,400 prefill SLA[cite: 1] | Replace with measured latency distributions and successful task time[cite: 1]. Distinguish cold/warm and short/long context; targets remain provisional[cite: 1]. |
| F18 | Major | Thermal stress hours predict remaining GPU life; delta implies servicing[cite: 1] | Keep temperature, power and trend observations[cite: 1]. Delete lifetime and service diagnoses without calibrated hardware-specific evidence[cite: 1]. The activation energy and thresholds are assumptions, not validated device parameters[cite: 1]. |
| F19 | Major | Every AM5 Ryzen provides the specified iGPU[cite: 1] | Detect exact CPU and device capabilities[cite: 1]. Compare CPU embeddings first; shared-memory iGPU work may contend for bandwidth and power[cite: 1]. |
| F20 | Major | Eliminates systemd while installing lingering systemd services[cite: 1] | Choose one service owner[cite: 1]. Foreground mode first, optional user service later; no automatic linger or privileged init[cite: 1]. |
| F21 | Major | OpenAI compatibility implies every coding client works[cite: 1] | Test exact client/version/protocol, tool schema, stream and cancellation behavior[cite: 1]. Development tools used to build Cupola are separate from clients configured to use Cupola[cite: 1]. |
| F22 | Major | Intent classifier silently swaps models and chooses context[cite: 1] | Replace with explicit profile selection[cite: 1]. Fix model identity per active request/session; prompt classification cannot reliably predict token requirements or compatibility[cite: 1]. |
| F23 | Major | Hash three repeated commands or stop after 30 calls[cite: 1] | Legitimate polls repeat, and varied harmful calls evade hashes[cite: 1]. Agent runner uses elapsed-time, cost and execution budgets plus meaningful-progress checks[cite: 1]. No routine approval every 30 calls[cite: 1]. |
| F24 | Major | seccomp parsing helper proves GGUF/backend safety[cite: 1] | Parser limits reduce one attack surface[cite: 1]. Backend and GPU driver still process untrusted inputs[cite: 1]. Use vetted artifacts, patched binaries, minimal privileges and restricted access; no “100% payload rejection” claim[cite: 1]. |
| F25 | Major | Fixed eight-week roadmap assumes all research succeeds[cite: 1] | Use dependency gates below[cite: 1]. Defer schedule commitment until the first hardware spike establishes feasibility and integration effort[cite: 1]. |
| F26 | Minor | Qwen 3.8 variants and demo scores presented as factual[cite: 1] | Treat all unverified names, SHAs, memory totals and scores as placeholders; select real artifacts by resolved identity and license during the spike[cite: 1]. |

Current upstream evidence matters: llama-server documents fitting, several API formats and other primitives; it marks --defrag-thold deprecated[cite: 1]. Recheck against the selected commit before implementing adapters[cite: 1]. [S1][cite: 1] AMD support is tied to a versioned GPU/OS/kernel matrix; “Fedora / Ubuntu / ROCm 6.x” is insufficient as a support contract[cite: 1]. [S5][cite: 1]

## 3. Corrected MVP contract

### Scope

One selected Linux configuration, one discrete GPU, one pinned llama.cpp build, one qualified coding model profile and one proven client[cite: 1]. Python 3.11+ is a proposed control-plane choice: standard-library asyncio/subprocess, TOML input, typed records, and pytest for meaningful failure tests[cite: 1]. Resolve and lock exact versions in the repository[cite: 1]. No native GPU programming or model implementation is needed for this slice[cite: 1].

Commands are proposed Cupola interfaces, not available software today[cite: 1]:

| Command | Required behavior |
| --- | --- |
| cupola doctor --json | Read-only device, permissions, OS, backend and capability report; unknown sensors stay unknown[cite: 1]. |
| cupola profile validate coding | Validate schema, hashes, paths and supported capabilities without loading weights[cite: 1]. |
| cupola plan coding --json | Explain requested versus effective settings, memory evidence and rejection reasons[cite: 1]. No mutation[cite: 1]. |
| cupola up coding | Acquire exclusive ownership, validate, launch, verify readiness and publish actual configuration[cite: 1]. |
| cupola status --json | Report state, owned backend identity, profile hash, health and last failure[cite: 1]. |
| cupola down | Stop admission, stop owned backend, reap and report cleanup outcome; repeated calls are safe[cite: 1]. |

For the first slice the client connects directly to llama-server on loopback[cite: 1]. No custom proxy or intent router[cite: 1]. The control socket is private under XDG_RUNTIME_DIR[cite: 1]. Persistent configuration uses XDG_CONFIG_HOME, logs/state XDG_STATE_HOME, assets XDG_DATA_HOME and disposable data XDG_CACHE_HOME, with documented defaults[cite: 1]. Use atomic writes and permissions appropriate to a single-user application[cite: 1]. Credentials stay out of versioned profiles and logs[cite: 1].

A service process owns the backend and control socket[cite: 1]. `up` attaches to an existing service or starts one using an explicit non-shell argument vector, waits for readiness and returns failure if startup fails[cite: 1]. Foreground mode is available for debugging[cite: 1]. Service installation is optional and must not introduce a second restart owner[cite: 1].

### Profile manifest

Required fields: schema version; alias; weights path, digest, source repository/revision and license record; tokenizer/template provenance and digest; backend commit/build identity and binary digest; OS/kernel/runtime/device qualification record; approved context and concurrency; K and V cache types; reserve bytes; allowed API routes; capabilities; probe evidence paths[cite: 1].

An embedded template is acceptable when qualified with that GGUF[cite: 1]. External templates require an explicit compatible binding[cite: 1]. Configuration never contains a mutable “latest” identity in a qualified profile[cite: 1]. Invalid or unknown keys produce clear errors[cite: 1]. No shell command strings in model metadata[cite: 1]. Checksums establish identity, not trustworthiness[cite: 1].

`plan` reports supported / unsupported / unknown for each capability[cite: 1]. A help flag is discovery evidence, not proof that a model/device combination works[cite: 1]. Unknown performance does not become zero or a green status[cite: 1].

### Memory planning

Use bytes internally and label GiB/MiB correctly[cite: 1]. For a conventional attention architecture, an approximate cache term is:

M_KV ≈ N_seq × sum over attention layers [C_l × (H_K,l × D_K,l × B_K,l + H_V,l × D_V,l × B_V,l)][cite: 1]

Here C_l is the backend's actual allocated token capacity per sequence for that layer, and B represents effective encoded bytes including quantization block overhead[cite: 1]. This is an estimate, not an allocator specification[cite: 1]. Shared caches, padding, sliding windows and recurrent architectures need separate handling; do not multiply by sequence count twice when the backend reports aggregate capacity[cite: 1].

The proposed admission budget is:

weights_on_device + KV + recurrent_state + compute_buffers + other_backend_allocations + safety_reserve ≤ observed_available_device_memory[cite: 1].

Estimate, then probe a conservative profile and record observed peak allocation and backend placement logs[cite: 1]. Begin with one sequence and no draft model[cite: 1]. Qualify larger context points separately[cite: 1]. Reuse upstream fitting only if its result obeys the profile policy[cite: 1]. Reject CPU placement under strict mode; never silently lower KV precision[cite: 1]. Context reduction may follow a profile's preauthorized minimum, but show the effective capacity before accepting work[cite: 1]. Requests still require prompt plus requested output to fit that capacity[cite: 1].

If memory changes between planning and loading, terminate the failed owned backend, confirm cleanup as far as observable and make at most one explicitly logged lower-capacity retry allowed by the profile[cite: 1]. Otherwise fail with a reason[cite: 1]. Do not retry malformed files or unsupported architectures as memory errors[cite: 1]. Never claim a mathematical estimate guarantees no OOM[cite: 1].

### Lifecycle and errors

States: STOPPED → VALIDATING → STARTING → READY → STOPPING → STOPPED[cite: 1]. Validation/start/readiness failures enter FAILED[cite: 1]. Unexpected exit enters FAILED; explicit retry may re-enter VALIDATING[cite: 1]. DEGRADED describes a live service with an unconfirmed backend or cleanup fault and blocks new launches until ownership is resolved[cite: 1].

Acquire a lock before any spawn[cite: 1]. Store PID plus process start identity and instance identifier; a stale PID alone is never authority to signal[cite: 1]. Health must verify the owned backend, not just an occupied port[cite: 1]. Bind failure must not trigger killing whoever owns that port[cite: 1].

Proposed default deadlines, configurable and subject to hardware qualification: startup 180 s; graceful stop 10 s; forced-exit observation 5 s[cite: 1]. These are control timeouts, not guaranteed driver response times[cite: 1]. Foreground mode uses process groups with documented limits[cite: 1]. Optional Linux service deployment uses cgroup containment where available[cite: 1]. A stuck GPU operation can exceed all process-level guarantees: surface diagnostics, avoid restart loops and never automatically reset the display GPU[cite: 1].

For MVP, changing models requires down/up and is explicitly disruptive[cite: 1]. No hot-swap promise[cite: 1]. Future drain support needs an admission gate, active-request tracking, a deadline, cancellation semantics and rollback behavior[cite: 1]. Do not implement those before a real need for a proxy is shown[cite: 1].

### Privacy and trust

Single-user local inference is the threat-model boundary[cite: 1]. Loopback binding is mandatory by default; authentication where supported and origin policy need verification[cite: 1]. Never expose the backend or administrative control endpoint to a network by default[cite: 1]. Minimal inherited environment; no cloud keys passed to the backend[cite: 1]. Prompts, generated code, tool arguments and reasoning content are excluded from ordinary logs[cite: 1]. Debug capture is opt-in with retention and redaction controls[cite: 1].

Use a bounded metadata helper without GPU access and with memory/time/file-size limits[cite: 1]. Keep file identity stable between validation and launch; recheck digest when content changes, reject symlink escape from managed asset paths, and treat mutation during validation as failure[cite: 1]. The actual inference process needs GPU permissions and therefore remains a distinct risk boundary[cite: 1]. No remote code execution options are required for loading approved GGUFs[cite: 1].

Cupola does not authorize or execute shell commands generated by a model[cite: 1]. The external coding agent owns repository permissions, sandboxing, network controls, execution budgets and context compaction[cite: 1]. Repository instructions may guide the agent within the user's authority; arbitrary repository content cannot override that authority[cite: 1]. Cloud fallback is off unless explicitly configured because it changes the data destination and cost[cite: 1].

## 4. Extensions and conditions for adding them

| Extension | Minimum prerequisite | Deliberate scope |
| --- | --- | --- |
| Stencil | A real model-template compatibility failure reproduced[cite: 1] | Download selected files at resolved revisions using the Hub API; stage changes, diff, test and atomically promote or roll back[cite: 1]. No automatic merges into active templates[cite: 1]. [S6][cite: 1] |
| Local Compass | Stable profile plus repeatable task harness[cite: 1] | Compare local candidates on identical tasks and budgets; retain raw outcomes[cite: 1]. External leaderboards are discovery metadata only[cite: 1]. |
| Sentinel | Accessible sensors and known device mapping[cite: 1] | Read-only temperature/power/utilization history; configurable sustained alerts with hysteresis[cite: 1]. No lifetime estimate or automatic servicing recommendation[cite: 1]. |
| Custom proxy | Verified client/lifecycle gap upstream cannot meet[cite: 1] | Preserve streaming, tool IDs, usage, errors and cancellation; no content rewriting by default[cite: 1]. |
| Speculation | Stable non-speculative baseline[cite: 1] | Exact draft/target compatibility, peak memory, acceptance and task-quality comparison; adopt only if end-to-end time improves[cite: 1]. |
| Cache checkpoints | A measured expensive-prefill use case[cite: 1] | Bind to model, tokenizer, template, backend, context, cache type and ownership; reject incompatible state; sensitive file handling[cite: 1]. |
| iGPU embeddings | RAG need and CPU baseline[cite: 1] | Detect actual hardware, compare latency and shared-resource contention, retain CPU fallback[cite: 1]. |
| Additional backend or OS | MVP qualification complete[cite: 1] | New compatibility adapter and qualification record; no inferred support[cite: 1]. |

## 5. Evaluation and release decision

Track successful task completion first[cite: 1]. Secondary measures: wall time per successful task, manual recovery time, p50/p95 first-token latency, prefill and decode rates reported separately, peak device memory, startup failure rate, cancellation outcome and retained backend processes[cite: 1].

Compare the pinned raw-backend baseline and Cupola on the same machine/profile and frozen set of representative repository tasks[cite: 1]. Proposed initial pilot: 20 tasks × 3 runs each, spanning bug fixes, a small feature, refactoring and test repair[cite: 1]. Reset the repository between runs, predeclare completion criteria, include failures in denominators, and record cold/warm state[cite: 1]. This is a useful pilot, not proof of general benchmark parity[cite: 1]. Capture human intervention and task-level dispersion; do not present 60 correlated runs as 60 independent task types[cite: 1].

Economic reporting, if needed: cloud spend per successful task and local electricity plus an explicitly chosen hardware-cost allocation per successful task[cite: 1]. Show assumptions and failed attempts[cite: 1]. Compare quality separately[cite: 1]. No composite “parity achieved” label[cite: 1].

Proposed MVP gates: no orphaned owned backend in 50 ordinary start/stop cycles; port-conflict and stale-PID tests never signal unrelated processes; all declared protocol fixtures pass; overflow fails explicitly; restart behavior is bounded; no secrets or prompt text in normal logs[cite: 1]. Complete representative real-GPU inference, cancellation and resource-pressure checks[cite: 1]. Passing a finite test does not establish zero future failures[cite: 1].

A task-quality regression blocks an optimization even if token speed improves[cite: 1]. No timer or adoption claim is a measured result until recorded on the actual machine[cite: 1].

## 6. Agentic build sequence

Use GLM through ZCode, Codex and Claude as interchangeable development contributors with a shared specification[cite: 1]. Proposed allocation is a workflow choice, not a claim that one model is inherently better[cite: 1]. ZCode is an agentic development environment; using it to build Cupola does not establish that it can use Cupola as its inference provider[cite: 1]. [S7][cite: 1]

| Ticket | Work and boundary | Dependency | Acceptance evidence | Suggested executor |
| --- | --- | --- | --- | --- |
| T00 | Inspect repository if present; collect read-only hardware/backend facts; resolve one actual model and license[cite: 1]. No driver installs or privileged changes[cite: 1]. | None[cite: 1] | capability report; exact missing prerequisites; no fabricated hashes[cite: 1] | Codex[cite: 1] |
| T01 | Direct llama-server spike using one pinned build and model[cite: 1]. No Cupola abstractions yet[cite: 1]. | T00[cite: 1] | text, stream and multi-turn tool trace; context/memory evidence; exact client compatibility[cite: 1] | Codex[cite: 1] |
| T02 | Minimal package, configuration schema, atomic state and plan/doctor interfaces[cite: 1]. | T01[cite: 1] | malformed configuration and missing-sensor tests; reproducible install[cite: 1] | GLM/ZCode[cite: 1] |
| T03 | Ownership, lock, state machine, launch/readiness/stop with injectable fake backend[cite: 1]. | T02[cite: 1] | simultaneous launch, early exit, timeout, port collision, stale PID, ignored TERM[cite: 1] | Codex[cite: 1] |
| T04 | Bounded GGUF validation and conservative memory/profile qualification[cite: 1]. | T03[cite: 1] | malformed fixture rejection; unknown architecture path; cleanup and bounded OOM recovery on real GPU[cite: 1] | Codex[cite: 1] |
| T05 | One real client acceptance journey and private logging[cite: 1]. | T04[cite: 1] | task → tool call → tool result → completion; disconnect/cancel/overflow; no prompt logging[cite: 1] | GLM/ZCode[cite: 1] |
| T06 | Adversarial code review and repair of concrete findings[cite: 1]. | T05[cite: 1] | reproducible bug cases with severity, patches and targeted regression evidence[cite: 1] | Claude reviews; original executor repairs[cite: 1] |
| T07 | Baseline comparison, install/uninstall, operational guide and release gate[cite: 1]. | T06[cite: 1] | hardware qualification artifact and honest pass/fail decision[cite: 1] | Codex integrates; Claude reviews evidence[cite: 1] |

Do not issue all tickets to a single unconstrained “build production” prompt[cite: 1]. Start T00 and T01, then revise estimates from evidence[cite: 1]. If raw inference cannot complete the client round trip, fix or change that dependency before building supervisory layers[cite: 1]. If raw-backend operation already meets the need with negligible interruption, prefer a thin configuration/diagnostics utility and stop broader platform work[cite: 1].

One writer per branch/worktree[cite: 1]. A second tool reviews a fixed diff and reports findings before editing[cite: 1]. Share interfaces and tests through the repository; do not let separate chat transcripts become competing specifications[cite: 1]. Do not run all three agents on every change[cite: 1]. Use independent review for lifecycle, memory and security boundaries; use the selected executor for routine implementation[cite: 1].

Recommended repository destinations, to create during implementation: docs/prd.md (this contract), docs/capabilities.md (observations), docs/decisions.md (material decisions), src/cupola/ (control plane), tests/unit/, tests/integration/, tests/hardware/, profiles/, and a lockfile[cite: 1]. Add repository agent instructions only after inspecting existing instructions; preserve them[cite: 1]. This document is not an installed skill[cite: 1].

### Copy-paste kickoff prompt

```text
Implement CUPOLA using the attached revised PRD as the product contract.
Start only with T00 and T01. Inspect the existing repository and its applicable
instructions first. Do not overwrite user work or existing agent instructions.

The objective is a reproducible raw llama-server coding round trip on the actual
machine before we implement a supervisor. Discover installed OS/kernel, GPU PCI
identity, permissions, backend version/help and relevant dependencies read-only.
Resolve one real model artifact with provenance, license and exact revision.
Do not invent model names, CLI flags, speed measurements or compatibility.

Use currently installed tools when adequate. Record missing prerequisites.
Do not install drivers, change GPU clocks, enable lingering, download large
weights or use paid APIs unless the current session authorizes that action.
Complete all available read-only and reversible work before reporting a blocker.

If hardware is unavailable, produce the capability report and a precise hardware
runbook; label hardware acceptance NOT RUN. Do not substitute mocked output for
measurements. Propose the smallest next ticket supported by the spike.

Return changed files, checks run and their actual results, unresolved failures,
and the next executable action. Do not claim production readiness.