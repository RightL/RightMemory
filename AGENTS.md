# RightMemory Agent Notes

## Project Shape
- RightMemory is a tree + graph hybrid memory system designed primarily for AI agents. Human readability matters, but it is not the main design center.
- Core runtime code lives in `rightmemory/`: config loading, command orchestration, standalone tools, CLI-agent delegation, transcript review, async update batching, isolated semantic writes, and provider transcript adapters.
- Canonical role prompts live in `rightmemory/prompts/`. Edit role behavior there first; installed skills do not contain generated role prompts.
- `skills/rightmemory-schema.md` is the schema source for memory files. `MEMORY.example.md` is the installer seed and the source for the managed example block that can be refreshed on reinstall.
- `install.sh` installs either standalone mode or cli-agent mode, preserves existing user memory files, and refreshes the managed example block when present.
- `retrieve` model config is independent. Other roles may reuse the configured writer executor when their own `[<role>.model]` or `[<role>.agent_cli]` table is absent, so upgrade-added roles can run without rewriting user config.

## Development Commands
- Run the test suite with `python -m unittest discover -s tests`.
- For syntax-only checks, use `python -m compileall -q rightmemory tests`.
- Use `./install.sh [--mode cli-agent|standalone] <memory-root> <skills-target>` when verifying install behavior.
- `uv` is available on PATH. Use `uv --version` if you need to check it before running the installer.
- Useful review commands are `rightmemory review scan --once`, `rightmemory review watch`, and `rightmemory review normalize --source <codex|claude> --path <file>`.
- Use `rightmemory prune` to run generation-based active memory pruning, and `rightmemory history --session <id> <query>` for explicit retrieval from pruned memory.
- Use `rightmemory shared-view list|build-file|build-question|approve|pull|status|ask|credential|accept-invite|note|notes|inbox|inbox-http` when debugging `MF#`/`MQ#` shared-view connections, provider view source files, HTTP hubs, credentials, or interaction records.
- Use `rightmemory hub init|status|token|serve` when debugging self-hosted HTTP shared-view hubs.
- Use `rightmemory watch start|status|stop|restart` to manage background review, dreamer, insight, pruner, and sync watchers. Use `rightmemory dreamer watch`, `rightmemory insight watch`, or `rightmemory prune watch` directly when debugging lower-level loops.
- Use `rightmemory doctor agent-cli` after configuring cli-agent mode to check provider commands, role config, and basic read/write probes.
- Semantic upgrade notes are Markdown files under `rightmemory/semantic_upgrades/`; validate them with `python -m unittest discover -s tests -p 'test_semantic_upgrades.py'`.

## Maintaining This File (IMPORTANT!)
- Treat this file (./AGENTS.md) as operational instructions for coding agents, not as a design document. Keep durable design explanation in `README.md` or `DESIGN_NOTES.md`.
- Update this file when setup commands, test commands, install behavior, role boundaries, or git/memory safety rules change.
- Remove stale commands or environment assumptions as soon as they stop matching the repo; bad instructions are worse than missing instructions.
- Keep it concise enough to stay useful in Codex project instructions. Prefer scoped nested `AGENTS.md` files if a subdirectory needs special rules.

## Memory Runtime Rules
- A memory root contains `MEMORY.md`, optional sibling `MEMORY_*.md` detail files, `shared_views.toml`, `shares.toml`, optional provider-owned `shared_views/<view-id>/` source files, `insight_logs/`, `rightmemory.toml`, and `.runtime/`.
- Named profiles are registered in `<default-memory-root>/profiles.toml`. `rightmemory profile create <name>` defaults new roots to a sibling profile area such as `~/.rightmemory-profiles/<name>` for the normal default root.
- Runtime commands can select a profile with `--profile <name>`, or by a user-managed `.rightmemory-profile` file in the project tree. Tracking that file is a user/project choice.
- Profile roots are ordinary memory roots with separate `MEMORY.md`, `rightmemory.toml`, `.runtime/`, Git history, watcher state, async queues, sessions, and insight logs.
- The installer creates a memory-root `.gitignore` allowlist so git status normally shows `MEMORY.md`, `MEMORY_*.md`, `shared_views.toml`, `shares.toml`, provider view source files under `shared_views/<view-id>/`, and `insight_logs/*.md`.
- Runtime/session/review state belongs under `.runtime/` and should not be committed.
- Share relationships live in `shares.toml`. Shared view connections use `MF#` headings for mirrored file views and `MQ#` headings for provider question views; `shared_views.toml` stores resolver metadata. Provider view source files live under `shared_views/<view-id>/`; generated `dist/`, imports, inboxes, credentials, and interaction records live under generated or runtime locations and should not be committed unless the user intentionally publishes them elsewhere.
- Watcher locks, install refresh stamps, dreamer and insight trigger state, isolated temporary state, and isolated worktrees belong under `.runtime/`.
- Semantic upgrade absorption state belongs under `.runtime/semantic-upgrades.json`. Fresh installs baseline current semantic upgrade notes because the seeded memory already matches the current schema. Existing memory roots may report pending semantic upgrade notes; dreamer is responsible for applying them during consolidation.
- Reviewer scans process one time-adjacent batch of eligible provider sessions per bounded scan. `scan --once` may review a partial batch; `watch` waits for a full `[review].batch_size` batch. Review state remains session-level: once a provider session has been reviewed, later changes or resumed turns with the same source/session id are skipped unless the review state is cleared.
- The default review window is 3 days via `[review].since_days`; keep that default unless the user explicitly changes the backlog policy.
- Pruner uses commit generations rather than wall-clock age. Defaults are `[pruner].generation_commits = 70` and `[pruner].revival_grace_checkpoints = 2`; prune commits use `prune:` subjects and may be empty checkpoint commits.
- Historian is read-only archaeology over `prune:` ledgers and Git snapshots. Ordinary retrieve should stay focused on active memory.
- Dreamer watch reads `.runtime/dreamer/trigger-state.json` and runs when accumulated points reach `[dreamer.watch].trigger_points`. Defaults are trigger `50`, update candidate `1.0`, reviewed provider session `1.5`, and check interval `3000` seconds. `rightmemory dreamer watch --interval <seconds>` changes the trigger-check cadence for that process.
- Insight watch reads `.runtime/insight/trigger-state.json` and runs when accumulated points reach `[insight.watch].trigger_points`. Defaults are trigger `150`, update candidate `1.0`, reviewed provider session `1.5`, and check interval `3000` seconds. `rightmemory insight watch --interval <seconds>` changes the trigger-check cadence for that process.
- Automatic `update`, `reviewer`, `dreamer`, `insight`, and `pruner` session turns that operate on the main state root run in isolated `.runtime/worktrees/` checkouts on `rightmemory-isolated-<role>-<uuid>` branches. The role commits normally; runtime validates and lands successful role-owned commits back to the main memory repo, then promotes temporary session/provider state. CLI-agent isolated turns use a fresh provider session for speculative work and promote the new provider record after success.
- Dirty sync-owned memory files block automatic semantic writes, but runtime gives `sync-reconciler` one bounded chance to repair local dirty state before failing the automatic write. Active memory role commits are limited to `MEMORY.md` and `MEMORY_*.md`; sync repair may also cover `shared_views.toml`, `shares.toml`, provider view source files, and `insight_logs/*.md`. Active memory commits keep `MEMORY.md` as a regular file. Failed isolated work is discarded and retried from the original source state.
- Stale isolated cleanup is role-scoped for review/dreamer/insight/pruner watcher startup and skips sync. Cleanup removes matching temporary branches and worktrees, not dirty files in the main memory repo.

## Upgrade Safety
- Before changing persisted state or install/watch/config behavior, check upgrade impact.
- If old state may break, be ignored, or need migration, tell the user and ask before implementing.
- Do not silently discard or rewrite existing user state.
- When a schema, example, role prompt, or agent-guidance change affects how existing memory should be organized or interpreted, add or update a semantic upgrade note under `rightmemory/semantic_upgrades/`. The note should tell dreamer what existing memory may need to revisit after reinstall, without adding maintenance text to user memory.

## Git And Safety
- Keep changes scoped. Do not revert or clean up user changes unless explicitly asked.
- Ignore unrelated untracked files such as `.DS_Store` and `tmp/`.
- When committing code changes, stage only intended repo files.
- Runtime memory commits for active memory-editing roles are limited to `MEMORY.md` and `MEMORY_*.md`; sync repair may also commit `shared_views.toml`, `shares.toml`, provider view source files, and `insight_logs/*.md`. The tool layer enforces this, but prompts should stay aligned.
- If a change touches prompt behavior, config shape, transcript review state, or git/memory safety, add or update focused tests.
- Avoid tests that pin role prompt prose by exact sentence or wording. Prompt tests should cover assembly boundaries and durable invariants, such as the right role/schema being included, placeholders not leaking, and standalone-only tool names not appearing in cli-agent prompts.

## Writing And Documentation Style
- When editing README, schema, prompt, or skill text, prefer coherence over patch-like accumulation. The result should read as if it was written fresh around the current idea, not as an old design with exceptions bolted on later.
- If a requested change modifies the conceptual model, integrate it into the surrounding explanation. Do not merely append a caveat such as "also this case" or "except now this other thing"; rewrite the relevant paragraph or bullet group so the rule feels native.
- When the user says wording is "not coherent", "patch-like", or "not newly written", treat that as a request to improve the conceptual shape of the prose, not only grammar. Look for old/new seams, repeated rules, awkward exceptions, and sentences that describe history instead of the final design.
- For important docs/schema changes, discuss the intended wording or show a concise proposed diff before applying broad edits. Small wording fixes can be applied directly, but larger rewrites should keep the user's framing visible.
- These docs and skills are instructions for future agents. Patch-like text causes future agents to inherit the order of edits instead of the intended model, while coherent text gives them a stable rule to follow.

## Recent Agent Correction Window
- This section is a rolling window of recent user corrections to agent behavior. Keep at most 15 numbered `###` correction entries here. Add new entries at the bottom; when adding would exceed 15 entries, delete entries from the top first.
- Correction entries should capture reusable agent-behavior fixes, not merely what the current task changed. The task incident is evidence for the behavior.
- Include enough concrete before/after detail for future agents to reconstruct the failure: what the agent wrote, proposed, or did; what the user rejected or accepted instead; and what habit should change. For wording or prompt corrections, include rejected and accepted snippets when they are the clearest evidence.
- Keep entries focused on preventing recurrence across tasks. Do not make them mere changelog entries or final diff summaries; use concrete artifacts to expose the reusable behavior mistake.

### 1. Retrieve Prompt Tool Constraints

Do not add negative guidance for tools retrieve does not expose. The wrong wording was like `Retrieve should not use broad shell, search, generic file-read, or provider-question tools.` The correction is to omit that entirely because retrieve currently exposes typed retrieve tools, not broad shell/search/generic read/provider-question tools.

### 2. Root Memory Prefix Prompt Scope

Keep root-memory-prefix retrieve prompt edits minimal and native to the existing prompt. The wrong shape was adding a new `## Memory Detail Files` section with several explanatory paragraphs. The correction is to update the existing source bullets in `rightmemory/prompts/retrieve.md`, for example changing the daily snapshot bullet to say the snapshot contains `MEMORY.md` and `read_memory_file(slug)` is for relevant `F#` headings backed by `MEMORY_<slug>.md`.

### 3. Schema-Specific Detail Files

Be schema-specific about detail files. The vague wording `read_memory_file(slug) reads an ordinary MEMORY_<slug>.md detail file` is not enough. The corrected wording should say: `read_memory_file(slug)` reads the `MEMORY_<slug>.md` detail file for a relevant `F#` heading. This matters because `skills/rightmemory-schema.md` defines `{F#slug}` as the pointer to sibling `MEMORY_<slug>.md`.

### 4. Label Prompt Diff Sources

When showing prompt diffs, label which prompt source each snippet edits. In this repo, retrieve prompt prose lives in `rightmemory/prompts/retrieve.md`, while generated tool guidance lives in `rightmemory/prompt.py` under `Available retrieve tools`. The user asked because an unlabeled prompt diff made it unclear whether one or two files were changing.

### 5. Corrections Are For Behavior, Not Only Memory

The reason for recording these corrections here is not only memory. The user wants future agents to internalize the preference: avoid prompt bloat, avoid documenting nonexistent constraints, tie wording to schema concepts such as `F#`, and make diffs concrete enough to review without guessing.

### 6. Overlong Prompt Diff For A Small Rule

The user asked how to reduce redundant memory-orchestrator updates. The agent first proposed a bloated `skills/memory-orchestrator-cli/SKILL.md` diff: the first, second, and fifth added bullets were useful, but the third, fourth, and sixth repeated or over-expanded the rule. It also used `AGENTS.md` and `DESIGN_NOTES.md` as general examples, which the user rejected as too repo-specific.

Rejected shape:

```diff
+- Before submitting an update, check whether the same useful information is already durably captured in a natural artifact that future agents are likely to inspect, such as a git commit message, design doc, `AGENTS.md`, `DESIGN_NOTES.md`, code comment, experiment report, run log, or project-local notes.
+- If a natural artifact already captures the useful information, skip the memory update unless memory adds retrieval value that the artifact alone does not provide.
+- Memory usually adds retrieval value for durable user preferences, workflow rules, corrections to stale memory, repeated failure patterns, hidden reasoning, non-obvious project/domain interpretation, environment constraints, or stable lookup rules for a class of artifacts.
+- Do not submit updates that mainly duplicate commit history, implementation chronology, report inventories, generated artifact lists, individual experiment rows, parameter tweaks, or one-off local states.
+- For recurring project artifacts, prefer one compact lookup rule over repeated updates. For example, remember that future agents should inspect the local experiment log/report directory when they need run details, rather than remembering every new experiment report path.
+- When submitting despite an existing artifact, state what extra retrieval value memory adds instead of only restating the artifact.
```

Accepted `skills/memory-orchestrator-cli/SKILL.md` shape:

```diff
+- Before submitting an update, check whether the same useful information is already durably captured in a natural artifact that future agents are likely to inspect, such as a git commit message, design doc, code comment, experiment report, run log, or project-local notes.
+- If a natural artifact already captures the useful information, skip the memory update unless memory adds retrieval value that the artifact alone does not provide.
+- For recurring project artifacts, prefer one compact lookup rule over repeated updates. For example, remember that future agents should inspect the local experiment log/report directory when they need run details, rather than remembering every new experiment report path.
```

Accepted `rightmemory/prompts/update.md` shape:

```diff
+- Treat natural artifacts such as git commits, project docs, experiment reports, run logs, code comments, and project-local notes as possible durable storage. Do not mirror them into memory unless the candidate adds retrieval value beyond the artifact.
+- When the useful durable shape is a recurring artifact family, prefer a compact lookup rule or durable conclusion over one memory entry per artifact.
```

The behavior correction is not just about this task: when a user rejects prose as too long, duplicative, or poorly targeted, preserve the concrete before/after evidence and rewrite toward the smallest coherent rule instead of adding taxonomies, repeated caveats, or weak examples.
