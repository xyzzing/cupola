# Compaction & Graph-Memory OSS — Interface Lessons for Benchmark Plugin Wiring

Sources: GitHub API metadata + raw README/docs, fetched 2026-09-26. Unverified claims are marked **uncertain**.

## A. Context compaction / context engineering

| Project | Repo · activity · license · lang | What it does | Integration surface | Data model |
|---|---|---|---|---|
| OpenHands condensers | `OpenHands/OpenHands` (ex `All-Hands-AI/OpenHands`) 89k★ TS MIT; code in `OpenHands/software-agent-sdk` 1.2k★ Python MIT, 2026-09-26 | Replaces first half of event log with one summary; `NoOp`/`LLMSummarizing`(`max_size`,`max_tokens`,`keep_first`)/`Pipeline`/`Rolling` | **SDK** `Agent(condenser=...)`, but also *data*: `CondenserSettings(enabled=True, max_size=50)` inside `OpenHandsAgentSettings` → `model_dump(mode="json")` | Append-only event log + `Condensation` tombstone events; `View` applies them at read time |
| Claude Code / Anthropic | docs `code.claude.com/docs/en/context-window`; harness repo **uncertain** (public `anthropics/claude-code` looks issues/docs-only) | `/compact [focus]`, `/autocompact 500k`, `/rewind`→Summarize here/up-to-here, per-model thresholds | Slash commands + `settings.json`; no plugin API to replace the compactor | Messages + summary; cache stats report "expected rebuild (compaction or tool-result clearing)" |
| LangMem / LangGraph | `langchain-ai/langmem` 1.7k★ MIT | `summarize_messages(...)`; `SummarizationNode(token_counter, model, max_tokens, max_tokens_before_summary, max_summary_tokens)` | **Code-level graph node**; needs `StateGraph` + checkpointer | `RunningSummary` in a separate state key; full history kept in `messages` |
| LlamaIndex memory blocks | `run-llama/llama_index` 52k★ MIT | `BaseMemoryBlock` (`_aget`/`_aput`/`atruncate`) rendered into the system message | `Memory.from_defaults(session_id, token_limit, async_database_uri)` — declarative, still Python | SQLite default; pluggable `AsyncDBChatStore`; per-block token budgets |
| Letta / MemGPT | `letta-ai/letta` V1 archived (Apache-2.0, 25k★); active `letta-ai/letta-code` | Recursive / partial-evict summarization | **Best config story**: `SummarizerSettings(BaseSettings)`, `env_prefix="letta_summarizer_"`, plus YAML via `letta.config_file` | Buffer + summary; `mode`, `message_buffer_limit=60`, `partial_evict_summarizer_percentage=0.30` |
| repomix | `yamadashy/repomix` 28k★ MIT TS | Packs a repo into one file | **MCP stdio**: `npx -y repomix --mcp`; `--sandbox [dir]` confines paths | File tree → packed text |
| claude-context | `zilliztech/claude-context` 12.6k★ MIT TS (2026-07) | Semantic code search | **MCP**: `npx @zilliz/claude-context-mcp` | Chunks in Milvus/Zilliz |
| context7 | `upstash/context7` 62k★ MIT TS | Version-specific library docs | **Two modes**: HTTP MCP `https://mcp.context7.com/mcp` (Bearer) **or** `ctx7` CLI + skill, *no MCP required* | Doc index keyed by library id |
| **kompact** (proxy) | `npow/kompact` Python | HTTP proxy compressing context; 8 transforms incl. schema optimizer, TOON/JSON compressors, observation masker, **cache aligner** | `kompact proxy --port 7878` + `export ANTHROPIC_BASE_URL=http://localhost:7878` — **zero code change**; per-request `X-Kompact-Disable: toon,code` | Stateless request/response transform |
| LMCache | `LMCache/LMCache` 12k★ Apache-2.0 | Persistent/reusable KV cache | vLLM connector: in-process `LMCacheConnectorV1` or standalone `LMCacheMPConnector` (sidecar) | KV blocks across CPU/disk tiers |
| GPTCache | `zilliztech/GPTCache` 8.2k★ MIT | Semantic LLM response cache | `pip install gptcache`; LangChain integration; Docker server | Embedding index + scalar stores |

*Thin evidence:* ccundo (CLI over Claude Code session JSONL); headroom, TokenTamer (compression proxies, README only).

## B. Knowledge graphs / graph memory

| Project | Repo · activity · license · lang | Integration surface | Data model |
|---|---|---|---|
| **Graphiti** | `getzep/graphiti` 31k★ Apache-2.0, 2026-09-26 | **Three surfaces**: SDK (`pip install graphiti-core`), FastAPI `server/`, **MCP** (`mcp_server/`; HTTP `/mcp/` or stdio). `config.yaml` → env → CLI args precedence | Episodes (provenance) → entity nodes with evolving summaries; fact edges with **bi-temporal validity windows**; communities |
| Zep | managed, proprietary; Py/TS/Go SDKs | REST/SDK only | Same model, hosted |
| cognee | `topoteretes/cognee` 31k★ Apache-2.0 Python | REST 8000 / UI 3000 / **MCP 8001** via `docker compose --profile ui --profile mcp up`; Python·TS·Rust SDKs; Claude Code + OpenClaw plugins | Graph + vector + relational; runs on one Postgres |
| Neo4j GraphRAG | `neo4j/neo4j-graphrag-python` 1.3k★ license `NOASSERTION` | **Library only** (`pip install neo4j-graphrag`); retrievers Vector/Hybrid/Text2Cypher. Separate official `mcp-neo4j` — **uncertain, not fetched** | Cypher property graph |
| microsoft/graphrag | `microsoft/graphrag` 36k★ MIT | **`settings.yml`/`settings.json` + `.env`**, `graphrag init --root P --force`, `${ENV_VAR}` substitution, LiteLLM underneath | Entities/relationships/claims, Leiden communities, community reports (Parquet) |
| LightRAG | `HKUDS/LightRAG` 40k★ MIT | **`lightrag-server` + `.env`** (`env.example`, wizard `make env-base`), REST, or SDK; tools `lightrag-rebuild-vdb`, `lightrag-clean-llmqc` | **Four** stores: KV, vector, graph, doc-status — in-memory + `WORKING_DIR` by default; Neo4j/Postgres/Milvus optional |
| Nano-GraphRAG | `gusye1234/nano-graphrag` 4k★ MIT | Library only | NetworkX + vector store |
| RAPTOR | `parthsarthi03/raptor` MIT | Library only (`RetrievalAugmentation`) | Hierarchical summary tree |
| Aider repo map | `Aider-AI/aider` 49k★ Apache-2.0 | **In-agent library**: `RepoMap(map_tokens=1024, refresh="auto")` | Symbol graph + tags cache `.aider.tags.cache.v{N}` |
| graft (local, npm) | builds `graft/` | **CLI + MCP tools** (`graft_find_code`, `graft_trace_calls`, `graft_file_api`, `graft_repo_map`) | Per-file markdown cards + `graph.json`; call edges only in the graph |

## Integration answers

**1 — Attachable with only config or a proxy.** Proxies/env vars: **kompact**, headroom, TokenTamer, any OpenAI-compatible gateway, LMCache MP (engine flag). Config-file-only, given an MCP client: **Graphiti, cognee, repomix, claude-context, context7, graft** (community LightRAG MCP wrappers — *uncertain*). Native config-only compaction exists only *inside* a host (`/autocompact`, Letta YAML). **No general OSS project compacts an arbitrary agent by config alone** — that gap is what a proxy fills.

**2 — MCP servers (verified).** Graphiti, cognee (8001), repomix, claude-context, context7 (HTTP), graft. *Uncertain:* third-party LightRAG MCP packages, `mcp-neo4j`.

**3 — Inside vs outside the agent loop.**

| Outside the loop (no fork) | Boundary | Inside (needs agent code) |
|---|---|---|
| kompact, headroom, TokenTamer | HTTP proxy agent↔model; fires on **every** request | OpenHands condensers, LangMem `SummarizationNode`, LlamaIndex `Memory`, aider `RepoMap`, RAPTOR |
| LMCache MP mode | inference-engine sidecar | — |
| graphiti, cognee, repomix, claude-context, context7, graft | **tool boundary** — fires only when the model *chooses* to call it | — |
| MS GraphRAG, LightRAG server | offline index + REST/MCP retrieval | — |

Ranking for a benchmark: **proxy > sidecar > MCP tool > in-process plugin**. A proxy compacts deterministically; an MCP tool fires opportunistically and is not a reproducible intervention.

**4 — Config shapes.**

```json
{"mcpServers": {"graphiti-memory": {"transport": "http", "url": "http://localhost:8000/mcp/"}}}
```
```toml
# Codex CLI — note the key is mcp_servers, not mcpServers
[mcp_servers.claude-context]
command = "npx"
args = ["@zilliz/claude-context-mcp@latest"]
env = { "OPENAI_API_KEY" = "...", "MILVUS_TOKEN" = "..." }
startup_timeout_ms = 20000
```
```yaml
# microsoft/graphrag settings.yml
completion_models:
  default_completion_model: {model_provider: openai, model: gpt-4.1, api_key: ${GRAPHRAG_API_KEY}}
```
Also Graphiti `config.yaml` (db/llm blocks, overridden by env then CLI flags), Letta `LETTA_SUMMARIZER_*`/YAML, LightRAG `.env`, OpenHands JSON `CondenserSettings`.

**5 — Reproducibility.**

| Risk | Where | Reset / snapshot |
|---|---|---|
| Cache returns a *similar* answer, not the same computation | GPTCache; LightRAG `ENABLE_LLM_CACHE` (on by default); Graphiti LLM cache | Clear store; `lightrag-clean-llmqc`; fresh `WORKING_DIR` |
| LLM extraction is non-deterministic **and concurrency-dependent** (graph shape varies per run) | Graphiti (`SEMAPHORE_LIMIT=10`), cognee, LightRAG | Graphiti MCP clear-graph/rebuild-indices tool; LightRAG: delete workspace or `lightrag-rebuild-vdb`; MS GraphRAG: `graphrag init --force` |
| Prompt/KV state leaks across runs | LMCache, Claude Code prompt cache, kompact cache aligner | LMCache flush API |
| Opaque external state | cognee Postgres, Neo4j/FalkorDB/Milvus containers | Drop the container volume — *uncertain* whether an in-band reset API exists |

Harness rule: **fresh volume + fresh working dir per run**, semantic caches off by default, fixed `temperature`, pinned concurrency.

## Ranked shortlist — wiring patterns worth copying

1. **kompact** — `ANTHROPIC_BASE_URL=http://localhost:7878` *is* the integration; a proxy plugin attaches to any agent with one env var.
2. **Graphiti** — one core, three surfaces (SDK / REST / MCP), plus `config.yaml` → env → CLI precedence a harness can override per run.
3. **OpenHands `CondenserSettings`** — the plugin is serializable data with a schema, so the chosen strategy lands in the run manifest.
4. **Letta `SummarizerSettings`** — `env_prefix`-scoped `BaseSettings`, every knob overridable from env or YAML: ideal for reproducibility pinning.
5. **repomix `--mcp`** — one command turns a CLI into a stdio MCP server; `--sandbox <dir>` gives a per-run confinement boundary.

## Verified vs uncertain

**Verified (primary sources):** repo URLs, licenses, languages, activity dates, star counts; OpenHands condenser classes + `CondenserSettings` + event-log/`Condensation`/`View` model; LangMem parameters; LlamaIndex `Memory.from_defaults` + `BaseMemoryBlock`; Letta `SummarizerSettings`; Graphiti MCP tools and transport configs; cognee compose profiles/ports; LightRAG `.env`, server, storage model; MS GraphRAG `settings.yml` + `graphrag init --force`; neo4j-graphrag retrievers; aider `RepoMap`; repomix/claude-context/context7/graft MCP wiring; kompact proxy flow.

**Uncertain:** whether `anthropics/claude-code` publishes harness source; semantics of Graphiti's clear-graph MCP tool; whether cognee and LightRAG expose in-band graph-reset APIs; existence/quality of official LightRAG and Neo4j MCP servers; headroom and TokenTamer config surfaces; hardware requirements not assessed.
