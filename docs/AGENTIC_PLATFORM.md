# NovaMind Agentic Platform

NovaMind keeps the existing Flask, provider, AgenticSeek, terminal, Skills and
MCP components. The agentic layer composes them instead of creating parallel
implementations.

## Runtime loop

`routes/agent.py` binds identity, workspace, memory, capability token and model
provider. `AgentEngine` then runs:

1. task understanding and context collection;
2. live tool/Skill/MCP discovery;
3. adaptive plan;
4. ReAct execution (`action → tool result → observation → next action`);
5. loop/deadline/step protection and confirmation policy;
6. output verification and final response.

The model chooses subsequent actions from prior tool observations. Tool blocks
are not pre-scripted. Provider calls continue through `ai_providers.py`. Only
the explicitly selected free model is called: failed requests are returned to
the caller without switching providers or silently choosing another model.
Groq may rotate among its configured API keys while remaining on Groq.

## Unified tools

`tool_registry.unified_catalog()` rebuilds a normalized live catalog containing:

- built-in web, terminal, filesystem, Git and knowledge tools;
- all tools returned by connected MCP servers;
- enabled Skills and their declared tool requirements.

`execute_unified()` provides one execution interface. `execute_parallel()` runs
only independent safe calls concurrently and serializes writes or calls sharing
a conflict key. Existing AgenticSeek adapters remain the actual executors.

Skills are persisted by `skills_registry.py`. Only Skills relevant to the
current goal are loaded into temporary task context; long-term storage is not
mixed with another user's conversational memory.

## MCP

`mcp_registry.py` persists connection configuration and secrets in the existing
mode-0600 store. Supported transports:

- Streamable HTTP;
- legacy HTTP+SSE;
- STDIO (`MCP_STDIO_ENABLED=1`).

Every successful connection performs `initialize`,
`notifications/initialized`, and `tools/list`. A server with no usable tools is
not marked active. Calls use `tools/call`; disconnect stops runtime sessions but
keeps configuration, while delete removes configuration and credentials.

STDIO uses argv with `shell=False`, an executable allow-list
(`MCP_STDIO_COMMANDS`), a minimal environment that does not inherit NovaMind API
keys, per-call timeout, and process cleanup at shutdown.

## Streaming protocol

Legacy events remain unchanged. The stream additionally emits:

- `task_started`
- `task_analysis`
- `plan_created`
- `tool_discovered`
- `tool_selected`
- `tool_call_started`
- `tool_call_completed` / `tool_call_failed`
- `observation`
- `verification_started`
- `verification_completed`
- `task_completed`

The browser renders concise action summaries and expandable existing tool cards.
Hidden model reasoning/chain-of-thought is never included in lifecycle events.

## Persistence and stream recovery

SQLite is migrated lazily and backward-compatibly. `agent_runs` gains `owner`
and `verification`; `agent_run_events` stores bounded lifecycle payloads for
inspection and interrupted-stream recovery. Existing rows are retained. Legacy
ownerless rows remain visible only in administrator/maintenance contexts.

The stable lifecycle envelope contains `event_id`, `task_id`, `timestamp`,
`sequence`, `type`, and `payload`; legacy flat fields remain during migration.

## Deployment

No new runtime dependency or paid service is required. Existing local, Termux
and Render commands remain valid. Optional settings:

- `MCP_AUTO_RECONNECT` and `MCP_HEALTH_RETRY_SECONDS` control bounded recovery;
- `AGENT_TOOL_RESULT_CHARS` bounds normalized tool results;
- existing STDIO allow-list and retry variables remain unchanged.

## Safety and limits

Existing guardrails, request-scoped capabilities, workspace isolation,
confirmation cards, cancellation, timeouts, output clipping, repeat detection,
step limits and MCP search limits remain enforced. MCP output is treated as
untrusted tool data. Connecting a server does not bypass risk confirmation.
