# RightMemory Profiles Design

## Problem

RightMemory currently has one active memory root per installed command. The
installer writes a wrapper that exports `RIGHTMEMORY_ROOT`, and the runtime
loads `rightmemory.toml`, `MEMORY.md`, sessions, async update state, watcher
state, and Git history from that root.

That works for a personal default memory, but it is awkward for long-lived
project work. A project can have durable facts, workflows, and agent behavior
that should not mix with the user's default cross-project memory. Users need a
smooth way to name separate memory roots and let a project select one without
turning root paths into repeated command-line plumbing.

## Goals

Add named profiles that map a short profile name to a separate memory root.
The design should keep the default root as the ordinary unprofiled memory, let
users choose a project profile explicitly, and let a project opt into a local
profile binding when that is convenient.

The feature should preserve existing no-profile behavior. Existing memory roots
should not be moved or rewritten during upgrade. Runtime state, watcher state,
session history, async update queues, Git history, and memory files should stay
separate per profile root.

## Core Decisions

The default memory root remains the profile home. It owns a local registry:

```text
<default-memory-root>/profiles.toml
```

The registry maps profile names to memory root paths. This avoids a hidden
`~/.config` control plane while keeping profile paths out of project
directories.

New profile roots default outside the default root, in a sibling profile-root
area. For the normal `~/.rightmemory` default root, that path is:

```text
~/.rightmemory-profiles/<profile-name>/
```

For a custom default root, use the same sibling pattern rather than nesting
profiles inside the default root. Keeping project roots outside the default root
preserves filesystem authority boundaries: a default-root memory agent remains
scoped to the default root and does not gain direct tool access to nested
project memory roots.

Project binding is a user-managed local convenience. RightMemory reads a file
named `.rightmemory-profile` when present, and the file contains a profile name.
Whether that file is tracked, ignored, or absent is left to the user and the
project.

Runtime commands do not auto-create missing profiles. A typo should fail with
guidance rather than creating a new memory root.

## Command Surface

Global profile selection:

```bash
rightmemory --profile my-project retrieve "what do we know about this codebase?"
rightmemory --profile my-project update submit --session abc "remember ..."
rightmemory --profile my-project watch start
rightmemory --profile my-project status
```

Profile management:

```bash
rightmemory profile list
rightmemory profile create my-project
rightmemory profile create my-project --root ~/memories/my-project
rightmemory profile show my-project
rightmemory profile remove my-project
```

`profile create` registers the profile and initializes the target root. When
`--root` is omitted, the root lives under the sibling profile-root area; for a
normal install, that is `~/.rightmemory-profiles/<profile-name>/`.

`profile remove` unregisters the profile and leaves the memory root on disk.
This keeps deletion separate from registry cleanup and avoids accidental loss
of user memory.

Profile management commands operate on the profile home registry rather than
the currently selected project profile. Running `rightmemory profile list`
inside a project directory should still show the registry from the default
memory root. Combining `--profile` with `profile ...` commands should fail with
a clear message because profile management is about the registry, not an active
memory role.

## Profile Resolution

For runtime commands, resolve the active memory root before loading any role
config:

1. `--profile <name>` selects the registered profile.
2. If no flag is present, walk upward from the current directory looking for
   `.rightmemory-profile`; when found, select that registered profile.
3. If no profile is selected, use `RIGHTMEMORY_ROOT` when set.
4. Otherwise use `~/.rightmemory`.

`RIGHTMEMORY_ROOT` keeps its existing user-facing meaning for no-profile
commands: it is the active root. When a named profile is selected, the selected
profile root becomes the active root for that command. The default root used as
the profile home is derived from `RIGHTMEMORY_ROOT` when set, otherwise
`~/.rightmemory`.

The installed wrapper can continue to seed `RIGHTMEMORY_ROOT` with the install
root. The implementation should avoid making import-time constants decide the
root before CLI profile resolution runs.

## Registry Shape

Use a small TOML file under the default root:

```toml
[profiles.my-project]
root = "~/.rightmemory-profiles/my-project"
```

Profile names should be portable, path-safe labels made from letters, numbers,
`.`, `_`, and `-`. Empty names and path-like names are rejected. Root paths are
expanded with `~` support and stored in a stable user-readable form.

Malformed registry files should fail before runtime state is touched. Missing
registries are treated as an empty profile list.

## Profile Root Initialization

`profile create` should initialize a profile root with the same memory-root
shape expected by the rest of RightMemory:

- `MEMORY.md` seeded from `MEMORY.example.md`
- `insight_logs/`
- `.runtime/` with runtime ignore rules
- memory-root `.gitignore` allowlist
- Git repository and initial memory baseline commit
- semantic upgrade baseline for fresh seeded memory
- a usable `rightmemory.toml`

The config seeding should be careful rather than a blind copy. A new project
profile should be able to run retrieve/update/dreamer/insight/pruner using the
same executor settings as the default root when available. Cross-root
operational behavior should not be copied in a way that can pollute the new
project memory.

Recommended seeding policy:

- copy global `[agent_cli]` and role-local executor tables such as
  `[retrieve.agent_cli]`, `[update.agent_cli]`, or role-local `[*.model]`
  tables;
- copy safe tuning sections for async update and watcher thresholds when they
  are present;
- do not copy sync enablement into the new root;
- write an explicit empty review source list so project profiles do not review
  all global Codex and Claude transcripts by default.

Users can edit the profile root's `rightmemory.toml` afterward when they want
profile-specific models, sync, or review sources.

If the requested root already exists, `profile create` should be conservative.
It can register a root that already looks like a usable RightMemory memory root.
It should fail with repair guidance when the directory exists but does not look
safe to initialize or register.

## Runtime And Watchers

After resolution, the active profile root behaves like an ordinary memory root.
Role config, prompt assembly, standalone tools, CLI-agent sessions, async update
state, trigger state, insight logs, isolated worktrees, and Git operations all
use that active root.

Managed watch commands should use the selected root:

```bash
rightmemory --profile my-project watch start
rightmemory --profile my-project watch status
rightmemory --profile my-project watch stop
```

Watcher pid files and logs stay under the selected root's `.runtime/watch/`.
Managed watcher subprocesses should inherit the resolved active root through
their environment so re-exec and long-running watch loops keep using the same
profile root.

`rightmemory status` follows the same resolution rules as other runtime
commands. Inside a project directory with `.rightmemory-profile`, it reports
that profile root unless `--profile` selects a different one.

## Error Handling

Expected failures should be explicit and actionable:

- `--profile typo` fails with a profile-not-found message and suggests
  `rightmemory profile create typo`;
- `.rightmemory-profile` naming a missing profile fails and includes the
  binding file path;
- invalid profile names fail before reading or writing registry state;
- malformed `profiles.toml` fails before loading runtime config;
- missing or uninitialized profile roots fail with repair or recreate guidance;
- attempting to combine `--profile` with `rightmemory profile ...` fails with a
  message explaining that profile management uses the profile home registry.

`profile remove` should print the root path that remains on disk.

## Testing

Focused tests should cover:

- existing no-profile behavior with `RIGHTMEMORY_ROOT`;
- `--profile` selecting a registered root before config loads;
- `.rightmemory-profile` discovery from a nested project directory;
- CLI flag precedence over project binding;
- profile management commands ignoring project binding;
- missing profiles and malformed registries failing clearly;
- invalid profile names being rejected;
- `profile create` initializing a separate root outside the default root;
- seeded profile config preserving executor usability without inheriting sync or
  broad transcript review behavior;
- `profile remove` unregistering without deleting the root;
- `watch start`, `watch status`, `watch stop`, and `status` using the selected
  profile root.

Installer tests should verify that existing installs keep their no-profile root
behavior and that the wrapper does not prevent profile selection.

## Documentation

Update README sections for install options, runtime basics, background
watchers, and status. The docs should frame profiles as project or agent memory
isolation: a profile is a separate memory root, not a subsection inside the
default memory.

Document `.rightmemory-profile` as an optional local convenience file. The docs
should say that tracking or ignoring the file is a user/project decision.

Update `AGENTS.md` because this changes config, watch, and install behavior.
The agent notes should mention the profile registry location, the default root
location for created profiles, and the fact that runtime state stays per root.

## Upgrade Impact

Existing users who do not use profiles keep the current root-selection behavior:
`RIGHTMEMORY_ROOT` when set, otherwise `~/.rightmemory`.

Existing memory roots are not moved. Existing `rightmemory.toml`, runtime
state, watcher state, and Git history remain in their current root.

This feature changes runtime/config/watch selection, but it does not change the
memory schema or how existing memory should be interpreted. It does not need a
semantic upgrade note.

## Out Of Scope

The first profile design does not add automatic profile creation from typos,
automatic project detection, team policy for committing `.rightmemory-profile`,
profile-root deletion, hosted profile sync, or shared memory across profiles.
