# Product Requirements Document (PRD)

## Scope-Aligned Architecture: Agentic Document Ingestion & Deterministic Task Planning

**Document Version:** 7.3.0

**Target Hardware:** Single 24GB VRAM GPU (AMD Radeon RX 7900 XTX / NVIDIA RTX 3090/4090)

**Host Environment:** Linux (Fedora / Ubuntu)

**Stack Components:** Cupola Supervisor Core (Substrate), DeepSeek Harness / DSH (Agent Orchestration), Minder (Cognitive Governor), Local 27B LLM (Reasoning Engine)

---

## 1. System Vision & The Scope-Aligned Contract

This architecture defines how a local 27B parameter model ingests complex technical documents (codebases, PRDs, financial models, architecture specs), analyzes their structural requirements, and deterministically compiles an executable, dependency-gated **Task DAG (`plan.json`)**.

To maintain stability on a 24GB GPU and prevent scope bloat, the architecture enforces a strict three-tier separation of concerns:

```
+----------------------------------------------------------------------------------------------------+
|                                    COGNITIVE CONTROL TIER (DSH)                                    |
|                                                                                                    |
|  - Document Ingestion & Chunking (Hierarchical AST & Structural Headings)                          |
|  - Topological Plan Compilation (plan.json Directed Acyclic Graph)                                |
|  - Workspace Checkpointing (ephemeral Git refs/cupola/checkpoints/*)                               |
+----------------------------------------------------------------------------------------------------+
                                 |                                   ^
    Dynamic Request Modulation   |                                   |  Reads Telemetry & State
    (Prompt + Thinking Budgets)  |                                   |  (instance.json / 1Hz)
                                 v                                   |
+-------------------------------------------------+                 |
|            MINDER (COGNITIVE REGULATOR)         |                 |
|                                                 |                 |
|  - Consequence-Aware Mode: DIRECT / LEAN / DEEP |                 |
|  - Context & KV-Cache Headroom Enforcer         |                 |
|  - Thermal Backpressure Damper                  |                 |
+-------------------------------------------------+                 |
          |                                                         |
          | Direct HTTP Token Stream (Loopback 127.0.0.1:8080/v1)   |
          v                                                         |
+-----------------------------------+             +--------------------------------------------------+
|         INFERENCE BACKEND         |             |             CUPOLA SUPERVISOR CORE               |
|                                   |             |                                                  |
|  - llama-server / vLLM (27B 4-bit)| <--- pidfd  | - Anvil: Physical VRAM & GTT Anti-Spill Guard    |
|  - Context window: 16k tokens     |   SIGSTOP   | - Compass: 1Hz hwmon Poller & Thermal Odometer   |
|  - Static Weights: ~14.5 GB       |   SIGCONT   | - Crucible: (PID, start_time, UUID) Authority    |
|  - KV-Cache Allocation: ~5.5 GB   |             | - Shield: cupola exec --sandbox (Resource Jail)  |
+-----------------------------------+             +--------------------------------------------------+

```

### The Boundary Matrix

| System Domain | Subsystem Owner | Exact Responsibility | Explicit Non-Goals |
| --- | --- | --- | --- |
| **Substrate & Hardware** | **Cupola** | Process lifecycle (`pidfd`), VRAM/GTT memory admission, `hwmon` thermal tracking, and isolated shell execution (`cupola exec`). | Never touches prompt tokens, never parses JSON schemas, never manages Git branches or task tickets. |
| **Agent & Workflow** | **DSH** | File ingestion, document decomposition, LLM prompting loops, Git tree rollback checkpoints, and `plan.json` maintenance. | Never interfaces directly with Linux `sysfs` or GPU hardware registers. |
| **Cognitive Policy** | **Minder** | Intercepts DSH requests; computes thinking token budgets (`DIRECT`, `LEAN`, `DEEP`) based on task risk and Cupola’s thermal telemetry. | Never executes shell commands or alters workspace files directly. |

---

## 2. Hardware Budget & Context Allocation (24GB Boundary)

Deploying a 27B parameter model (e.g., Qwen-2.5-27B) on a single 24GB frame while processing dense technical documentation requires strict memory fencing:

```
Total Physical VRAM: 24,576 MB (100%)
+------------------------------------------------------------------------------------+
|  Base Weights (4-bit AWQ / EXL2 / GGUF Q4_K_M)               : 14,848 MB (60.4%)   |
|  Paged KV-Cache (16,384 Context Window, FP8 Quantized)       :  5,632 MB (22.9%)   |
|  ROCm / CUDA Driver Buffers & Activation Overhead            :  1,536 MB  (6.2%)   |
|  Cupola Anvil Hard Safety Margin (Anti-GTT Spill Buffer)     :  2,560 MB (10.4%)   |
+------------------------------------------------------------------------------------+

```

### Context Budgeting Invariant

The active context window must not exceed **16,384 tokens**. Reading a large document (e.g., a 100-page specification or 10,000 lines of code) by loading the raw text into a single context prompt causes GPU out-of-memory errors or triggers GTT spillover across the PCIe bus, dropping inference speed from 45 t/s to under 2 t/s.

DSH must parse documents hierarchically rather than naively stuffing raw text into the prompt.

---

## 3. End-to-End Workflow: Ingest $\rightarrow$ Plan $\rightarrow$ Compile $\rightarrow$ Gate

```
  +-----------------------------------------------------------------------------------------+
  | STEP 1: DOCUMENT INGESTION & STRUCTURAL ANCHORING (DSH Client)                          |
  | - Reads target document(s) from workspace.                                              |
  | - Extracts Table of Contents, Section Headers, Code Blocks, and Interface Signatures.   |
  | - Compiles document_manifest.json with SHA-256 hashes and token budgets.               |
  +-----------------------------------------------------------------------------------------+
                                              |
                                              v
  +-----------------------------------------------------------------------------------------+
  | STEP 2: COGNITIVE MODULATION & PLANNING PROMPT (Minder Interceptor)                     |
  | - Checks Cupola instance.json: Verifies VRAM headroom > 2.5GB and Hotspot < 85°C.       |
  | - Selects Mode: DEEP (High-consequence architecture synthesis).                         |
  | - Injects strict JSON-Schema grammar constraints for DAG construction.                  |
  +-----------------------------------------------------------------------------------------+
                                              |
                                              v
  +-----------------------------------------------------------------------------------------+
  | STEP 3: REASONING & TOPOLOGICAL COMPILATION (27B Model)                                 |
  | - Model generates internal <think> trace: Identifies dependencies and critical paths.   |
  | - Emits raw plan.json defining discrete, leaf-level implementation tasks.                |
  +-----------------------------------------------------------------------------------------+
                                              |
                                              v
  +-----------------------------------------------------------------------------------------+
  | STEP 4: DETERMINISTIC PLAN VALIDATION (DSH Core)                                        |
  | - Validates JSON Schema conformance.                                                    |
  | - Verifies DAG Acyclicity via Kahn's Algorithm (Rejects circular dependencies).         |
  | - Confirms every task has a concrete verification target (test file or AST spec).       |
  +-----------------------------------------------------------------------------------------+
                                              |
                                              v
  +-----------------------------------------------------------------------------------------+
  | STEP 5: WORKSPACE CHECKPOINTING & DISPATCH LOCK (DSH + Cupola)                          |
  | - DSH creates baseline Git ref: refs/cupola/checkpoints/plan_v1_baseline.               |
  | - plan.json committed to $WORKSPACE/.cupola/plan.json.                                  |
  | - Ready for iterative execution via cupola exec --sandbox.                              |
  +-----------------------------------------------------------------------------------------+

```

---

## 4. Subsystem Detailed Specifications

### 4.1 Document Ingestion & Chunking Engine (DSH)

To preserve the 16k context window, DSH processes input documentation through an out-of-band pre-processor before calling the 27B model:

1. **Document Structural Parsing:**
* Markdown/Text: Parsed into a hierarchical tree based on `#`, `##`, `###` headers.
* Source Code / API Docs: Parsed into Abstract Syntax Trees (`ast.parse` for Python, `tree-sitter` for C/Rust/JS) extracting class, method, and function signatures while stripping implementations.
* Spreadsheets / Financial Models: Extracted as schema headers, named formula ranges, and balance sheet validation rows.


2. **Anchor Indexing (`document_manifest.json`):**
DSH creates an index of content blocks. Each block is assigned an anchor ID, byte offset, and summary signature.
3. **Context-Window Packaging:**
The prompt sent to the 27B model for planning receives:
* The complete structural outline (Table of Contents + Signatures).
* High-priority requirement sections (e.g., "Objectives", "Covenants", "Data Structures").
* Strict instruction to reference anchor IDs rather than re-quoting raw text.



### 4.2 Minder Cognitive Policy for Planning & Ingestion

Minder dynamically configures sampling flags and thinking token allocations based on task consequence and real-time Cupola hardware telemetry:

```
                                  INCOMING DSH REQUEST
                                            |
                                            v
                         +-------------------------------------+
                         | Read Cupola instance.json Telemetry |
                         +-------------------------------------+
                                            |
                    -------------------------------------------------
                   |                                                 |
                   v                                                 v
       [ Thermal Pacing Active ]                         [ Nominal Health State ]
       Hotspot > 88°C or VRAM < 1.5GB                    Hotspot <= 88°C and VRAM >= 1.5GB
                   |                                                 |
                   v                                                 v
        Clamp Mode to LEAN/DIRECT                        Evaluate Task Consequence
        Max Thinking Tokens: 1,024                                   |
                                                -------------------------------------------
                                               |                                           |
                                               v                                           v
                                      [ Document Ingestion ]                     [ Plan DAG Synthesis ]
                                      Low/Medium Consequence                     High Consequence
                                               |                                           |
                                               v                                           v
                                          Mode: LEAN                                  Mode: DEEP
                                   Thinking Tokens: 512–1,024                  Thinking Tokens: 4,096
                                   Temp: 0.2 | Top_P: 0.8                      Temp: 0.6 | Top_P: 0.95

```

#### Thinking Modes for Planning Workflows

* **`DIRECT` (0 thinking tokens):** Used for single-file metadata extraction, keyword indexing, and schema validation. Fast, low power, zero thermal strain.
* **`LEAN` (512–1,024 thinking tokens):** Used for document section summarization and individual task definition updates.
* **`DEEP` (4,096 thinking tokens):** Reserved exclusively for **Full DAG Plan Synthesis**. The 27B model runs deep causal chains to discover hidden dependencies, race conditions, and integration test requirements.

#### Thermal Backpressure Override

If Cupola's `instance.json` reports `telemetry.pacing_active == true` ($T_{\text{junction}} > 88^\circ\text{C}$):

* Minder downscales `DEEP` requests to `LEAN`.
* Minder inserts an explicit `time.sleep(1.0)` pre-dispatch delay, allowing the GPU cooling system to stabilize junction temperatures without halting the supervisor.

---

## 5. Formal Data Contracts & Schemas

### 5.1 `document_manifest.json` (DSH Document Map)

Stored at `$WORKSPACE/.cupola/document_manifest.json`:

```json
{
  "manifest_version": "1.0.0",
  "source_files": [
    {
      "path": "docs/prd_renewable_waterfall.md",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "total_tokens_est": 4200,
      "sections": [
        {
          "anchor_id": "sec_01_objectives",
          "title": "System Objectives & Return Hurdle",
          "token_count": 350,
          "summary": "Defines Project IRR target of 8.5% and DSCR covenant threshold of 1.20x."
        },
        {
          "anchor_id": "sec_02_waterfall_math",
          "title": "Cash Distribution Waterfall",
          "token_count": 1200,
          "summary": "Mandates sequential debt service priority before sponsor equity dividend distribution."
        }
      ]
    }
  ]
}

```

### 5.2 `plan.json` (Deterministic Task Graph)

Stored at `$WORKSPACE/.cupola/plan.json`. This is the single source of truth for all autonomous coding tasks:

```json
{
  "plan_version": "1.0.0",
  "project_name": "Renewables Waterfall Engine",
  "document_baseline_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "created_at": 1726831000,
  "tasks": [
    {
      "id": "task_001",
      "title": "Define PPA and Waterfall Data Models",
      "phase": "SCHEMA",
      "status": "COMPLETED",
      "source_anchors": ["sec_01_objectives"],
      "dependencies": [],
      "target_files": ["models/waterfall.py"],
      "verification": {
        "type": "AST_AND_TEST",
        "command": "pytest tests/test_waterfall_models.py"
      },
      "checkpoint_ref": "refs/cupola/checkpoints/task_001_success"
    },
    {
      "id": "task_002",
      "title": "Implement DSCR Minimum Covenant Solver",
      "phase": "CORE_MATH",
      "status": "PENDING",
      "source_anchors": ["sec_01_objectives", "sec_02_waterfall_math"],
      "dependencies": ["task_001"],
      "target_files": ["engine/dscr.py"],
      "verification": {
        "type": "AST_AND_TEST",
        "command": "pytest tests/test_dscr.py"
      },
      "checkpoint_ref": null
    },
    {
      "id": "task_003",
      "title": "Wire Cash Flow Priority Cascade",
      "phase": "INTEGRATION",
      "status": "BLOCKED",
      "source_anchors": ["sec_02_waterfall_math"],
      "dependencies": ["task_002"],
      "target_files": ["engine/cascade.py"],
      "verification": {
        "type": "AST_AND_TEST",
        "command": "pytest tests/test_cascade.py"
      },
      "checkpoint_ref": null
    }
  ]
}

```

---

## 6. Cupola Substrate Primitives for Planning & Verification

Cupola provides two out-of-band primitives to support this workflow without taking on task planning responsibilities.

### 6.1 `cupola exec --sandbox` (Execution Primitive)

DSH uses `cupola exec` to validate code generated during task execution inside an isolated environment.

```bash
cupola exec --sandbox --timeout 15 --max-memory-mb 2048 -- pytest tests/test_dscr.py

```

#### Sandboxing Mechanics (Zero External Dependencies)

* **Resource Fencing:** Implemented via standard library `resource.setrlimit`:
* `RLIMIT_AS`: Limits maximum virtual memory to 2,048 MB to prevent memory-exhaustion exploits.
* `RLIMIT_CPU`: Limits CPU runtime to 15 seconds to kill runaway loops.


* **Process Group Isolation:** Spawns via `subprocess.Popen` with `preexec_fn=os.setsid`. On timeout or error, Cupola terminates the entire process group cleanly via `os.killpg(p.pid, signal.SIGKILL)`.
* **Environment Scrubbing:** Drops environment variables matching `*TOKEN*`, `*KEY*`, `*SECRET*`, or `SSH_*`. Passes only `PATH`, `LANG`, and `VIRTUAL_ENV`.

### 6.2 Atomic Telemetry Broadcast (`instance.json`)

Cupola continuously refreshes `$XDG_STATE_HOME/cupola/instance.json` at 1 Hz via an atomic rename operation (`.tmp` $\rightarrow$ `instance.json`).

```python
# Minimal Cupola Atomic State Updater
def update_state_atomic(state_data: dict, target_path: Path):
    temp_path = target_path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(state_data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp_path.replace(target_path)

```

---

## 7. Operational Edge Cases & Mitigation Protocols

| Edge Case | Root Cause | Detection Point | Automated System Mitigation |
| --- | --- | --- | --- |
| **Circular Plan Dependencies** | Model hallucinates mutual requirements ($A \rightarrow B \rightarrow A$). | DSH Plan Validator during Kahn's topological sort. | DSH rejects plan, isolates the cycle nodes, and calls 27B model in `LEAN` mode with targeted error: `"Cycle detected between task_002 and task_003. Restructure dependencies."` |
| **Document Context Exhaustion** | Target documentation exceeds the 16k context window. | DSH Document Ingestor during token estimation. | Pre-processor strips non-structural prose, summarizes code bodies to interface stubs, and builds an anchor map to split the document across multiple ingestion passes. |
| **GTT Memory Spillover** | Dynamic KV-cache expansion pushes total allocations past physical VRAM. | Cupola Anvil polling `/sys/class/drm/card*/device/mem_info_gtt_used`. | Anvil flags `gtt_spill_detected: true` in `instance.json`. Minder immediately clamps the 27B model's `max_tokens` ceiling and enforces prompt truncation. |
| **Thermal Saturation During Planning** | Long `DEEP` reasoning chain heats silicon past $88^\circ\text{C}$. | Cupola Compass reading `hwmon` junction sensor. | Compass sets `pacing_active: true`. Minder down-regulates subsequent turns to `LEAN` mode (1k token budget) and inserts pacing sleeps between turns. |
| **Syntax-Breaking Code Output** | Model outputs incomplete or invalid Python syntax during task execution. | DSH AST Parser (`ast.parse`). | Catches `SyntaxError` before executing tests. Cupola triggers a hard Git checkout revert to the task's starting checkpoint, returning line-level tracebacks to the model. |

---

## 8. Verification & Acceptance Criteria

1. **Deterministic Plan Compilation:** Given the same technical document and a fixed seed, DSH and the 27B model must compile an identical task DAG structure with zero cyclic dependencies.
2. **Context Ceiling Enforcement:** At no point during document ingestion, planning, or code verification may context size exceed 16,384 tokens, maintaining a minimum of 2.5 GB of free physical VRAM at all times.
3. **Zero GTT Memory Thrashing:** `/sys/class/drm/card*/device/mem_info_gtt_used` must read `0 bytes` throughout long-running planning and generation sessions.
4. **Clean Worktree Isolation:** 100% of failed verification runs must cleanly roll back workspace modifications to the pre-task Git ref without leaving orphan files or lingering background processes.
5. **Standard-Library Compliance:** The Cupola Supervisor Core and its CLI execution utilities must run on Python 3.10+ without installing external dependencies via `pip`.
