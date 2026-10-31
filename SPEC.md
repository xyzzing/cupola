# SPEC.md

## 1. Specification Overview

`cupola` is an auditable, reproducible local inference supervisor for a single developer operating an AMD Radeon workstation under Linux (Ubuntu / Fedora) [cite: 3]. It manages an upstream `llama-server` process, providing deterministic process supervision, conservative pre-flight admission checks, and verifiable lifecycle transitions without running an in-path HTTP proxy [cite: 3].

---

## 2. Process Lifecycle & State Machine

The supervisor controls backend lifecycle transitions via a finite state machine (FSM) [cite: 3]. All mutations require holding an exclusive file lock (`cupola.lock`) located in `$XDG_RUNTIME_DIR/cupola/` [cite: 3].

```
                 ┌──────────────┐
                 │   STOPPED    │◄────────────────────────────────┐
                 └──────┬───────┘                                 │
                        │ cupola up <profile>                     │
                        ▼                                         │
                 ┌──────────────┐                                 │
                 │  VALIDATING  │───► [Validation Failure] ───────┤
                 └──────┬───────┘                                 │
                        │ preflight checks pass                   │
                        ▼                                         │
                 ┌──────────────┐                                 │
                 │   STARTING   │───► [Spawn / Ready Timeout] ────┤
                 └──────┬───────┘                                 │
                        │ backend `/health` OK                    │
                        ▼                                         │
                 ┌──────────────┐                                 │
                 │    READY     │───► cupola down ────────────────┤
                 └──────┬───────┘                                 │
                        │                                         │
                        │ child process crash / unhandled exit    │
                        ▼                                         │
                 ┌──────────────┐                                 │
                 │    FAILED    │─────────────────────────────────┘
                 └──────────────┘
```

### 2.1 State Definitions
* **`STOPPED`**: No owned backend process exists [cite: 3]. Lock is released.
* **`VALIDATING`**: Inspects profile manifests, hashes, paths, and bounded GGUF metadata; samples PCI device available memory [cite: 3]. Rejection transitions directly to `FAILED` or `STOPPED` [cite: 3].
* **`STARTING`**: Spawns `llama-server` directly on loopback with pinned arguments [cite: 3]. Records PID, process start-time (`/proc/[pid]/stat`), and an instance UUID [cite: 3].
* **`READY`**: Backend confirms HTTP 200 on `/health` [cite: 3]. Direct client access on `127.0.0.1:<port>` is unblocked [cite: 3].
* **`STOPPING`**: Initiates graceful shutdown via `SIGTERM` followed by cleanup verification [cite: 3].
* **`FAILED`**: Explicit error state retaining diagnostics, last exit code, and failure reason [cite: 3].
* **`DEGRADED`**: live service state indicating an unconfirmed backend or failed cleanup [cite: 3]. Blocks new launches until ownership is resolved [cite: 3].

### 2.2 Process Control Invariants
1. **Ownership Authority:** A PID alone is never authority to issue POSIX signals [cite: 3]. Process identification must verify `(PID, start_time, instance_uuid)` against the recorded launch descriptor before dispatching `SIGTERM` or `SIGKILL` [cite: 3].
2. **Port Conflict Protection:** If an assigned port is occupied during launch, `cupola` aborts with an actionable error [cite: 3]. It must never signal or terminate unrelated processes listening on that port [cite: 3].
3. **Graceful Shutdown Bounds:**
   * `SIGTERM` issued; poll exit for up to `stop_timeout_seconds` (default: 10s) [cite: 3].
   * If process remains active, issue `SIGKILL` and observe for `kill_timeout_seconds` (default: 5s) [cite: 3].
   * If driver resources or zombie tasks fail to clear, transition to `DEGRADED` [cite: 3].

---

## 3. Pre-Flight Memory Admission (Anvil)

Cupola rejects mathematical claims of "zero OOM guarantees" [cite: 3]. Memory planning acts as a conservative admission gate based on bounded metadata parsing and real-time sysfs telemetry [cite: 3].

### 3.1 Admission Formula

A launch plan is admitted under strict mode if and only if:

$$	ext{Memory}_{	ext{Required}} + 	ext{Reserve}_{	ext{Safety}} \le 	ext{VRAM}_{	ext{ObservedFree}}$$

Where:
* **$	ext{Reserve}_{	ext{Safety}}$**: Configurable safety buffer reserving memory for window compositing and system usage (default: $2048	ext{ MiB}$) [cite: 3].
* **$	ext{VRAM}_{	ext{ObservedFree}}$**: Free device memory read from `/sys/class/drm/card*/device/mem_info_vram_used` matched to the discrete GPU PCI ID [cite: 3].
* **$	ext{Memory}_{	ext{Required}}$** is defined as:

$$	ext{Memory}_{	ext{Required}} = M_{	ext{weights}} + M_{	ext{KV}} + M_{	ext{compute}}$$

### 3.2 Key-Value Cache Estimation

For standard multi-head / grouped-query attention architectures:

$$M_{	ext{KV}} pprox N_{	ext{seq}} 	imes \sum_{l=1}^{L} \left[ C_l 	imes (H_{K,l} 	imes D_{K,l} 	imes B_K + H_{V,l} 	imes D_{V,l} 	imes B_V) ight]$$

Where:
* $N_{	ext{seq}}$: Number of parallel sequences (default: 1 for coding MVP) [cite: 3].
* $C_l$: Allocated context tokens per layer [cite: 3].
* $H_{K}, H_{V}$: Number of Key/Value heads [cite: 3].
* $D$: Head dimension ($D_{	ext{model}} / H_{	ext{heads}}$) [cite: 3].
* $B_K, B_V$: Quantized byte size per cache entry (e.g., 1.0 for Q8_0, 2.0 for FP16) [cite: 3].

*Architectures with recurrent state or hybrid sliding windows are flagged as unsupported in strict mode until verified with backend logs [cite: 3].*

---

## 4. CLI Contract & Subcommands

Cupola exposes its interface via standard subcommands returning human-readable text or structured JSON (`--json`) [cite: 3]:

| Command | Signature | Description |
| :--- | :--- | :--- |
| `cupola doctor` | `cupola doctor [--json]` | Read-only report of PCI identity, ROCm/amdgpu sysfs nodes, OS version, and dependency versions [cite: 3]. |
| `cupola profile validate` | `cupola profile validate <profile>` | Validates TOML manifest, checks file paths, parses bounded GGUF metadata, and validates SHA256 hashes without loading weights [cite: 3]. |
| `cupola plan` | `cupola plan <profile> [--json]` | Computes requested vs. effective settings, KV cache size, and VRAM budget against live sysfs readings [cite: 3]. No process execution [cite: 3]. |
| `cupola up` | `cupola up <profile> [--foreground]` | Acquires lock, verifies admission, starts `llama-server` via direct subprocess, and polls `/health` until ready [cite: 3]. |
| `cupola status` | `cupola status [--json]` | Inspects instance state file, verifies backend PID existence, returns operational uptime, context length, and ports [cite: 3]. |
| `cupola down` | `cupola down` | Reaps owned backend via verified `(PID, start_time)` check and cleans temporary state; idempotent [cite: 3]. |

---

## 5. Security & Isolation Boundaries

1. **No In-Path Execution of Agent Tools:** Cupola does not evaluate, parse, or execute model-generated bash commands or tool calls [cite: 3]. Agent sandboxing, containerization, and tool confirmation belong strictly to the external coding agent [cite: 3].
2. **Loopback & Private Sockets:** Backend binds strictly to `127.0.0.1` [cite: 3]. No external network exposure by default [cite: 3].
3. **Bounded Metadata Reading:** GGUF parser reads tensor headers using bounded chunk streams, rejecting malformed structures, unapproved symlinks, and files outside whitelisted directories [cite: 3].
4. **Privacy:** Logs exclude prompts, generated code completions, and reasoning tags [cite: 3]. Only operational metrics, error traces, and backend diagnostic outputs are retained [cite: 3].
