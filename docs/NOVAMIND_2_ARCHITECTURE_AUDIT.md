# NovaMind 2.0 — repository architecture audit

Audit baseline: `58f55fe`. The audit covered the Flask application, all routes,
AgenticSeek fork, provider layer, registries, MCP transports, Skills, memory,
terminal/filesystem sandbox, streaming UI, authentication/identity, SQLite
schema, tests, workflows and deployment files.

## Before

The product already had substantial working capabilities, but authority was
split:

- `AgentEngine` selected an AgenticSeek role and assembled request-scoped tool
  adapter objects.
- `Agent.run_tool()` executed those objects directly.
- `tool_registry.py` separately described built-ins and exposed
  `execute_unified()`, but Agent Core used it only for a discovery event.
- provider normalization in `ai_providers.py` supported native `tool_calls`,
  while `NovaProvider` discarded that capability and used text blocks only.
- lifecycle events were additive flat dictionaries without stable ids,
  timestamps, payload envelopes, replay storage, or complete browser handling.
- verification equated a non-empty answer with success.
- `agent_runs` persisted summaries and tool steps, but not ownership, event
  history or verification evidence.
- MCP and Skill stores were process-global. This was safe only while their
  management was administrator-only; `ADMIN_ACCESS_FOR_ALL_USERS` made strict
  per-user ownership necessary.

### Duplicate/conflicting mechanisms

1. AgenticSeek adapter execution versus `ToolSpec`/`execute_unified` execution.
2. Native provider tool-call normalization versus text block execution.
3. Legacy `plan` events, planner-agent plans and a generic lifecycle plan.
4. `tasks` (user checklist) versus `agent_runs` (autonomous execution record).
   They have different purposes and are retained, but are now documented as
   separate concepts.
5. Built-in web calls in forced search versus web adapter calls in the ReAct
   loop. Forced search remains for the explicit Search UI mode; it now emits the
   same lifecycle evidence.

### Unused/incomplete components

- `execute_unified()` and `execute_parallel()` were tested but not the actual
  Agent Core execution authority.
- Native tool calling was implemented below the adapter but not connected to
  AgenticSeek.
- persisted `plan` had no authoritative task-specific plan in the unified path.
- MCP health failures did not remove stale tools from active discovery.
- browser ignored several required lifecycle event types.

### Security findings

- MCP records had no owner and a non-admin with delegated settings access could
  observe/use another user's connection.
- Skills were shared globally under delegated settings access.
- agent-run detail/list queries had no owner predicate.
- STDIO environment inheritance had already been hardened at baseline; command
  allow-list, `shell=False`, endpoint origin checks and mode-0600 persistence
  are preserved.
- external output was already wrapped as untrusted and workspace/capability
  checks were strong; these are preserved.

## Preserve

- Flask routes and NDJSON transport, chat/history/auth/Google login.
- provider catalog, free-model enforcement and strict single-provider selection (no automatic failover).
- AgenticSeek prompt/adapter compatibility and existing planner/coding roles.
- terminal/filesystem sandbox and confirmation policy.
- memory, knowledge, media, scheduler, Skills creator and MCP UI.
- SQLite and backward-compatible lazy migrations.

## Refactor/consolidate

- `tool_registry.py` becomes the execution authority for request-scoped legacy
  adapters and structured built-in/MCP calls.
- `NovaProvider` carries normalized native tool calls into the same Agent loop.
- planning, event protocol and evidence verification move to small `core/`
  services used by the existing engine (not a second engine).
- MCP, Skills and autonomous run records become owner-scoped.
- frontend lifecycle handling and interrupted-stream recovery use persisted runs.

## Replace/remove

No user-facing capability is removed. The generic decorative lifecycle plan is
replaced by a capability-aware task-specific plan. Direct adapter execution in
NovaMind runs is replaced by `RuntimeToolRegistry`; standalone vendored
AgenticSeek retains a compatibility fallback. Non-empty-answer verification is
replaced by evidence-based outcome classification.

## After

`routes/agent.py` constructs one request and identity context. `AgentEngine`
collects context, discovers the live authoritative catalog, creates a
criteria-based plan, and runs one stateful ReAct loop. Native calls and text
compatibility calls both reach request-scoped `RuntimeToolRegistry`; structured
calls reach the same module's `execute_unified`. Results become bounded
observations in `RunState`, are sent back to the model, persisted as events, and
used by `core.verification` to classify the outcome. The frontend renders and
can recover that protocol while legacy events remain accepted.

## Remaining intentional boundaries

- Python cannot forcibly kill an arbitrary in-process thread after timeout;
  subprocess-backed terminal and STDIO tools retain their own hard process
  timeouts. Registry timeout returns control and cancellation is cooperative for
  in-process callbacks.
- recovery of a mutating call is never automatic unless the transport can prove
  it is safe/idempotent. The model receives the error and must revise its plan.
- old ownerless MCP records are treated as administrator-owned for compatibility.
