# RightMemory Agent Notes

## Project Shape
- RightMemory is a tree + graph hybrid memory system designed primarily for AI agents. Human readability matters, but it is not the main design center.
- Core standalone code lives in `rightmemory/`: config loading, runtime orchestration, tools, transcript review, async update batching, and provider transcript adapters.
- Canonical role prompts live in `rightmemory/prompts/`. Edit role behavior there first; installed subagent skills are wrappers around the same canonical prompts.
- `skills/rightmemory-schema.md` is the schema source for memory files. `MEMORY.example.md` is the installer seed and the source for the managed example block that can be refreshed on reinstall.
- `install.sh` installs either standalone mode or subagent mode, preserves existing user memory files, and refreshes the managed example block when present.

## Development Commands
- Run the test suite with `python -m unittest discover -s tests`.
- For syntax-only checks, use `python -m compileall -q rightmemory tests`.
- Use `./install.sh [--mode subagent|standalone] <memory-root> <skills-target>` when verifying install behavior.
- `uv` is available through the existing conda environment: `conda run -n rightmemory uv --version`. Use `conda run -n rightmemory ./install.sh ...` when the installer needs `uv`.
- Useful review commands are `rightmemory review scan --once`, `rightmemory review watch`, and `rightmemory review normalize --source <codex|claude> --path <file>`.
- Use `rightmemory watch start|status|stop|restart` to manage standalone background review and dreamer watchers. Use `rightmemory dreamer watch` directly only when debugging the lower-level dream loop.

## Maintaining This File
- Treat this file as operational instructions for coding agents, not as a design document. Keep durable design explanation in `README.md` or `DESIGN_NOTES.md`.
- Update this file when setup commands, test commands, install behavior, role boundaries, or git/memory safety rules change.
- Remove stale commands or environment assumptions as soon as they stop matching the repo; bad instructions are worse than missing instructions.
- Keep it concise enough to stay useful in Codex project instructions. Prefer scoped nested `AGENTS.md` files if a subdirectory needs special rules.

## Agent Behavior
- Do not use Superpowers skills unless the user explicitly asks for them.

## Memory Runtime Rules
- A memory root contains `MEMORY.md`, optional sibling `MEMORY_*.md` detail files, `dream_logs/`, `rightmemory.toml`, and `.runtime/`.
- The installer creates a memory-root `.gitignore` allowlist so git status normally shows only `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md`.
- Runtime/session/review state belongs under `.runtime/` and should not be committed.
- Scheduled watcher locks, install refresh stamps, and dreamer watch state also belong under `.runtime/`.
- Reviewer scans process one normalized provider session at a time. `scan --once` attempts one eligible session and exits; `watch` repeats one-session scans until no eligible work remains. Review state is session-level: once a provider session has been reviewed, later changes or resumed turns with the same source/session id are skipped unless the review state is cleared.
- The default review window is 3 days via `[review].since_days`; keep that default unless the user explicitly changes the backlog policy.

## Git And Safety
- Keep changes scoped. Do not revert or clean up user changes unless explicitly asked.
- Ignore unrelated untracked files such as `.DS_Store` and `tmp/`.
- When committing code changes, stage only intended repo files.
- Runtime memory commits must be limited to `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md`; the tool layer enforces this, but prompts should stay aligned.
- If a change touches prompt behavior, config shape, transcript review state, or git/memory safety, add or update focused tests.

## Writing And Documentation Style
- When editing README, schema, prompt, or skill text, prefer coherence over patch-like accumulation. The result should read as if it was written fresh around the current idea, not as an old design with exceptions bolted on later.
- If a requested change modifies the conceptual model, integrate it into the surrounding explanation. Do not merely append a caveat such as "also this case" or "except now this other thing"; rewrite the relevant paragraph or bullet group so the rule feels native.
- When the user says wording is "not coherent", "patch-like", or "not newly written", treat that as a request to improve the conceptual shape of the prose, not only grammar. Look for old/new seams, repeated rules, awkward exceptions, and sentences that describe history instead of the final design.
- For important docs/schema changes, discuss the intended wording or show a concise proposed diff before applying broad edits. Small wording fixes can be applied directly, but larger rewrites should keep the user's framing visible.
- These docs and skills are instructions for future agents. Patch-like text causes future agents to inherit the order of edits instead of the intended model, while coherent text gives them a stable rule to follow.
