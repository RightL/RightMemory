# Reviewer Skill Distillation Design

## Goal

Extend automatic transcript review so it distills both ordinary durable memory
and skill-shaped reusable knowledge.

The reviewer may create or update memory-backed skills inside the RightMemory
tree, and may create supporting artifacts under the memory root when the
reusable lesson needs dense notes, repeatable scripts, templates, or similar
material. It should not edit installed Codex, Claude, or host-agent skill
folders.

The durable product is the memory tree plus skill artifacts. Git history is the
audit trail, so there is no separate review log file.

## Borrowed Lessons

Hermes is useful mainly for its background self-improvement shape: completed
conversations can be reviewed for user preferences, reusable techniques,
workflow corrections, and skill-library improvements. Its strongest ideas for
RightMemory are class-level skill judgment, support files for detailed material,
and care around transient setup failures. Hermes mutates installed skills
directly; RightMemory should instead write memory-native skill knowledge.

Codex Skill Creator is useful for the shape of skill knowledge: concise
instructions, an appropriate degree of freedom, progressive disclosure, optional
resources, and validation. RightMemory should use those principles rather than
copying the full Codex skill text into starter memory.

## Memory-Backed Skills

A memory-backed skill is a normal RightMemory topic whose content is procedural
enough to guide future work. It can live under any `#` domain where the tree
makes retrieval natural.

When the topic is detailed, it can use a file-backed heading:

```md
### Browser Debugging Workflow {F#skill-browser-debugging}
```

The detail file, such as `MEMORY_skill-browser-debugging.md`, acts like a
compact skill guide. It may describe when the skill applies, the reusable
workflow, pitfalls, validation cues, and pointers to supporting artifacts. These
sections are writing aids, not a rigid template.

Support artifacts live under a slug-scoped folder:

```text
skill_artifacts/skill-browser-debugging/
  references/
  scripts/
  templates/
  assets/
```

The reviewer should create artifact folders according to the material, not as a
checklist. Reference notes are for dense background, scripts are for repeatable
actions, and templates/assets are for reusable starting material. If the
memory-backed skill file can hold the lesson clearly, no artifact folder is
needed.

## Reviewer Behavior

For each normalized transcript, the reviewer evaluates whether the session
contains knowledge that will help future agents act, decide, retrieve context,
or avoid repeated mistakes.

The reviewer chooses the edit shape by coherence:

- ordinary memory fact;
- refinement of an existing memory-backed skill;
- support artifact under a memory-backed skill;
- new memory-backed skill;
- no edit.

The decision test is where a future agent would naturally retrieve and apply the
lesson without bloating or fragmenting the graph. There is no standing bias
toward refining existing topics or creating new ones. Refinement is appropriate
when the lesson improves an existing class of work; creation is appropriate when
the lesson represents a distinct class of work.

User corrections can belong in both memory and skill knowledge. Memory captures
the user's preference or expectation; a memory-backed skill captures how to do a
class of work differently next time.

The reviewer should avoid hardening transient setup failures into durable
negative rules. When there is a reusable fix or diagnostic pattern, capture that
positive lesson. Otherwise, leave the event out of durable memory.

## Dirty-State Recovery

This section was superseded by the isolated-write design on 2026-05-22. Current
runtime behavior keeps reviewer transcript processing separate from dirty-main
repair: automatic writes stop at the dirty-main guard, call `sync-reconciler`
once with bounded dirty-file context, and retry the original review only after
the memory repo is clean.

The shared memory write lock should prevent overlapping reviewer, update, and
dreamer edits. If tracked reviewer-owned paths are dirty anyway, the reviewer
resolves them before reviewing the transcript.

Reviewer-owned paths are:

- `MEMORY.md`
- `MEMORY_*.md`
- `skill_artifacts/...`

If the existing dirty changes are coherent and valid, the reviewer commits them
as a separate baseline commit based on the diff, then proceeds. If they are
invalid, partial, or unsafe to preserve, the reviewer discards those dirty
changes and proceeds. The baseline or cleanup should not be mixed with the
current transcript review commit.

No discarded-diff archive is needed.

## Git And Tooling

Commit tools need to include `skill_artifacts/...` in the reviewer-owned commit
allowlist. The installer-created memory-root `.gitignore` should surface those
paths in normal `git status`, alongside memory files and dream logs.

Reviewer commits should support a concise subject plus optional body. The body
can record the distilled signal and any skipped uncertainty, replacing a
separate `review_logs/` artifact.

Memory validation continues to check `MEMORY.md` and `MEMORY_*.md`. Artifact
files are committed durable support material, but they are not graph-validated
unless a later schema explicitly defines artifact structure.

## Starter Example

`MEMORY.example.md` should gain a compact memory-backed skill example for skill
creation guidance. This is a good starter because agents creating skills need a
procedural guide, and it demonstrates the new reviewer output shape.

The example should be written in RightMemory's own voice instead of copying
Hermes or Codex Skill Creator verbatim. Hermes is too tied to its installed
skill mutation tools, and Codex Skill Creator is too long for starter memory.
The example should distill the shared principles: create class-level skills,
keep guidance concise, choose support resources by purpose, validate the result,
and avoid one-session artifact skills.

The starter example should stay compact enough to live inside the managed
`MEMORY.example.md` block. It can describe when a procedural topic should grow
into a file-backed detail topic without requiring the installer to manage a
second sample detail file.

## Prompt Changes

The reviewer prompt should describe skill distillation as part of transcript
review. It should cover:

- ordinary memory versus memory-backed skill knowledge;
- edit-shape judgment based on future retrieval and application;
- optional support artifacts;
- dirty-state recovery;
- commit behavior and sync behavior after successful edits;
- no-op behavior when there is no durable signal.

The prompt should keep examples sparse and clearly illustrative, so future
reviewers reason from the governing criteria rather than treating examples as a
closed list.

## Tests

Focused tests should cover:

- reviewer prompt includes memory-backed skill distillation guidance;
- scanner/reviewer message still sends normalized transcript JSON;
- commit allowlist accepts `skill_artifacts/<slug>/...` and rejects unrelated
  files;
- commit tooling supports an optional body while preserving subject validation;
- installer memory-root `.gitignore` surfaces skill artifacts;
- memory validation ignores artifact internals while continuing to validate the
  memory graph;
- `MEMORY.example.md` contains a compact skill-creation guidance example.

## Out Of Scope

This design does not add installed skill mutation, external memory providers,
context compression, or separate review logs.
