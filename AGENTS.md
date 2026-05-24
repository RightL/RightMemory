# RightMemory Agent Notes

## Project Shape
- RightMemory is a tree + graph hybrid memory system designed primarily for AI agents. Human readability matters, but it is not the main design center.
- Core runtime code lives in `rightmemory/`: config loading, command orchestration, standalone tools, CLI-agent delegation, transcript review, async update batching, isolated semantic writes, and provider transcript adapters.
- Canonical role prompts live in `rightmemory/prompts/`. Edit role behavior there first; installed skills do not contain generated curator/dreamer role prompts.
- `skills/rightmemory-schema.md` is the schema source for memory files. `MEMORY.example.md` is the installer seed and the source for the managed example block that can be refreshed on reinstall.
- `install.sh` installs either standalone mode or cli-agent mode, preserves existing user memory files, and refreshes the managed example block when present.

## Development Commands
- Run the test suite with `conda run -n rightmemory python -m unittest discover -s tests`.
- For syntax-only checks, use `conda run -n rightmemory python -m compileall -q rightmemory tests`.
- Use `./install.sh [--mode cli-agent|standalone] <memory-root> <skills-target>` when verifying install behavior.
- `uv` is available through the existing conda environment: `conda run -n rightmemory uv --version`. Use `conda run -n rightmemory ./install.sh ...` when the installer needs `uv`.
- Useful review commands are `rightmemory review scan --once`, `rightmemory review watch`, and `rightmemory review normalize --source <codex|claude> --path <file>`.
- Use `rightmemory watch start|status|stop|restart` to manage background review and dreamer watchers. Use `rightmemory dreamer watch` directly when debugging the lower-level trigger loop.
- Use `rightmemory doctor agent-cli` after configuring cli-agent mode to check provider commands, role config, and basic read/write probes.
- Semantic upgrade notes are Markdown files under `rightmemory/semantic_upgrades/`; validate them with `conda run -n rightmemory python -m unittest discover -s tests -p 'test_semantic_upgrades.py'`.

## Maintaining This File (IMPORTANT!)
- Treat this file (./AGENTS.md) as operational instructions for coding agents, not as a design document. Keep durable design explanation in `README.md` or `DESIGN_NOTES.md`.
- Update this file when setup commands, test commands, install behavior, role boundaries, or git/memory safety rules change.
- Remove stale commands or environment assumptions as soon as they stop matching the repo; bad instructions are worse than missing instructions.
- Keep it concise enough to stay useful in Codex project instructions. Prefer scoped nested `AGENTS.md` files if a subdirectory needs special rules.

## Agent Behavior
- Do not use Superpowers skills unless the user explicitly asks for them.

## Memory Runtime Rules
- A memory root contains `MEMORY.md`, optional sibling `MEMORY_*.md` detail files, `dream_logs/`, `rightmemory.toml`, and `.runtime/`.
- The installer creates a memory-root `.gitignore` allowlist so git status normally shows only `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md`.
- Runtime/session/review state belongs under `.runtime/` and should not be committed.
- Watcher locks, install refresh stamps, dreamer trigger state, isolated temporary state, and isolated worktrees belong under `.runtime/`.
- Semantic upgrade absorption state belongs under `.runtime/semantic-upgrades.json`. Fresh installs baseline current semantic upgrade notes because the seeded memory already matches the current schema. Existing memory roots may report pending semantic upgrade notes; dreamer is responsible for applying them during consolidation.
- Reviewer scans process one time-adjacent batch of eligible provider sessions per bounded scan. `scan --once` attempts one batch and exits; `watch` repeats batch scans until no eligible work remains. Review state remains session-level: once a provider session has been reviewed, later changes or resumed turns with the same source/session id are skipped unless the review state is cleared.
- The default review window is 3 days via `[review].since_days`; keep that default unless the user explicitly changes the backlog policy.
- Dreamer watch reads `.runtime/dreamer/trigger-state.json` and runs when accumulated points reach `[dreamer.watch].trigger_points`. Defaults are trigger `50`, update candidate `1.0`, reviewed provider session `1.5`, and check interval `3000` seconds. `rightmemory dreamer watch --interval <seconds>` changes the trigger-check cadence for that process.
- Automatic `update`, `reviewer`, and `dreamer` session turns that operate on the main state root run in isolated `.runtime/worktrees/` checkouts on `rightmemory-isolated-<role>-<uuid>` branches. The role commits normally; runtime validates and lands successful memory commits back to the main memory repo, then promotes temporary session/provider state. CLI-agent isolated turns use a fresh provider session for speculative work and promote the new provider record after success.
- Dirty main memory files block automatic semantic writes. Temporary commits may touch `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md`, and each temporary commit must keep `MEMORY.md` as a regular file. Failed isolated work is discarded and retried from the original source state.
- Stale isolated cleanup is role-scoped for review/dreamer watcher startup and skips sync. Cleanup removes matching temporary branches and worktrees, not dirty files in the main memory repo.

## Upgrade Safety
- Before changing persisted state or install/watch/config behavior, check upgrade impact.
- If old state may break, be ignored, or need migration, tell the user and ask before implementing.
- Do not silently discard or rewrite existing user state.
- When a schema, example, role prompt, or agent-guidance change affects how existing memory should be organized or interpreted, add or update a semantic upgrade note under `rightmemory/semantic_upgrades/`. The note should tell dreamer what existing memory may need to revisit after reinstall, without adding maintenance text to user memory.

## Git And Safety
- Keep changes scoped. Do not revert or clean up user changes unless explicitly asked.
- Ignore unrelated untracked files such as `.DS_Store` and `tmp/`.
- When committing code changes, stage only intended repo files.
- Runtime memory commits must be limited to `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md`; the tool layer enforces this, but prompts should stay aligned.
- If a change touches prompt behavior, config shape, transcript review state, or git/memory safety, add or update focused tests.
- Avoid tests that pin role prompt prose by exact sentence or wording. Prompt tests should cover assembly boundaries and durable invariants, such as the right role/schema being included, placeholders not leaking, and standalone-only tool names not appearing in cli-agent prompts.

## Writing And Documentation Style
- When editing README, schema, prompt, or skill text, prefer coherence over patch-like accumulation. The result should read as if it was written fresh around the current idea, not as an old design with exceptions bolted on later.
- If a requested change modifies the conceptual model, integrate it into the surrounding explanation. Do not merely append a caveat such as "also this case" or "except now this other thing"; rewrite the relevant paragraph or bullet group so the rule feels native.
- When the user says wording is "not coherent", "patch-like", or "not newly written", treat that as a request to improve the conceptual shape of the prose, not only grammar. Look for old/new seams, repeated rules, awkward exceptions, and sentences that describe history instead of the final design.
- For important docs/schema changes, discuss the intended wording or show a concise proposed diff before applying broad edits. Small wording fixes can be applied directly, but larger rewrites should keep the user's framing visible.
- These docs and skills are instructions for future agents. Patch-like text causes future agents to inherit the order of edits instead of the intended model, while coherent text gives them a stable rule to follow.
