# ARCHITECTURE.md

## 1. System Topology

Cupola is architected as an out-of-band supervisor rather than an in-path proxy [cite: 3]. Clients connect directly to the underlying `llama-server` on loopback [cite: 3].

```
┌────────────────────────────────────────────────────────┐
│             External Coding Agent Client               │
│          (Claude Code, Codex, ZCode, OpenClaw)         │
└───────────────────────────┬────────────────────────────┘
                            │ Direct HTTP / SSE (Loopback: 127.0.0.1:8080)
                            ▼
┌────────────────────────────────────────────────────────┐
│            Upstream Inference Backend                  │
│       (llama-server, pinned build, AMD ROCm)           │
└───────────────────────────▲────────────────────────────┘
                            │ Process Supervision / Signals / /health
┌───────────────────────────┴────────────────────────────┐
│                    cupola (CLI Core)                   │
│                                                        │
│  ┌────────────────────────┐   ┌─────────────────────┐  │
│  │     Crucible (FSM)     │   │     Anvil (GGUF)    │  │
│  │   Process Supervisor   │   │ Pre-Flight Planning │  │
│  └───────────┬────────────┘   └──────────┬──────────┘  │
│              │                           │             │
│  ┌───────────┴────────────┐   ┌──────────┴──────────┐  │
│  │    Lock & State Engine │   │   Hardware Telemetry│  │
│  │   Instance Verification│   │   (sysfs / hwmon)   │  │
│  └────────────────────────┘   └─────────────────────┘  │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│           Operating System & Hardware Interface        │
│    AMD Radeon RX 7900 XTX (amdgpu sysfs / DRM nodes)   │
└────────────────────────────────────────────────────────┘
```

---

## 2. Core Subsystems

### 2.1 Crucible (Process Supervisor)
* **Execution Boundary:** Crucible executes `llama-server` using Python's standard `subprocess` or `asyncio.subprocess` without shell wrappers (`shell=False`) [cite: 3].
* **Instance Tracking:** At launch, Crucible records an instance state record:
  ```json
  {
    "instance_uuid": "e5c7a6d8-912f-48b4-8263-228741369cf1",
    "pid": 48291,
    "start_time": 1726482910,
    "port": 8080,
    "profile": "qwen-32b-coder",
    "state": "READY"
  }
  ```
* **Safe Termination Protocol:** Before issuing signals, Crucible inspects `/proc/{pid}/stat` to verify that process start-time matches `start_time` [cite: 3]. If the PID has wrapped or belongs to another binary, the signal is aborted [cite: 3].

### 2.2 Anvil (Pre-Flight Planner & Bounded GGUF Parser)
* **Metadata Extraction:** Implements a streaming reader that decodes the GGUF header and key-value metadata arrays without reading model tensors into memory [cite: 3].
* **Resource Calibration:** Reads current VRAM allocation via AMD GPU sysfs paths:
  `/sys/class/drm/card{N}/device/mem_info_vram_used` [cite: 3].
* **Context Budgeting:** Computes the KV cache requirements based on target context length ($C$) and rejects startup if total requirements exceed available device memory minus the safety reserve [cite: 3].

### 2.3 Profile Engine
Profiles are explicitly versioned, immutable TOML documents [cite: 3]:

```toml
schema_version = "1.0"
alias = "qwen2.5-coder-32b"

[model]
path = "/var/lib/cupola/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf"
sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
context_length = 32768
ngl = 99

[backend]
binary_path = "/usr/local/bin/llama-server"
pinned_commit = "b3600"
reserve_vram_mib = 2048

[client]
allowed_routes = ["/v1/chat/completions", "/health"]
```

---

## 3. Directory Layout (XDG Standard Compliance)

Cupola adheres strictly to the XDG Base Directory specification [cite: 3]:

| Purpose | Environment Variable | Default Location |
| :--- | :--- | :--- |
| **Configuration** | `$XDG_CONFIG_HOME` | `~/.config/cupola/profiles/*.toml` [cite: 3] |
| **Runtime & Sockets** | `$XDG_RUNTIME_DIR` | `/run/user/$UID/cupola/cupola.lock`, `state.json` [cite: 3] |
| **State & Logs** | `$XDG_STATE_HOME` | `~/.local/state/cupola/logs/` [cite: 3] |
| **Model Weights** | User-defined / `$XDG_DATA_HOME` | `~/.local/share/cupola/models/` [cite: 3] |

---

## 4. Extension Gate Architecture

To prevent architectural regression, future capabilities are gated behind explicit operational prerequisites [cite: 3]:

* **Stencil (Template Parity):** Added only when an upstream Jinja template parsing failure is reproduced in a real test harness [cite: 3].
* **Compass (Local Benchmarking):** Added only after baseline inference stability is proven across 20 representative repository tasks [cite: 3].
* **Sentinel (Telemetry):** Added for passive temperature/power tracking without automatic diagnostic servicing claims [cite: 3].
