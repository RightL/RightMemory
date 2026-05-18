# CLI Agent Mode Design

## Goal

Replace RightMemory's current spawned-subagent install mode with a CLI-agent
mode that keeps the `rightmemory` command as the stable interface while using
Codex CLI or Claude Code CLI as the role executor.

The design keeps two clear runtime choices:

- `standalone`: RightMemory's current Pydantic AI runtime with custom tools.
- `cli-agent`: RightMemory CLI orchestration with Codex or Claude Code handling
  role execution inside the memory root.

The old `subagent` install mode is replaced by `cli-agent`. It should not be
silently mapped to the new behavior; the installer should reject it with a clear
message that names `cli-agent`.

## Verified CLI Session Behavior

The mode depends on non-interactive CLI sessions being resumable. This was
checked with a hidden-token continuity test.

For Codex, a first `codex exec --json` call returned a `thread_id` and replied
with a token that appeared in the first prompt. A second `codex exec resume
--json <thread_id>` call was asked what token appeared in the previous user
message, without repeating the token. The resumed call returned the token.

For Claude Code, a first `claude -p --output-format json --session-id <uuid>`
call returned the supplied session id and replied with a token from the first
prompt. Reusing `--session-id` directly reported that the session was already in
use, while `claude -p --output-format json --resume <uuid>` resumed the session
and returned the prior token.

This is functional evidence that both providers can preserve conversation
history across non-interactive invocations when RightMemory stores and reuses the
provider session identifier.

## Installed Shape

`cli-agent` mode installs the runtime and a CLI-calling `memory-orchestrator`
skill. It does not install `memory-curator`, `memory-dreamer`, or a retriever
role skill.

The orchestrator talks to memory through the existing command surface:

```bash
rightmemory retrieve --session <agent-session-id> "<memory need>"
rightmemory update submit --session <agent-session-id> "<candidate brief>"
rightmemory update pull --session <agent-session-id>
rightmemory dreamer --session <dream-session-id> "run a dream cycle"
rightmemory watch start
```

All roles can use the CLI-agent executor:

- `retrieve`
- `update`
- `dreamer`
- `reviewer`
- `sync-reconciler`

The Python CLI remains responsible for role selection, sessions, locks,
watchers, async update batching, review scanning, sync preflight, sync repair,
debug traces, and caller-facing stdout.

## Config Shape

User-facing config should stay small. Provider and model are the normal knobs:

```toml
[agent_cli]
provider = "codex"

[retrieve.agent_cli]
model = "gpt-5.4-mini"

[update.agent_cli]
provider = "claude"
model = "sonnet"
```

`[agent_cli].provider` is the default provider for every role.
`[<role>.agent_cli].provider` overrides that default for one role.
`[<role>.agent_cli].model` maps to the provider's model flag when present.

RightMemory supplies the practical defaults for working directory, output
format, session resume, and role-appropriate execution boundaries. The config
does not ask normal users to choose sandbox or permission flags. If a provider
is missing, unauthenticated, or not resumable, the command fails clearly instead
of switching to another execution path.

Existing `[<role>.model]` tables continue to configure the standalone Pydantic
AI runtime.

## Runtime Architecture

Refactor `RightMemoryRuntime` so role orchestration is separate from the agent
turn implementation.

The current Pydantic path becomes one executor. A new `CliAgentExecutor` handles
Codex and Claude Code calls.

At a high level:

1. The CLI loads role config.
2. Runtime acquires the same locks and performs the same sync flow it already
   owns for that role.
3. Runtime calls the configured executor for the model turn.
4. The executor composes a thin runtime wrapper plus the canonical role prompt.
5. The executor launches Codex or Claude Code in the memory root.
6. The executor parses the final assistant response.
7. Runtime continues with post-turn sync and returns the final text to the
   caller.

Session state should remain under `.runtime/`. For CLI-agent mode, each
RightMemory role/session pair stores the provider id needed for continuation.

Codex:

- First call runs `codex exec --json --cd <memory-root> ...`.
- The executor records the returned `thread_id`.
- Later calls run `codex exec resume --json <thread_id> ...`.

Claude Code:

- First call uses a deterministic UUID associated with the RightMemory
  role/session pair and passes it with `--session-id`.
- Later calls use `--resume <uuid>`.

The stored mapping should include provider name, provider session id, role,
RightMemory session id, created/updated timestamps, and enough diagnostic data
to explain a failed resume.

## Prompt Composition

CLI-agent prompts should be thin. Codex and Claude Code already know how to use
their own tools, so RightMemory should not duplicate tool instructions or restate
role rules that live in canonical prompts.

The wrapper should cover:

- this is RightMemory `<role>` mode;
- work in the configured memory root;
- treat `MEMORY.md`, sibling `MEMORY_*.md`, and `dream_logs/` as the memory
  store;
- follow the canonical role instructions below;
- return a concise final reply for the caller.

The canonical role prompts in `rightmemory/prompts/` remain the source of role
behavior for both runtime modes.

## Provider Defaults

Provider flags are runtime mechanics, not prompt content. They should give the
agent a normal coding-agent environment rooted at the memory directory while
keeping retrieval and write roles separated by command-selected role behavior.

Codex should run with `--cd <memory-root>`, JSON output, and a role-appropriate
sandbox default.

Claude Code should run from `<memory-root>`, use JSON output, and use
role-appropriate defaults that allow the selected role to do its job without
turning setup into a long permissions exercise.

The first implementation should keep these defaults in code. More user-exposed
CLI flag customization can wait for a concrete need.

## Doctor Command

Add a user-facing verification command:

```bash
rightmemory doctor agent-cli
```

The command should create a temporary memory root and verify the configured
provider behavior on the user's machine. It should print a compact pass/fail
report and leave the real memory root untouched.

The doctor should check the governing capabilities rather than merely checking
version strings:

- configured CLI exists;
- first non-interactive call succeeds;
- resume sees prior conversation history;
- retrieval can read a temporary memory file;
- a write-capable role can edit a temporary memory file;
- the provider can inspect, stage, and commit inside a temporary Git memory
  repo;
- default boundaries do not permit a write outside the temporary memory root;
- config resolves as expected for each role.

The command should fail clearly when auth, session continuation, filesystem
access, Git behavior, or config is not ready.

## Migration

Installer changes:

- accept `--mode cli-agent` and `--mode standalone`;
- reject `--mode subagent` with a clear message;
- install the CLI runtime for `cli-agent`;
- install the CLI-calling orchestrator skill for `cli-agent`;
- remove old `memory-curator` and `memory-dreamer` generated skills from the
  same skill target when they identify as RightMemory-owned skills.

Documentation changes:

- describe `standalone` as the custom Pydantic AI runtime;
- describe `cli-agent` as the Codex/Claude Code-backed runtime;
- document the small `[agent_cli]` config;
- document `rightmemory doctor agent-cli` as the setup confidence check;
- remove current instructions that present generated curator/dreamer subagent
  skills as the active install shape.

## Tests

Unit tests should cover deterministic RightMemory behavior:

- installer accepts `cli-agent` and rejects `subagent`;
- `cli-agent` installs the runtime and orchestrator without curator/dreamer
  role skills;
- config parses `[agent_cli]` and `[<role>.agent_cli]`;
- missing provider config produces a clear error in CLI-agent mode;
- command construction for first and resumed Codex calls;
- command construction for first and resumed Claude Code calls;
- JSON output parsing for Codex and Claude Code;
- provider session mapping persists by role/session;
- `retrieve`, `update`, `dreamer`, `reviewer`, and `sync-reconciler` can select
  the CLI-agent executor;
- standalone Pydantic AI tests keep using the existing executor path;
- `doctor agent-cli` reports each check result and uses a temporary memory root.

The live provider behavior belongs in `rightmemory doctor agent-cli`, because it
depends on local auth, installed CLI versions, and provider permission settings.

## Out Of Scope

This design does not add MCP support, new memory schema rules, remote agent
services, broad provider-specific prompt tuning, or a compatibility mode for the
old generated curator/dreamer subagent workflow.
