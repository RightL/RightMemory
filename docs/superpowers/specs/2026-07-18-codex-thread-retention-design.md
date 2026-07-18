# Codex Thread Retention Design

## Purpose

RightMemory CLI-agent mode currently gives Codex the wrong conversation
lifecycle for its roles. Retrieve starts a fresh Codex thread for every call
and reconstructs prior conversation in the next prompt, while most other roles
can persist provider-session mappings and resume when a RightMemory session id
is reused. Internal Codex threads also remain in the user's normal Codex
history indefinitely.

This design makes retrieve the only role that resumes across independent
RightMemory invocations, makes every other CLI-agent role one-shot, and deletes
newly registered RightMemory Codex threads after 24 hours without successful
activity. Deletion uses Codex App Server's supported `thread/delete` method;
RightMemory never edits Codex rollout files or SQLite state directly.

## Decisions

- The inactivity lifetime is fixed at 24 hours in this change. It is not a new
  user configuration setting.
- `retrieve` is the only CLI-agent role that resumes across independent
  RightMemory commands.
- Every other CLI-agent role starts a fresh provider thread for each independent
  command or automatic cycle.
- An explicit interactive `chat` command may keep one process-local thread for
  the lifetime of that chat process, but it does not create a mapping that a
  later process can resume.
- The role policy applies to Codex and Claude. Automatic provider-thread
  deletion applies only to Codex because this design relies on Codex App
  Server's supported deletion API.
- Cleanup runs before CLI-agent work when needed and also through the managed
  RightMemory watcher.
- Expired retrieve sessions reset their RightMemory-local conversation and
  delivery state. Their next use starts from the current Memory and Pursuit
  roots.
- Existing Codex threads that predate the ownership registry are not discovered,
  imported, or deleted.

## Goals

- Preserve provider conversation continuity and prefix-cache opportunity for
  repeated retrieve calls in one active RightMemory session.
- Avoid replaying prior retrieve questions, answers, root snapshots, or role
  instructions into an already resumed provider thread.
- Prevent updater and maintenance work from accumulating unrelated multi-turn
  provider context.
- Remove newly created RightMemory Codex conversations after 24 hours of
  inactivity without risking ordinary user Codex threads.
- Keep cleanup failure non-fatal to Memory and Pursuit work.
- Continue excluding every internal CLI-agent provider session from transcript
  review, including one-shot and failed isolated sessions.
- Preserve existing runtime files and old provider-session records without a
  destructive migration.

## Non-Goals

- No dedicated `CODEX_HOME`.
- No direct deletion of files under `~/.codex` and no direct SQLite mutation.
- No discovery or cleanup of existing unregistered RightMemory Codex threads.
- No automatic deletion of Claude sessions.
- No configurable retention period in this change.
- No change to Memory, Pursuit, graph, shared-view, or updater semantics.
- No implementation of the separate structured retrieve-selection design. The
  lifecycle here must remain compatible with that design's future delivery
  coverage state.

## Current Behavior

`RightMemoryRuntime._run_session_cli_agent` calls the stateless executor path
for retrieve and the persisted session path for every other role. The stateless
retrieve path does not retain the returned Codex thread id. Instead,
`RetrieveContextStore` saves each query and answer, and every later retrieve
prompt includes that full local conversation again.

`CliAgentExecutor.run_session_turn` loads and saves a provider mapping for any
role. Codex resumes whenever a stored thread id exists. The existing
`fresh_provider_session` flag affects Claude id creation but does not prevent
Codex resume, even though isolated runtimes set it for speculative write turns.

`ProviderSessionStore` records only the latest mapping for a
role/RightMemory-session pair. It is sufficient for resumption but not for
ownership of multiple one-shot threads. `CliAgentExecutor.cleanup` is a no-op,
so neither mappings nor provider threads expire.

## Session Policy

Introduce an explicit CLI-agent conversation policy instead of inferring
behavior from whether a mapping happens to exist:

- `retrieve`: persisted session. Load the active mapping, resume the provider
  thread when it is still active, and update its successful-activity timestamp
  only after a successful turn.
- non-retrieve command or automatic cycle: one-shot session. Always start a new
  provider thread and never load or save an active resume mapping.
- non-retrieve interactive chat: process-local session. The first message starts
  a new provider thread; later messages in the same process reuse the in-memory
  id. Exiting the process discards the resume handle while retaining the
  ownership record for later cleanup and review exclusion.

The policy is provider-neutral. For Codex, one-shot means omitting `resume`.
For Claude, it means creating a fresh UUID instead of loading the old mapping.
Existing non-retrieve mappings remain runtime state for compatibility and
transcript-review exclusion, but new execution ignores them.

## Retrieve Context Flow

A new persisted retrieve thread receives:

1. the canonical retrieve role instructions;
2. the current daily snapshot of `MEMORY.md` and `PURSUITS.md`;
3. currently relevant recent submitted candidates; and
4. the current caller query.

A resumed retrieve thread receives only continuation data:

1. committed root changes since the last successful retrieve turn;
2. recent submitted candidates not previously delivered to that session; and
3. the current caller query.

It does not receive the canonical role instructions, full root snapshot, or
prior query/answer transcript again because those are already in the provider
thread. RightMemory may continue recording query/answer turns for diagnostics
and standalone compatibility, but CLI-agent resume does not render those turns
into the resumed prompt.

If a pre-feature retrieve session has local context but no registered active
provider thread, the first post-feature CLI-agent call treats it as a new
provider conversation. It uses the current root snapshot and does not replay
the legacy query/answer history. The old runtime file is not used as proof of a
provider thread.

When a registered retrieve thread expires, RightMemory removes its active
mapping and resets the complete retrieve-session lifecycle state. That includes
stored query/answer turns, delivered root commit, recent-candidate delivery
markers, and any future structured-selection delivery coverage. The next call
may therefore return relevant content again from a clean current snapshot.

## Provider Thread Ownership Registry

Keep active resume mappings under the existing location:

```text
.runtime/agent_cli_sessions/<role>/<rightmemory-session>.json
```

Add a provider-neutral ownership registry:

```text
.runtime/agent_cli_threads/<provider>/<thread-key>.json
```

`thread-key` is derived from a hash of the provider thread id so provider ids
never become unchecked filesystem paths. Each record contains:

- schema version;
- provider and provider session id;
- RightMemory role and session id;
- conversation policy (`persistent`, `one-shot`, or `process-local`);
- creation time;
- last successful activity time, nullable until the first successful turn;
- lifecycle status (`active` or `delete-pending`);
- last deletion attempt time and compact error text when deletion is pending.

The expiration clock uses `last_successful_activity_at`, falling back to
`created_at` when the provider thread was created but the role turn never
completed successfully.

Every newly created CLI-agent provider thread receives an ownership record.
Transcript review consults this registry in addition to legacy active mappings,
so one-shot and failed internal sessions never become updater evidence.

Codex may emit `thread.started` before a later command failure. The executor
must therefore inspect partial JSONL stdout on nonzero exit and register any
valid returned thread id before propagating the role error. It must not invent
an id when the event is absent or malformed.

Isolated execution initially writes ownership state into its temporary state
overlay. Successful isolation promotes it with the other role state. If
provider work succeeded but later validation or landing failed, the isolation
failure path copies the new ownership record into the main runtime registry so
the abandoned thread remains excluded from review and eligible for cleanup.

Claude ownership records remain available for transcript-review exclusion but
are not processed by the Codex deletion worker.

## Expiration And Cleanup

Expiration is logical before it is physical. Once a registered retrieve thread
has been inactive for 24 hours, RightMemory must never resume it again, even if
Codex deletion is temporarily unavailable.

For each expired record, cleanup:

1. acquires the cleanup lock;
2. acquires the existing role/session lock when the record owns a resumable
   RightMemory session;
3. rereads the ownership record and active mapping;
4. skips the record if successful activity is now newer than the cutoff;
5. marks the ownership record `delete-pending`;
6. removes the active mapping only when it still points to that exact provider
   thread;
7. resets retrieve-local lifecycle state when that mapping was detached;
8. sends the exact Codex thread id to the deletion client; and
9. removes the ownership record after confirmed or already-missing deletion.

One-shot records have no active mapping, so cleanup marks and deletes them
without resetting a RightMemory session.

The lock order is always cleanup lock followed by role/session lock. The
opportunistic cleanup entry point runs before normal runtime code acquires a
session lock. This prevents cleanup/turn lock inversion. After acquiring locks,
cleanup always rereads timestamps and mapping identity so a concurrent
successful retrieve cannot be deleted based on a stale scan.

## Codex App Server Deletion Client

Use one short-lived `codex app-server` stdio process for each non-empty cleanup
batch. The client:

1. starts the default JSONL stdio transport;
2. sends `initialize` with RightMemory client metadata;
3. sends the `initialized` notification;
4. sends one `thread/delete` request per exact registered thread id;
5. correlates responses by request id while ignoring unrelated notifications;
6. treats success and an already-missing thread as successful cleanup; and
7. shuts down the child process within bounded time.

The process inherits the same environment as `codex exec`, including any
shell-scoped `CODEX_HOME`, so deletion targets the same Codex state store that
created the thread. RightMemory does not copy credentials or introduce an API
key path.

App Server startup, handshake, protocol, timeout, and per-thread deletion
errors do not fail the user's retrieve, update, or maintenance operation. The
ownership record remains `delete-pending`, stores a bounded diagnostic, and is
retried by a later cleanup pass. Malformed ownership records are reported and
left untouched rather than guessed or deleted.

The protocol follows the documented stable lifecycle: initialize the
connection before requests, then use `thread/delete`, which removes persisted
rollout files and associated metadata. See the official
[Codex App Server documentation](https://developers.openai.com/codex/app-server#delete-a-thread).

## Cleanup Triggers

Cleanup has two triggers:

- Opportunistic: before top-level CLI-agent execution, scan the registry and
  run deletion only when expired Codex records exist. Nested isolated runtimes
  do not start their own cleanup pass.
- Background: add a lightweight managed watch target that scans hourly. It is
  included in `rightmemory watch start|stop|restart|status` with the other
  managed runtime services.

The watcher and opportunistic path call the same cleanup service. The scan is
local and cheap; `codex app-server` starts only for a non-empty deletion batch.
Standalone-only installations safely no-op when no Codex ownership records
exist. A thread becomes eligible exactly at 24 hours of inactivity; an idle
machine may retain it until the next hourly sweep, while the next CLI-agent
command always checks before deciding whether to resume.

Provide one direct bounded cleanup command for watcher wiring and diagnostics:

```text
rightmemory agent-cli cleanup --once
```

The direct command reports deleted, pending, skipped, and malformed record
counts without exposing credentials or full provider prompts.

## Compatibility And Upgrade Impact

The new ownership registry is additive runtime state under `.runtime/`. Existing
provider mapping JSON remains loadable without new fields. There is no scan or
migration of existing Codex history, and old provider threads are never deleted
merely because their prompts or working directory look like RightMemory.

Existing non-retrieve mappings are ignored for new resume decisions but remain
on disk. Existing retrieve-local conversation files without a registered
provider thread are not replayed into the first new Codex thread. This is a
runtime conversation-boundary change, not a rewrite of user-authored Memory or
Pursuit.

No semantic upgrade note is required because Memory, Pursuit, graph, and prompt
schema meaning do not change. `README.md`, `DESIGN_NOTES.md`, and `AGENTS.md`
must describe the final role lifecycle and cleanup command coherently.

## Testing

Focused tests must cover:

- the complete role-policy matrix for Codex and Claude;
- retrieve resumes by RightMemory session id across executor instances;
- all non-retrieve independent calls start fresh provider sessions;
- explicit non-retrieve chat retains only process-local continuity;
- isolated Codex roles honor one-shot policy rather than ignoring
  `fresh_provider_session`;
- the first retrieve prompt contains role instructions and the current root
  snapshot;
- resumed retrieve prompts omit role instructions, snapshot, and local Q/A
  replay while including new root diffs, recent candidates, and the query;
- legacy local retrieve context without a registered provider thread starts a
  clean provider conversation;
- every new provider thread receives one ownership record with atomic writes;
- a Codex command that emits `thread.started` and then fails still records that
  exact thread for review exclusion and cleanup;
- transcript review excludes active, one-shot, process-local, and failed
  isolated ownership records;
- successful isolated execution promotes ownership state;
- failed isolated execution preserves ownership state for later deletion;
- the 24-hour boundary uses last successful activity and falls back to creation
  time;
- renewed activity wins a cleanup race after timestamp recheck;
- cleanup detaches only a mapping that still points to the expired thread;
- expired retrieve cleanup resets query/answer, commit, recent-candidate, and
  future structured-delivery state;
- App Server initialization and multiple `thread/delete` requests share one
  JSONL process;
- success and already-missing responses remove registry records;
- startup, timeout, malformed response, and per-thread failure remain non-fatal
  and retryable;
- malformed registry records are reported but never guessed or deleted;
- opportunistic cleanup runs only at the top-level CLI-agent boundary;
- the managed cleanup watcher starts, reports status, and stops through the
  existing watch manager;
- existing provider mappings and unregistered Codex history remain untouched;
- `python -m compileall -q rightmemory tests` passes; and
- `python -m unittest discover -s tests` passes.
