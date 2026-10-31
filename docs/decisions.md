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

## ADR-005: Engine Abstraction at the Spawn Boundary
- **Status:** Proposed
- **Context:** The engine boundary is currently llama-server-specific in three places: `backend.py` (version detection and ROCm feature probes), `discovery.find_llama_server()` (hardcoded binary lookup), and `supervisor.py` (builds the spawn command and owns the process). The deployment base is AMD ROCm only. The agent-facing contracts (`contracts.py`: `RuntimeSnapshot`, `InferenceIntent`, `InferenceGrant`) are already engine-agnostic, and ADR-002 keeps cupola out of the request path. Serving NVIDIA or Apple Silicon hosts requires a second engine without touching the FSM or the admission math.
- **Decision:** Keep `contracts.py` as the only engine-agnostic surface agents see, and introduce a minimal backend interface at exactly two seams: command construction (the spawn argument list in `supervisor.py`) and capability probing (`backend.detect_features`). A backend is a module that maps a `ProfileSpec` to a spawn command and answers the feature probe; the FSM, admission (`check_admission`, `check_weights_pin`), and telemetry stay backend-blind. llama-server/ROCm remains the only certified backend; a second backend lands behind a profile field naming its module, with its own admission numbers measured before first certify - no per-backend defaults invented. Nothing in this ADR changes the request path (ADR-002 holds).
- **Consequences:** `backend.py` and `discovery.py` refactor into the llama-server backend module; profiles gain one optional field; multi-GPU and engine-specific flags stay out of scope until a second backend actually exists. Numbered 005 to follow ADR-004 (Prometheus endpoint), which is on local main pending push.
