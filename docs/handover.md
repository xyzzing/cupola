# CUPOLA — Handover Document

**Date:** 2026-09-16
**Completed:** T00 (capability report), T01 (raw backend spike), T02 (minimal package, config schema, atomic state, plan/doctor interfaces), T03 (ownership, lock, state machine, launch/readiness/stop), T04 (bounded GGUF validation and memory/profile qualification), T05 (private operational logging and acceptance tests)
**Next:** T06 (adversarial code review and repair)

## 1. What Has Been Done

### T00 — Hardware/Backend Capability Report
- Inspected repository: only `docs/prd.md` and `SKILLS.md` exist
- Collected verified hardware facts (see `docs/capabilities.md` Section 1-4)
- Identified active llama-server build: v257 (c49ebdb), ROCmFPX, bound to 0.0.0.0:8080
- Identified active model: [model name redacted].gguf (15.3 GB, IQ4_XS)
- Corrected known environment facts: ROCm is 7.1.1 (not 6.x), ROCM_PATH is stale
- Established baseline capability report

### T01 — Raw Backend Spike
- Ran 10 API tests against the running llama-server
- All tests passed: single-turn, streaming, multi-turn, tool calling, cancellation, error handling
- Latency: prefill ~912 tok/s, decode ~24.5 tok/s
- VRAM: 20.5 GB baseline, +0.7 GB for 5K token context
- Server remained healthy throughout all tests
- API is OpenAI-compatible

### T02 — Minimal Package & Interfaces
- Created Python package structure (`src/cupola/`)
- Configuration schema (TOML) with ProfileSpec and CupolaConfig dataclasses
- Atomic state management (write-temp-then-rename)
- CLI: `doctor`, `validate`, `plan`, `up`, `down`, `status` (all support `--json`)
- Hardware probing (kernel, CPU, RAM, GPU, VRAM, ROCm, llama-server)
- Editable install via hatchling (`pip install -e .`)

### T03 — Ownership, Lock, State Machine, Lifecycle
- Process ownership via (PID, start_time, instance_uuid) tuple
- Exclusive file lock via O_CREAT|O_EXCL on $XDG_RUNTIME_DIR/cupola/cupola.lock
- Finite state machine: STOPPED→VALIDATING→STARTING→READY→STOPPED/FAILED/DEGRADED
- Supervisor (Crucible) with launch/readiness polling/SIGTERM→SIGKILL/DEGRADED escalation
- XDG Base Directory spec compliance

### T04 — Bounded GGUF Validation & Memory Admission
- Bounded GGUF header parser (`src/cupola/gguf.py`) — never loads tensor data
- VRAM telemetry via sysfs (`/sys/class/drm/card*/device/mem_info_vram_*`)
- KV cache estimation from actual model architecture parameters
- Memory admission formula: `weights + KV + compute + reserve ≤ available_VRAM`
- `cupola plan` now shows GGUF metadata and admission decision
- Profile config with correct backend path

### T05 — Private Operational Logging & Acceptance Tests
- `src/cupola/logging.py`: OperationalLogger — JSON lines, 600 perms, operational events only
- Supervisor integrated with logging (launch_start, launch_ready, launch_failed_validation, stop_starting, stop_sigterm, stop_completed, etc.)
- `tests/integration/test_acceptance.py`: full client journey test + logging verification
- `cupola plan` uses llama-server API for model info when server is running
- 43 tests passing, ruff clean, agentic verification passing

## 2. Current State

### Package Structure
```
src/cupola/
├── __init__.py
├── cli.py            # CLI entry point
├── config.py         # ProfileSpec, CupolaConfig, TOML loading
├── hardware.py       # Hardware probing
├── state.py          # Atomic state management
├── lock.py           # Exclusive file lock
├── supervisor.py     # Crucible — process supervisor
├── gguf.py           # Bounded GGUF parser
├── memory.py         # VRAM telemetry & admission
└── logging.py        # Private operational logging
```

### Running Services
- llama-server v257 on 0.0.0.0:8080 (PID varies)
- Model: [model name redacted].gguf
- Config: 63 GPU layers, ctx=131072, KV cache q4_0, flash attn on

### Known Limitations
- F04: VRAM monitoring is device-wide
- F14: MTP speculative decoding unverified
- F26: Model provenance/license unverified
- PR #16391: Prompt cache disabled (SIGABRT on ROCm)
- TOP_K: ROCm lacks GPU-side op (runs on CPU)
- GGUF parser has limited support for non-standard value type encodings

## 3. What Needs to Be Done Next

### T06 — Adversarial Code Review and Repair
**Scope:**
- Thorough code review of all modules
- Fix any security, correctness, or robustness issues
- Edge case testing
- Error handling review
- Concurrency review

**Dependencies:** T05 is complete

**Acceptance evidence:**
- All issues found are fixed or documented
- Tests cover the fixed issues
- Agentic verification passes

### T07 — Baseline Comparison, Install/Uninstall, Operational Guide
**Scope:**
- Baseline performance comparison vs current setup
- Install/uninstall instructions
- Operational guide for daily use

## 4. Verification Commands

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run agentic verification
python3 .agentic/verify.py

# Run lint
python3 -m ruff check src/ tests/

# CLI commands
python3 -m cupola.cli doctor --json
python3 -m cupola.cli validate coding
python3 -m cupola.cli plan coding
python3 -m cupola.cli status
```

## 5. Key Environment Facts

| Fact | Value |
|---|---|
| OS | Fedora 44, kernel 7.2.5-200.fc44.x86_64 |
| CPU | AMD Ryzen 9 7900X (12c/24t) |
| RAM | 96 GB DDR5 |
| GPU | AMD Radeon RX 7900 XT/XTX (Navi 31), 24 GB VRAM |
| ROCm | 7.1.1 |
| llama-server | v257 (c49ebdb), ROCmFPX build |
| Model | [model name redacted].gguf |
| Server URL | http://0.0.0.0:8080 |
| Backend binary | [local path redacted] |

## 6. Handover Notes

This is a **local inference supervisor** project for a single developer using an AMD workstation. The goal is to make starting/recovering local coding sessions more reliable than the current pinned llama-server command.

The raw backend is proven to work (T01). The supervisor layer is complete through T05 (config, state, lock, supervisor, GGUF validation, memory admission, logging). The next step is adversarial code review (T06).

Key design principles from the PRD:
- Single user, single machine, single GPU
- Python 3.11+ control plane (stdlib only)
- Foreground mode first, optional systemd service later
- No custom proxy or intent router for MVP
- Explicit profile selection (no silent model swapping)
- Atomic writes, private control socket under XDG_RUNTIME_DIR
- No shell command strings in model metadata