# RightMemory Agent Notes

## Project Shape
- RightMemory is one addressable graph organized into two Markdown document trees: durable Memory and live Pursuit. Human readability matters, but agent retrieval is the main design center.
- Core runtime code lives in `rightmemory/`: config loading, command orchestration, standalone tools, CLI-agent delegation, transcript-review candidate extraction, async unified updates, offline update-review correction, isolated semantic writes, sync, and provider transcript adapters.
- Canonical role prompts live in `rightmemory/prompts/`. Edit role behavior there first; installed skills do not contain generated role prompts.
- `skills/rightmemory-schema.md` defines the shared graph and file semantics. `PURSUIT_RULES.md` defines Pursuit lifecycle judgment; `MEMORY.example.md` and `PURSUITS.example.md` provide managed starter shapes. Do not add a parallel instruction layer.
- RightMemory installs exactly two independent user-facing skills: read-only `memory-retriever` and full-state `rightmemory-orchestrator`. The user selects between them; neither has trigger priority.
- `install.sh` and `install.ps1` are platform bootstraps for the shared stdlib-only `rightmemory.install_core` transaction. Both modes preserve existing user-authored RightMemory files and refresh managed example content only when its markers are present.
- `retrieve` model config is independent. Other roles may reuse the configured writer executor when their own `[<role>.model]` or `[<role>.agent_cli]` table is absent, so upgrade-added roles can run without rewriting user config.

## Development Commands
- Run the test suite with `python -m unittest discover -s tests`.
- For syntax-only checks, use `python -m compileall -q rightmemory tests`.
- Use `./install.sh [--mode cli-agent|standalone] <memory-root> <skills-target>` on macOS/Linux/WSL or `.\install.ps1 [--mode cli-agent|standalone] <memory-root> <skills-target>` on Windows PowerShell when verifying install behavior.
- `uv` is available on PATH. Use `uv --version` if you need to check it before running the installer.
- Use `rightmemory review scan --once`, `rightmemory review watch`, and `rightmemory review normalize --source <codex|claude> --path <file>` for transcript-review extraction. Transcript review submits evidence through the unified updater; it does not edit the graph directly.
- Use `rightmemory update-review scan --once` or `rightmemory update-review watch` to process stable human comments on unified-updater review documents.
- Use `rightmemory prune` to run generation-based active memory pruning, and `rightmemory history --session <id> <query>` for explicit retrieval from pruned memory.
- Use `rightmemory shared-view list|build-file|build-question|approve|pull|status|ask|credential|accept-invite|note|notes|inbox|inbox-http` when debugging `MF#`/`MQ#` shared-view connections, provider view source files, HTTP hubs, credentials, or interaction records.
- Use `rightmemory hub init|status|token|serve` when debugging self-hosted HTTP shared-view hubs.
- Use `rightmemory watch start|status|stop|restart` to manage transcript review, update review, dreamer, insight, pruner, and sync watchers. Use the corresponding direct `watch` command when debugging a lower-level loop.
- Use `rightmemory doctor agent-cli` after configuring cli-agent mode to check provider commands, role config, and basic read/write probes.
- Semantic upgrade notes are Markdown files under `rightmemory/semantic_upgrades/`; validate them with `python -m unittest discover -s tests -p 'test_semantic_upgrades.py'`.

## Maintaining This File (IMPORTANT!)
- Treat this file (./AGENTS.md) as operational instructions for coding agents, not as a design document. Keep durable design explanation in `README.md` or `DESIGN_NOTES.md`.
- Update this file when setup commands, test commands, install behavior, role boundaries, or git/memory safety rules change.
- Remove stale commands or environment assumptions as soon as they stop matching the repo; bad instructions are worse than missing instructions.
- Keep it concise enough to stay useful in Codex project instructions. Prefer scoped nested `AGENTS.md` files if a subdirectory needs special rules.

## RightMemory Runtime Rules
- A RightMemory root contains `MEMORY.md`, `PURSUITS.md`, `PURSUIT_RULES.md`, optional `MEMORY_*.md` and `PURSUIT_*.md` backing files, optional root `corrections.md`, `shared_views.toml`, `shares.toml`, optional provider-owned `shared_views/<view-id>/` source files, `insight_logs/`, `rightmemory.toml`, and `.runtime/`.
- Named profiles are registered in `<default-memory-root>/profiles.toml`. `rightmemory profile create <name>` defaults new roots to a sibling profile area such as `~/.rightmemory-profiles/<name>` for the normal default root.
- Runtime commands can select a profile with `--profile <name>`, or by a user-managed `.rightmemory-profile` file in the project tree. Tracking that file is a user/project choice.
- Profile roots are ordinary RightMemory roots with separate Memory, Pursuit, correction feedback, config, `.runtime/`, Git history, watcher state, async queues, sessions, and insight logs.
- The installer creates a root `.gitignore` allowlist so Git status normally shows `MEMORY.md`, `MEMORY_*.md`, `PURSUITS.md`, `PURSUIT_*.md`, `PURSUIT_RULES.md`, `corrections.md`, sharing metadata and provider view sources, and `insight_logs/*.md`.
- Runtime/session state and generated update-review documents belong under `.runtime/` and should not be committed. Root `corrections.md` is synchronized updater feedback and is committed.
- Parsed graph membership starts at `MEMORY.md` and `PURSUITS.md` and follows F# references. File globs do not establish graph membership. Existing unreferenced `MEMORY_*.md` files remain user state and must not be deleted, overwritten, or silently ignored during upgrades.
- Share relationships live in `shares.toml`. Shared view connections use `MF#` headings for mirrored file views and `MQ#` headings for provider question views; `shared_views.toml` stores resolver metadata. Provider view source files live under `shared_views/<view-id>/`; generated `dist/`, imports, inboxes, credentials, and interaction records live under generated or runtime locations and should not be committed unless the user intentionally publishes them elsewhere.
- Watcher locks, PID-bound stop requests, process-identity registrations, install refresh stamps, dreamer and insight trigger state, isolated temporary state, and isolated worktrees belong under `.runtime/`.
- Semantic upgrade absorption state belongs under `.runtime/semantic-upgrades.json`. Fresh installs baseline current semantic upgrade notes because the seeded memory already matches the current schema. Existing memory roots may report pending semantic upgrade notes; dreamer is responsible for applying them during consolidation.
- Transcript-review scans process one time-adjacent batch of eligible provider sessions per bounded scan. `scan --once` may review a partial batch; `watch` waits for a full `[review].batch_size` batch. Review state remains session-level: once a provider session has been reviewed, later changes or resumed turns with the same source/session id are skipped unless the review state is cleared. Successful extraction submits candidates through the same unified update queue.
- The default review window is 3 days via `[review].since_days`; keep that default unless the user explicitly changes the backlog policy.
- Every normal unified-updater commit creates one local Markdown review document with one overall human comment area. `update-review` processes each stable non-empty comment revision at most once. Resolved and expired blank documents are disposable runtime state; needs-input documents remain until the comment changes.
- Unified-update session ids provide provenance and batching boundaries, not task identity. The updater reconciles related candidates as evolving accounts and may change Memory, Pursuit, both, or neither in one transaction.
- Pruner uses commit generations rather than wall-clock age. Defaults are `[pruner].generation_commits = 70` and `[pruner].revival_grace_checkpoints = 2`; prune commits use `prune:` subjects and may be empty checkpoint commits.
- Historian is read-only archaeology over `prune:` ledgers and Git snapshots. Ordinary retrieve should stay focused on active memory.
- Dreamer watch reads `.runtime/dreamer/trigger-state.json` and runs when accumulated points reach `[dreamer.watch].trigger_points`. Each successful updater transaction or update-review correction adds one configured unit only when it changes Memory; Pursuit-only, no-op, failed, and transcript-extraction activity adds none. Defaults are trigger `50`, Memory-changing update `1.0`, and check interval `3000` seconds.
- Insight watch reads `.runtime/insight/trigger-state.json` and follows the same Memory-change accounting with a default trigger of `150`, update unit `1.0`, and check interval `3000` seconds. Insight reads active Memory and prior Insight logs, not Pursuit or updater-only `corrections.md`.
- Automatic unified-update, dreamer, insight, and pruner turns that operate on the main state root run in isolated `.runtime/worktrees/` checkouts on `rightmemory-isolated-<role>-<uuid>` branches. Runtime validates and lands successful role-owned commits, then promotes temporary session/provider state. CLI-agent isolated turns use a fresh provider session for speculative work and promote the new provider record after success. Transcript review is read-only extraction and queues any resulting candidate instead of entering the semantic-write path.
- Dirty synchronized state blocks automatic semantic writes, but runtime gives `sync-reconciler` one bounded chance to repair it before failing. Normal unified updates may commit Memory and Pursuit together. Update-review correction mode may also change root `corrections.md` alongside a successful state correction; correction-only commits are invalid. Memory-oriented maintenance roles retain narrower Memory-only surfaces. Sync repair may additionally cover `PURSUIT_RULES.md`, sharing metadata, provider view sources, and `insight_logs/*.md`. Failed isolated work is discarded and retried from the original source state.
- Stale isolated cleanup is role-scoped for review/dreamer/insight/pruner watcher startup and skips sync. Cleanup removes matching temporary branches and worktrees, not dirty files in the main memory repo.

## Upgrade Safety
- Before changing persisted state or install/watch/config behavior, check upgrade impact.
- If old state may break, be ignored, or need migration, tell the user and ask before implementing.
- Do not silently discard or rewrite existing user state.
- Before relinking, moving, or removing legacy backing files, inspect every existing root and backing file plus incoming and outgoing graph references. Preserve orphaned content until its role is known.
- When a schema, example, role prompt, or agent-guidance change affects how existing RightMemory should be organized or interpreted, add or update a semantic upgrade note under `rightmemory/semantic_upgrades/`. The note should tell dreamer what existing Memory may need to revisit after reinstall, without adding maintenance text to user state or exceeding Dreamer's Memory-only authority.

## Git And Safety
- Keep changes scoped. Do not revert or clean up user changes unless explicitly asked.
- Ignore unrelated untracked files such as `.DS_Store` and `tmp/`.
- When committing code changes, stage only intended repo files.
- Unified updater commits may span Memory and Pursuit in one transaction. Only update-review correction mode may add `corrections.md`, alongside a successful state correction; narrower maintenance-role and sync-repair surfaces remain as described above. The tool layer enforces this, but prompts should stay aligned.
- If a change touches prompt behavior, config shape, transcript-review or update-review state, or Git/RightMemory safety, add or update focused tests.
- Avoid tests that pin role prompt prose by exact sentence or wording. Prompt tests should cover assembly boundaries and durable invariants, such as the right role/schema being included, placeholders not leaking, and standalone-only tool names not appearing in cli-agent prompts.

## Writing And Documentation Style
- When editing README, schema, prompt, or skill text, prefer coherence over patch-like accumulation. The result should read as if it was written fresh around the current idea, not as an old design with exceptions bolted on later.
- If a requested change modifies the conceptual model, integrate it into the surrounding explanation. Do not merely append a caveat such as "also this case" or "except now this other thing"; rewrite the relevant paragraph or bullet group so the rule feels native.
- When the user says wording is "not coherent", "patch-like", or "not newly written", treat that as a request to improve the conceptual shape of the prose, not only grammar. Look for old/new seams, repeated rules, awkward exceptions, and sentences that describe history instead of the final design.
- For important docs/schema changes, discuss the intended wording or show a concise proposed diff before applying broad edits. Small wording fixes can be applied directly, but larger rewrites should keep the user's framing visible.
- These docs and skills are instructions for future agents. Patch-like text causes future agents to inherit the order of edits instead of the intended model, while coherent text gives them a stable rule to follow.
