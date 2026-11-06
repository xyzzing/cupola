# Agent Memory & Self-Learning OSS: survey + interface lessons

Metadata from `api.github.com`, 2026-09-26; last active = `pushed_at` year.

## A. Memory platforms

| Project | ★ / lic / lang / last active | Does + how you integrate it | Data model | R/W · MCP |
|---|---|---|---|---|
| [Mem0](https://github.com/mem0ai/mem0) | 66.0k / Apache-2.0 / Py / 2026 | `Memory.from_config({...})` + `add()`/`search()`; self-host compose | Fact + metadata + embedding + entities; SQLite `~/.mem0/history.db` = source of truth, vector store = semantic | RW · OpenMemory MCP (docker, SSE `:8765`); `mem0-mcp` archived |
| [Letta](https://github.com/letta-ai/letta-code) ([pointer repo](https://github.com/letta-ai/letta), 24.9k Apache-2.0) | 3.4k / lic UNCERTAIN / TS / 2026 | Agents edit their own memory; "dreaming" consolidates lessons. `npm i -g @letta-ai/letta-code`, `letta server` | Blocks `{label,description,value,limit}` in prompt (no retrieval); archival passages; git-backed MemFS | RW · MCP **client only** |
| [Zep](https://github.com/getzep/zep) | 4.9k / Apache-2.0 / Py / 2026 | Managed memory; repo now examples + SDKs (`zep-cloud`); legacy CE deprecated | Threads + user graph + facts over Graphiti (internals UNCERTAIN) | RW · **hosted MCP** `https://api.getzep.com/mcp`, OAuth |
| [Graphiti](https://github.com/getzep/graphiti) | 31.2k / Apache-2.0 / Py / 2026 | `pip install graphiti-core[...]` + Neo4j/FalkorDB/Kuzu; bundled `mcp_server` | Episodes (provenance) → entity nodes + fact edges with `valid_at`/`invalid_at`, `group_id`, Pydantic ontology | RW · self-host MCP `:8000/mcp/`; `add_memory`, `search_memory_facts/nodes` |
| [cognee](https://github.com/topoteretes/cognee) | 31.0k / Apache-2.0 / Py / 2026 | ECL pipelines build graph+vector memory; `pip install cognee`, `cognee-mcp` Docker/uvx | Graph + vector + relational; default SQLite + LanceDB + embedded Kuzu, or one Postgres | RW · MCP `remember`/`recall`/`forget`; trimmed `tools/list` |
| [Supermemory](https://github.com/supermemoryai/supermemory) | 30.9k / MIT / TS / 2026 | Hosted MCP or SDK `add()`, `profile({containerTag,q})` | Memories + `profile.static`/`dynamic`, scoped by `containerTag` | RW · **hosted MCP** `https://mcp.supermemory.ai/mcp`; `memory`/`recall`/`context` |
| [Memobase](https://github.com/memodb-io/memobase) | 2.9k / Apache-2.0 / Py / 2026-01 | SDK + running backend (project URL + key); `insert(ChatBlob)`, `flush()`, `context()` | Users → blobs → async topic/subtopic profiles | RW · in-repo `src/mcp` stdio (`MEMOBASE_API_KEY`) |
| [Memary](https://github.com/kingjulio8238/Memary) | 2.7k / MIT / Py / 2024 (stale) | `pip install memary`, `.env` Neo4j creds, `ChatAgent` | Neo4j graph + memory stream; ≤2-hop subgraph retrieval | RW · none |
| [txtai](https://github.com/neuml/txtai) | 13.0k / Apache-2.0 / Py / 2026 | Embeddings DB (vector + graph + SQL), YAML app config | Rows/edges/columns built by the developer | Retrieval-only · apps over MCP, not a memory schema |
| [LlamaIndex Memory](https://github.com/run-llama/llama_index) | 52.3k / MIT / Py / 2026 | `Memory.from_defaults(session_id, memory_blocks=[...])` | Short-term + `Static`/`FactExtraction`/`Vector` blocks with `priority` truncation; SQLite default, any DB URI | RW (fact block) · MCP as tool server (UNCERTAIN) |
| [MCP reference memory](https://github.com/modelcontextprotocol/servers/blob/main/src/memory/README.md) | 90.6k monorepo / MIT / TS / 2026 | `npx -y @modelcontextprotocol/server-memory` | Entities `{name,entityType,observations[]}` + relations `{from,to,relationType}`; JSONL, `MEMORY_FILE_PATH`; 9 tools + a graph resource | RW · **is** the MCP server |
| [OpenHands condenser](https://github.com/OpenHands/OpenHands) | 89.2k / MIT / TS / 2026 | `CondenserBase.condense()`; `LLMSummarizing`/`Rolling`/`NoOp`/`Pipeline` | Event log + `Condensation{forgotten_event_ids}`; keeps first 4 + last events, LLM-summarizes the middle past 120 | RW within run · MCP client |

### Research lineage

| Project | ★ / lic / last active | Integration · memory model | Loop |
|---|---|---|---|
| [Voyager](https://github.com/MineDojo/Voyager) | 7.2k / MIT / 2024 | `SkillManager(library_dir)` · Chroma `skill_vectordb` of executable JS | Writes skills |
| [Generative Agents](https://github.com/joonspk-research/generative_agents) | 22.1k / Apache-2.0 / 2024 | Sim · memory-stream JSON `{description,keywords,poignancy,created}`; recency decay + importance + embedding relevance | Writes reflections |
| [Reflexion](https://github.com/noahshinn/reflexion) | 3.3k / MIT / 2025 | `use_memory` flag · self-reflections in context | RW |
| [Self-Refine](https://github.com/madaan/self-refine) | 822 / Apache-2.0 / 2024 | **No persistent memory** | Read-only |
| [ExpeL](https://github.com/LeapLabTHU/ExpeL) | 244 / Apache-2.0 / 2024 | `train.py` · trajectory pool + LLM-extracted insights | RW |
| [A-MEM](https://github.com/agiresearch/A-mem) | 1.2k / MIT / 2025 | Zettelkasten notes in ChromaDB; agent links/evolves notes | RW |
| [AWM](https://github.com/zorazrw/agent-workflow-memory) | 474 / Apache-2.0 / 2025 | `pipeline.py` · induced workflows injected into context | RW |
| "minder"-style | [agent-minder](https://github.com/aptx-health/agent-minder) 2★; [Reflexio](https://github.com/ReflexioAI/reflexio) 374★ Apache-2.0 2026 | minder = Go job dispatcher (`.agent-minder/jobs.yaml`); Reflexio = `reflexio services start`, SQLite | Corrections → persisted lessons (RW) |

## Integration answers

1. **MCP memory servers:** canonical MCP memory, Mem0 OpenMemory, Zep (hosted), Graphiti (self-hosted), cognee, Supermemory (hosted), Memobase (stdio). **Client-only:** Letta, OpenHands. **None:** Memary, txtai, LlamaIndex, research repos, minder.
2. **Single-file declarative config:** MCP `mcp.json` (one entry); Graphiti `config.yaml` + compose; cognee `.env` + compose; Letta `.af` (agent + memory in one portable file). **Code at startup:** mem0, Graphiti/cognee/memary Python APIs, LlamaIndex `Memory.from_defaults`, research repos.
3. **Record shape:** no standard; converged fields are `{id, text/fact, scope, type/tags, timestamp, embedding, provenance}`. Variants: mem0 fact + metadata + scope ids; Graphiti fact + validity window + episode provenance; MCP entity/observations + relation (no time, no embedding); Letta block `{label,description,value,limit}`.
4. **Read-only vs read-write:** read-only/stateless = Self-Refine, txtai-as-used, OpenHands condensers (per run). RW self-learning = mem0, Letta, Zep, Graphiti, cognee, Supermemory, Memobase, Memary, A-MEM, ExpeL, AWM, Reflexion, Voyager, GenAgents, LlamaIndex fact block. **Benchmark flag:** these persist agent-written state and several use LLM extraction (nondeterministic) — letting the agent write measures something different unless the store is reset per run and extraction pinned.
5. **Interchange standard:** none for memory content. De facto: MCP tools as transport, with the reference server's entity/relation/observation JSONL as the closest shape. Adjacent standards: **Agent Plugins 1.0.0** (`plugin.json` + `skills/` + `mcp.json`) packages MCP servers declaratively; **Letta `.af`** serializes an agent *including* memory blocks and `mcp_servers`.

## Interface lessons

- Default to `retrieve(query, scope, top_k)` + optional `record(...)` — nearly every project converges there (mem0, cognee, supermemory, MCP, memobase).
- Make **scope a first-class argument** (`user_id`/`run_id`, `containerTag`, `group_id`, `session_id`) — free run isolation.
- Config as data: `{provider, config}` is the shared idiom (mem0 `from_config`, graphiti/cognee config files, MCP `mcp.json`).
- Expose `readonly: true` and a write-disable path (mem0 `infer=False`, Letta read-only blocks) — the key knob for reproducibility.
- Make storage injectable (`MEMORY_FILE_PATH`, `MEM0_DIR`, `async_database_uri`, `NEO4J_URI`) so each run gets a throwaway store; require `source` + `created_at` (Graphiti is the model; the MCP reference server has neither).

## Shortlist — interfaces worth copying

1. **Canonical MCP memory server** — one-config-line attach, tiny stable tool surface, injectable JSONL path.
2. **mem0** — `{provider, config}` + scoped `add/search` and an explicit write-inference switch.
3. **Graphiti** — `config.yaml` + compose + `/mcp/`, plus the best record schema (episodes, validity windows).
4. **Letta `.af` AgentFile** — one portable file for agent + memory + MCP servers; ideal for snapshot/restore.
5. **Zep Memory plugin** — hosted MCP + Agent Plugins 1.0.0 packaging as a declarative bundle.

## Verified vs uncertain

**Verified:** all metadata above; mem0 config/stores/defaults; Letta blocks, `.af` and MCP-client role; Zep examples-only repo + hosted MCP + plugin packaging; Graphiti MCP config/tools and graph model; cognee MCP modes; Supermemory MCP URL and SDK; Memobase SDK + MCP; LlamaIndex Memory blocks; OpenHands condenser defaults; the MCP reference server schema; research-repo storage as cited.

**Uncertain:** Letta `letta-code` license/language; Zep thread/summary internals; whether LlamaIndex ships a *memory* MCP server; which "minder" was meant; Supermemory's vendor benchmark claims; unadopted "memory protocol" preprints.
