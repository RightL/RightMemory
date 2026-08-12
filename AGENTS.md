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
- `rightmemory/reference/` owns the package-shipped schema and semantic rule documents. These are product definitions, not Memory-root or skills-root state.
- `rightmemory/reference/rightmemory-schema.md` defines the shared Memory and Pursuit graph and file schema; `rightmemory/reference/PURSUIT_RULES.md` defines Pursuit lifecycle judgment; `rightmemory/reference/AGENT_CORRECTION_MEMORY_RULES.md` defines the fixed Agent Correction Memory module.
- `rightmemory/reference/RIGHTMEMORY_EDIT_CORRECTION_RULES.md` defines non-semantic feedback about edits to RightMemory; `RIGHTMEMORY_EDIT_CORRECTIONS.example.md` illustrates its format.
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

- Install may create semantic state only when bootstrapping a new root. A reinstall must preserve a complete existing root byte-for-byte or refuse an incomplete root before changing the root, runtime installation, installed skills, or install stamp. Existing roots containing the legacy `PURSUIT_RULES.md` or `AGENT_CORRECTION_MEMORY_RULES.md` package copies must be refused for explicit review and removal. Do not restore managed-example refresh or implicit existing-root migration.
- Incoming sync state must merge, receive any model repair, and pass complete validation in a leased candidate worktree. Publish only by fast-forwarding the unchanged active root to the exact validated candidate; a failed merge, repair, validation, or publication check must leave active state unchanged.
- An `MF#` import is a version-two, schema-valid Memory document in a view-local namespace, not free-form Markdown. It may contain ordinary, F#, M#, and S# content with package-local backings; nested MF# and MQ# connections are invalid. Direct MF ranges are invalid, while imported M# and S# resources use qualified sources.

## Verification

- Run the full test suite with `python -m tests` for changes that affect executable
  behavior beyond agent-facing text. It runs every `test_*.py` module in bounded
  parallel processes; use `python -m tests --jobs N` to override the default
  concurrency of six.
- For changes limited to agent-facing text, do not run the full test suite. Run only
  relevant non-test validation such as syntax or packaging checks when applicable.
  Run tests only when executable behavior also changes or when the user explicitly
  requests them.
- Run syntax checks with `python -m compileall -q rightmemory tests`.
- Add or update focused tests when changing configuration shape, transcript/update-record state, CLI-agent thread lifecycle, or Git/memory safety.
- Do not test agent-facing text. Tests must not inspect, compare, snapshot, parse, or
  assert any content supplied to an agent, including prompts, assembled instructions,
  schemas and references embedded in prompts, runtime context blocks, formatting
  markers, tool descriptions, or output-format instructions. Do not read canonical
  prompt files from tests. Test only non-text behavior at the surrounding boundary,
  such as role and tool selection, state transitions, persistence, invocation, and
  parsing of agent output. Editing agent-facing text must never require test changes.
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

### Recorded Writing Failure

**What the agent wrote:** During a discussion of when RightMemory should retrieve and submit, the user proposed two broad retrieval judgments: retrieve when missing context prevents understanding the request, and retrieve when stored context could prevent an avoidable mismatch or repeated user redirection. Instead of evaluating those judgments directly, the agent expanded the later-retrieval case into the sentence "Retrieve again when exploration reveals a new person, project, identifier, artifact family, prior decision, or problem category that materially changes the retrieval query." Those categories did not represent different decisions or behavior; they made one ordinary judgment look like an exhaustive taxonomy. When the user said submission evidence should distinguish Memory, Pursuit, and Agent Corrections, the agent escalated that distinction into a YAML-like envelope containing `boundary`, `task_label`, `memory_candidates`, `pursuit_candidates`, and `correction_candidates`, followed by required-looking fields inside each section. The user had not requested a wire format, and no implementation constraint had established a need for one. The response therefore changed the subject from improving submission judgment to designing an unsolicited protocol. Elsewhere in the same explanation, the agent repeatedly converted contextual guidance into categorical commands through phrases built around `only when`, `must`, and mirrored `do this` / `do not do this` formulations.

**User feedback:** The user said this was a recurring and extremely annoying model-writing pattern. The enumeration made the prose feel artificial and bloated instead of clarifying the retrieval judgment. The typed envelope made a simple distinction rigid and needlessly complicated. Repeated categorical command phrasing made flexible judgment read like a mechanical policy. The user specifically objected that the agent kept inventing lists, restrictions, schemas, and procedural structure that had not been requested, and that these additions obscured the actual idea under discussion.
