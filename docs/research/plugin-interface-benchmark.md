# Plugin / extension interfaces for agent-wireable capabilities

"One step" = one artifact written + one command run, by either the repo owner or a cold-start agent.

## 1. Ecosystem matrix

| Ecosystem | Third party adds | Manifest / artifact | Declarative or code | Discovery | Version pinning | Failure surfacing |
|---|---|---|---|---|---|---|
| **MCP** | server process (stdio/HTTP/SSE) | `mcpServers` JSON in host config; `server.json` for registry | **declarative only** | host reads config; server declares `capabilities`; tools via `tools/list` | none in config (`npx -y pkg@ver` is whatever you write); registry pins `version` | JSON-RPC errors + `isError:true` results; per-server log files |
| **Claude Code plugins** | plugin dir + marketplace entry | `.claude-plugin/plugin.json`, `skills/<n>/SKILL.md`, `commands/`, `agents/`, `hooks/` | declarative manifest, code optional | directory convention + manifest overrides; marketplace | `version` pins the plugin; `dependencies` names other plugins | `claude plugin validate` (pass/warn/fail); unknown top-level keys **silently stripped** |
| **Agent Skills** | folder with `SKILL.md` | YAML frontmatter + Markdown | declarative | scan for `SKILL.md`; model selects by `description` | none (no version field) | validation rules only; no runtime health |
| **Codex / OpenAI** | TOML entry, or skill dir | `~/.codex/config.toml` `[mcp_servers.*]`; `$CODEX_HOME/skills/` | declarative | config parse; `skill-installer` skill | you write exact command/args | `codex mcp list/get`; issue #26810: stdio server shown as "Tools: none" |
| **OpenAI Agents SDK** | import a Python object | none | **code only** | explicit `tools=[...]` on the Agent | dependency lockfile | Python exceptions; configurable tool timeouts |
| **LangChain / LlamaIndex** | install a package | `pip install langchain-<x>` | code | manual import; LlamaHub `download_loader` deprecated | package lockfile | import errors |
| **VS Code** | package with `package.json` | `contributes` + `activationEvents` + `main` | **hybrid**: declare contributions, code implements | publisher scans manifest; declared commands auto-activate | `engines.vscode`; marketplace has no lock | extension-host isolation |
| **Obsidian** | folder + `manifest.json` | required `id`,`name`,`version`,`minAppVersion`,`description` | code (`main.js`) | folder scan; `id` must equal folder name | `version`/`minAppVersion` | in-app error notice; `isDesktopOnly` flag |
| **Home Assistant** | Python dir + `manifest.json` | `domain`, `requirements`, `dependencies`, discovery matchers | hybrid: manifest declares, `config_flow.py` implements | manifest + zeroconf/ssdp/dhcp/usb/mqtt matchers | `requirements: ["aiohue==1.9.1"]` (exact pins in manifest) | component load fails loudly; quality scale + diagnostics |
| **Neovim** | dir on `runtimepath` | none required; optional `lua/<p>/health.lua` | code (Lua) | `runtimepath`/`packpath` | plugin manager | **`:checkhealth`** explicit `ok/warn/error` |
| **pytest** | package + entry point | `[project.entry-points.pytest11]` | code (hooks) | **entry_points**, zero config; `PYTEST_DISABLE_PLUGIN_AUTOLOAD` escape hatch | pip lockfile | `--trace-config`, `-p no:name`; import failure aborts startup |
| **Django** | app package | `INSTALLED_APPS` + `apps.py:AppConfig` | code, explicit registry | literal setting; `ready()` registers | requirements file | `AppRegistryNotReady`/`LookupError` at startup |
| **Terraform** | provider binary | HCL `required_providers` constraint | declarative requirement | registry protocol | **best in class**: lockfile with exact version + constraints + hashes | checksum mismatch = hard error |
| **A2A** | remote agent | Agent Card at `/.well-known/agent-card.json` | declarative | well-known URI, curated registry, or direct config | HTTP caching/ETag; no pinning | discovery/HTTP errors only |

### Real minimal examples (verbatim)

MCP host config — the entire integration:
```json
{"mcpServers":{"filesystem":{"command":"npx",
  "args":["-y","@modelcontextprotocol/server-filesystem","/Users/username/Desktop"]}}}
```
MCP tool discovery (`tools/list` result):
```json
{"tools":[{"name":"get_weather","description":"Get current weather information for a location",
 "inputSchema":{"type":"object","properties":{"location":{"type":"string"}},"required":["location"]}}]}
```
Agent Skill — the whole agent-facing contract:
```yaml
---
name: skill-name
description: A description of what this skill does and when to use it.
---
```
Claude Code plugin: `name` is "the only required key"; defaults are `skills/`, `agents/`, `hooks/hooks.json`, `.mcp.json`; inline MCP:
```json
{"mcpServers":{"deploy-api":{"command":"node","args":["${CLAUDE_PLUGIN_ROOT}/server.js"]}}}
```
Codex: `[mcp_servers.my-server]` · `command = "npx"` · `args = [...]` in `~/.codex/config.toml`.
Home Assistant minimal: `{"domain":"your_domain_name","name":"Your Integration","codeowners":[],"dependencies":[],"documentation":"https://www.example.com","integration_type":"hub","iot_class":"cloud_polling","requirements":[]}`.
Python discovery (`importlib.metadata`):
```python
from importlib.metadata import entry_points
eps = entry_points(group="timmins.display")
display = eps[0].load()   # EntryPoint(name='excl', value='pkg:fn', group='timmins.display')
```
```toml
[project.entry-points.pytest11]
myproject = "myproject.pluginmodule"
```
Terraform lock:
```hcl
provider "registry.terraform.io/hashicorp/azurerm" {
  version = "2.30.0"; constraints = "~> 2.12"; hashes = ["h1:FJwsuowaG5CIdZ0WQyFZH9r6kIJeRKts9+GcRsTz1+Y="]
}
```
AGENTS.md: plain Markdown, no required fields, no schema; nearest file in the tree wins. ACP: JSON-RPC 2.0 between editor and agent; compatibility is negotiated via `protocolVersion` in `initialize`, not by artifact version.

## 2. Decision answers

**Q1 — Minimum viable manifest.** One file per capability. The only field an agent must *read* is `description`.

```toml
# .sinter/capabilities/qdrant-memory.toml
apiVersion  = "sinter/v1"
kind        = "memory"        # slot: memory|compactor|graph|skill|grader|service
id          = "qdrant-memory" # unique, namespaced, stable
version     = "1.4.2"         # exact pin; the resolver never guesses
description = "Vector memory store. Use for long-term recall across sessions."
provides    = ["memory.store", "memory.search"]  # declared, asserted at load
secrets     = ["QDRANT_URL"]  # prompts the user; never stored in the repo
requires    = { sinter = ">=0.3,<0.4", python = ">=3.11" }
[entry]                       # exactly one launch shape
type = "mcp"; transport = "stdio"; command = "uvx"; args = ["qdrant-mcp@1.4.2"]
[health]                      # doctor probe, not a doc convention
type = "mcp-tool"; name = "ping"; timeout_ms = 3000
```
Why each field: `kind` routes to a subsystem (HA `integration_type`); `id`+`version` give identity and pinning (Obsidian `id`/`version`); `description` is the discovery surface — SKILL.md frontmatter and A2A `AgentSkill.description` prove 1–2 sentences suffice for a model to select a capability; `provides` turns "loaded" into "claimed and checkable"; `entry` with an enum transport removes the stdio/HTTP ambiguity MCP's dialects suffer; `secrets` mirrors Claude Code `userConfig`; `requires`+`health` stop silent degradation.

**Q2 — Declarative vs code.** The boundary is *out-of-process vs in-process*. Addressable as a process or endpoint over an existing wire protocol (MCP, HTTP, CLI) → the manifest suffices; MCP proves it: five lines of JSON, no code, language-agnostic. Must run inside the host and mutate host state or control flow (compaction policy, VRAM admission, grading logic, lifecycle hooks) → code is unavoidable: pytest needs `pytest11` hook modules, Django needs `AppConfig.ready()`, VS Code needs `main`. Copy VS Code's hybrid: **declare everything the host needs to plan with (names, schemas, wiring points); implement only behavior.**

**Q3 — Discovery for both agents and installers.** All three, layered:
1. **Directory convention** `.sinter/capabilities/*.toml` — the only mechanism a cold-start agent can use in one write, with no install and no restart.
2. **Entry points** `[project.entry-points."sinter.capabilities"]` — the only mechanism where `pip install` *is* registration (pytest/VS Code model), zero config.
3. **Lock/registry file** `sinter.lock` — the only mechanism that makes a run reproducible.

Precedence: lock > entry points > directory. Directory is the agent path; entry points the package path; the lock records whichever won. That combination is most robust. A registry (MCP Registry, A2A curated registries) only pays off for third-party distribution — MCP itself says its registry is "not intended to be directly consumed by host applications".

**Q4 — Preventing silent degradation.** Observed failure modes: Claude Code **strips** unrecognized top-level keys and still loads; VS Code extensions can activate and register nothing; Codex lists a valid stdio server as "Tools: none". What works: (a) explicit `provides` asserted at load — a declared capability that will not resolve fails the load; (b) a first-class doctor — Neovim `:checkhealth` (`vim.health.ok/warn/error`), HA diagnostics, `sinter doctor`; (c) a strict validator in CI — `claude plugin validate --strict` turns warnings into failures; (d) a dry-run/admission step that prints the *resolved* capability set before the benchmark starts; (e) per-extension log files (`mcp-server-<NAME>.log`) so a no-op is attributable.

**Q5 — Reproducibility.** Terraform's `.terraform.lock.hcl` is the model: config declares a range (`constraints = "~> 2.12"`), the lock records the exact version plus hashes, re-selection needs an explicit `-upgrade`, and a checksum mismatch is a hard error ("doesn't match any of the checksums previously recorded"). pip/npm lockfiles pin transitive deps, but entry-point resolution then silently follows whatever is installed. VS Code is the cautionary case: `.vscode/extensions.json` holds recommendations with **no version field**, so extension sets are not reproducible by design. Design: human-authored manifests with semver ranges → generated lock with exact version + integrity digest (wheel hash, git commit, or OCI digest) → only `sinter up --upgrade` rewrites it.

## 3. Recommendation

- **Copy MCP for the wire.** Do not invent a tool schema: reuse `tools/list`, JSON Schema `inputSchema`, `capabilities.tools.listChanged`, `isError:true`. This is what lets a memory service, compactor, graph, grader, or skill provider attach as *processes* with no host code.
- **Copy Claude Code `plugin.json`+`userConfig` for packaging**: standard layout, path overrides, declared secret prompts, validate-strict in CI.
- **Copy SKILL.md for the agent-facing half**: one always-loaded `description`, body on trigger — the cheapest possible one-step for a model that has never seen the repo.
- **Copy pytest/VS Code entry points for installer-side registration; copy Terraform lock semantics for pinning.**
- **Copy Neovim `:checkhealth` + HA diagnostics for the doctor; copy HA `dependencies`/`after_dependencies`/`requirements` as the dependency precedent.**

Concrete design for this repo: `.sinter/capabilities/<id>.toml` (agent path) + `[project.entry-points."sinter.capabilities"]` (package path) + generated `sinter.lock` with precedence lock > entry points > directory, required `provides`/`health`, `sinter caps doctor`, and `--strict` CI validation that fails on unknown keys instead of stripping them.

## 4. Verified vs uncertain

**Verified (primary docs):** MCP `mcpServers`, `tools/list`, `isError`, registry `server.json`; Claude Code `plugin.json`, layout, `userConfig`, key-stripping, `validate`; Agent Skills frontmatter; Codex `[mcp_servers.*]`; VS Code `contributes`/`activationEvents`/`engines.vscode`; Obsidian required keys; HA manifest + discovery matchers; Neovim `:checkhealth`; pytest `pytest11` and discovery order; Django `AppConfig`; Terraform lock semantics and checksum error; `importlib.metadata.entry_points`; A2A Agent Card + well-known URI; ACP `protocolVersion`; AGENTS.md.

**Uncertain:** (a) LangChain/LlamaIndex entry-point plugin discovery — LangChain v1 docs no longer present a `langchain.tools` group and `download_loader` is deprecated in favour of plain packages; "LangChain has a plugin entry-point system" is **not verified**. (b) VS Code version pinning: `--install-extension publisher.name@version` exists but there is no supported lockfile. (c) Codex "Tools: none" is a tracked issue, not documented behavior. (d) The MCP Registry is explicitly in preview and may reset.
