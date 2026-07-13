# RightMemory Agent Notes

## Project Shape
- RightMemory is a tree + graph hybrid memory system designed primarily for AI agents. Human readability matters, but it is not the main design center.
- Core runtime code lives in `rightmemory/`: config loading, command orchestration, standalone tools, CLI-agent delegation, transcript review, async update batching, isolated semantic writes, and provider transcript adapters.
- Canonical role prompts live in `rightmemory/prompts/`. Edit role behavior there first; installed skills do not contain generated role prompts.
- `skills/rightmemory-schema.md` is the schema source for memory files. `MEMORY.example.md` is the installer seed and the source for the managed example block that can be refreshed on reinstall.
- `install.sh` and `install.ps1` are platform bootstraps for the shared stdlib-only `rightmemory.install_core` transaction. Both modes preserve existing user memory files and refresh the managed example block when present.
- `retrieve` model config is independent. Other roles may reuse the configured writer executor when their own `[<role>.model]` or `[<role>.agent_cli]` table is absent, so upgrade-added roles can run without rewriting user config.

## Development Commands
- Run the test suite with `python -m unittest discover -s tests`.
- For syntax-only checks, use `python -m compileall -q rightmemory tests`.
- Use `./install.sh [--mode cli-agent|standalone] <memory-root> <skills-target>` on macOS/Linux/WSL or `.\install.ps1 [--mode cli-agent|standalone] <memory-root> <skills-target>` on Windows PowerShell when verifying install behavior.
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
- Watcher locks, PID-bound stop requests, process-identity registrations, install refresh stamps, dreamer and insight trigger state, isolated temporary state, and isolated worktrees belong under `.runtime/`.
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
