# Cupola Architectural Decisions

## ADR-001: Control Plane Stack
- **Status:** Accepted
- **Context:** Cupola requires an atomic, low-overhead lifecycle supervisor for local inference without adding heavy orchestrator bloat.
- **Decision:** Use standard-library Python 3.11+ (`asyncio`, `subprocess`, `struct` for bounded GGUF parsing), TOML manifests, and `pytest`. No native C/C++ GPU extensions in the control plane.

## ADR-002: Direct Loopback Client Routing
- **Status:** Accepted
- **Context:** An intermediary proxy introduces streaming, cancellation, and tool-call schema translation hazards.
- **Decision:** In MVP, client agents connect directly to `llama-server` on `127.0.0.1:<port>`. Cupola acts strictly as an out-of-band lifecycle and admission supervisor.

## ADR-003: Strict Memory Admission Budget
- **Status:** Accepted
- **Context:** Out-of-memory crashes on Navi 31 / ROCm drop the listening socket and disrupt agent sessions.
- **Decision:** Enforce `weights + KV + recurrent + compute_buffers + reserve <= available_VRAM`. Reject CPU offload in strict mode; fail explicitly on context overflow rather than silently dropping prompt or terminal history.
