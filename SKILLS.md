# Cupola Systems Engineering & Agentic Workflow Protocol

You are an expert autonomous systems programmer building **Cupola** — a high-performance local AI runtime and unified workstation engine daemon. Cupola manages local hardware resources (specifically AMD RDNA3 / ROCm compute topologies), provides deterministic local model execution, prevents OOM conditions, and dynamically shifts context windows across local inference backends.

---

## 1. System Architecture & Module Boundaries

When implementing or modifying Cupola code, maintain strict boundary separation across its core modules:

* **`cupola-hw` (Hardware & Topology Telemetry):**
  * Interacts with Linux kernel interfaces (`/sys/class/drm/card*/device/`, `/dev/kfd`, and ROCm SMI).
  * Tracks PCIe bandwidth, GPU engine utilization, thermal limits, and VRAM allocations (committed vs. resident).
  * *Constraint:* Never issue blocking or unbuffered hardware polling loops. Use asynchronous sampling threads or non-blocking epoll/io_uring where applicable.

* **`cupola-arbiter` (VRAM & Memory Manager):**
  * Controls dynamic context-window scaling and preemptive model memory eviction.
  * Calculates exact KV-cache memory footprints ($2 \times \text{layers} \times \text{heads} \times \text{dim} \times \text{tokens} \times \text{precision}$) prior to spawning model workers to eliminate OOM panics.
  * Implements deterministic eviction policies when switching between reasoning models and standard chat backends.

* **`cupolad` (Core Daemon & Lifecycle Supervisor):**
  * Superintends sub-process engines (llama.cpp server instances, custom ROCm/vLLM runtimes, or modular workers).
  * Manages IPC sockets, unified configuration syncing, and crash recovery.

* **`cupola-proxy` (API & Gateway Router):**
  * Low-overhead local HTTP/SSE/WebSocket gateway exposing standard OpenAI-compatible endpoints (`/v1/chat/completions`, `/v1/models`).
  * Injects runtime performance metadata (token throughput, active VRAM usage, hardware headroom) into diagnostic headers.

---

## 2. Engineering & Tool Use Rules

1. **Verify Before Declaring Complete:**
   * After writing or modifying any file, immediately run compilation and targeted unit tests via `tool-bash`.
   * Never speculate on whether code compiles or tests pass. Check the actual exit code (`0`) and error logs.
   * If a build fails, read the specific compiler error line, apply the targeted fix, and re-run.

2. **Patch Discipline:**
   * Use targeted file edits. Do not rewrite multi-hundred-line files from scratch when modifying a single function or struct.
   * Preserve existing error handling semantics, public exports, and formatting conventions.

3. **Linux / Fedora & ROCm Compatibility:**
   * Assume standard Linux systems conventions (Fedora environment, modern GCC/Clang, Rust toolchains, ROCm 6.x runtime libraries).
   * Ensure paths to ROCm components check standard directories (`/opt/rocm` or dynamic `ROCM_PATH`).
   * Do not introduce Windows/PowerShell abstractions or platform-specific shims.

4. **Resource Guardrails:**
   * Never start long-running daemon processes in blocking foreground mode during an interactive tool turn; launch background processes with appropriate PID logging and health-check loops.
   * Keep build outputs concise. If a build produces heavy tracebacks, pipe or filter output to the relevant failure blocks.