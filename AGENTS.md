# RightMemory Repository Guidance

## Purpose And Audience

- This file instructs coding agents that inspect and modify the RightMemory repository. It does not define the behavior of RightMemory's runtime roles.
- Runtime-role behavior belongs in `rightmemory/prompts/`, the implementation, and focused tests. Keep product behavior and design rationale in `README.md` and `DESIGN_NOTES.md` rather than duplicating them here.
- Add instructions here only when they are durable, repository-specific, and actionable during development. Put narrowly scoped guidance in a nested `AGENTS.md` when appropriate.

## Required Worktree Workflow

- Make implementation changes only on a dedicated task branch checked out in a Git worktree under the primary checkout's `./.worktree/` directory. Do not modify implementation files directly in the primary checkout.
- If the current directory is already a task-specific worktree, use it; do not create a nested worktree.
- Changes to runtime code, tests, configuration, installers, prompts, schemas, or other behavior-defining files count as implementation changes. Read-only investigation and discussion do not require a worktree.
- Preserve unrelated changes in both the primary checkout and task worktrees.

## Repository Map

- Core runtime code lives in `rightmemory/`; tests live in `tests/`.
- `rightmemory/graph.py` owns the canonical RightMemory grammar and in-memory document index. Graph-aware validation, retrieval, tools, sync, and shared-view code must consume that index rather than parse the Markdown structure again.
- Canonical role prompts live in `rightmemory/prompts/`. Edit role behavior there first; installed skills are not the source of truth for role prompts.
- `skills/rightmemory-schema.md` defines the shared Memory and Pursuit graph and file schema; `PURSUIT_RULES.md` defines Pursuit lifecycle judgment; `AGENT_CORRECTION_MEMORY_RULES.md` defines the fixed Agent Correction Memory module.
- `RIGHTMEMORY_EDIT_CORRECTION_RULES.md` defines non-semantic feedback about edits to RightMemory; `RIGHTMEMORY_EDIT_CORRECTIONS.example.md` illustrates its format.
- `MEMORY.example.md` and `PURSUITS.example.md` are installer seeds and sources of their managed example blocks.
- `install.sh` and `install.ps1` are platform bootstraps for the shared stdlib-only `rightmemory.install_core` transaction.
- Use `README.md` for behavior, usage, command, configuration, and file-layout documentation. Use `DESIGN_NOTES.md` for durable design rationale.

## Development Posture

- RightMemory is under rapid development and does not currently promise broad backward compatibility. Prefer a coherent current design over preserving undocumented historical behavior.
- Do not add migration frameworks, compatibility adapters, deprecated aliases, dual-format handling, fallback branches, or similar machinery for hypothetical users. Add an upgrade path only when the task explicitly requires one.
- When a change intentionally breaks an old format or behavior, update the implementation, tests, examples, and documentation together, and report the breakage in the handoff.
- Backward compatibility is optional; data safety is not. Never silently delete or overwrite a user's real memory data, and use disposable roots or fixtures for destructive verification.
- Do not create semantic upgrade notes merely to preserve hypothetical compatibility. Add or update one only when the task explicitly requires existing memory roots to be upgraded.

## State Admission Invariants

- Install may create semantic state only when bootstrapping a new root. A reinstall must preserve a complete existing root byte-for-byte or refuse an incomplete root before changing the root, runtime installation, installed skills, or install stamp. Do not restore managed-example refresh or implicit existing-root migration.
- Incoming sync state must merge, receive any model repair, and pass complete validation in a leased candidate worktree. Publish only by fast-forwarding the unchanged active root to the exact validated candidate; a failed merge, repair, validation, or publication check must leave active state unchanged.
- An `MF#` import is a version-two, schema-valid Memory document in a view-local namespace, not free-form Markdown. It may contain ordinary, F#, M#, and S# content with package-local backings; nested MF# and MQ# connections are invalid. Direct MF ranges are invalid, while imported M# and S# resources use qualified sources.

## Verification

- Run the full test suite with `python -m tests`. It runs every `test_*.py` module
  in bounded parallel processes; use `python -m tests --jobs N` to override the
  default concurrency of six.
- Run syntax checks with `python -m compileall -q rightmemory tests`.
- Add or update focused tests when changing prompt behavior, configuration shape, transcript/update-record state, CLI-agent thread lifecycle, or Git/memory safety.
- Prompt tests should verify assembly boundaries and durable invariants rather than pinning exact prose.
- When changing installer behavior, verify the affected modes with `./install.sh [--mode cli-agent|standalone] <memory-root> <skills-target>` on macOS/Linux/WSL or `.\install.ps1 [--mode cli-agent|standalone] <memory-root> <skills-target>` on Windows PowerShell. Use disposable test roots.
- When changing semantic upgrade machinery or notes, run `python -m unittest discover -s tests -p 'test_semantic_upgrades.py'`.

## Git And Repository Safety

- Keep changes scoped. Do not revert, remove, or clean up unrelated user changes.
- Ignore unrelated untracked files such as `.DS_Store` and `tmp/`.
- Stage and commit only files that belong to the task.
- When landing a task-worktree branch, use an explicit merge commit rather than a fast-forward merge. Its commit message body must summarize the changes being merged.
- Do not commit worktrees, `.runtime/` state, generated test output, credentials, or other local runtime artifacts.

## Writing And Documentation

- Write README, schema, prompt, and skill text as a coherent description of the current design, not as a history of accumulated exceptions.
- When a change modifies the conceptual model, rewrite the surrounding explanation so the new rule is native to it rather than appending a caveat.
- For broad documentation or schema changes, align on the intended conceptual wording before editing. Small local corrections can be applied directly.
