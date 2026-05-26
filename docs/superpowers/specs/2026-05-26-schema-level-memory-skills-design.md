# Schema-Level Memory Skills Design

## Goal

Add schema-level memory skills to RightMemory.

A memory skill is a reusable instruction asset. It can be a workflow, a
judgment playbook, a prompt-shaped instruction the user would otherwise repeat,
or a bounded operating style for a recurring situation. Ordinary memory records
durable facts, context, and preferences. Skill memory tells a future agent how
to act when the relevant situation comes up.

The design keeps skills inside the memory tree rather than creating an external
skill runtime. Retrieval stays progressive: broad retrieval sees the skill name
and compact description; a second retrieval can open the full skill body when
the main agent decides to use it.

## Schema And Storage

Introduce `S#` as an addressable heading marker:

```md
## Two-Side Review {S#two-side-review} → [rel:workflow-adversarial-review]

A reusable review style where one pass builds the strongest supporting case and
another pass looks for gaps, risks, and counterevidence.
```

The graph id is `two-side-review`, so edges target `two-side-review`, not
`S#two-side-review`. Heading ids, skill ids, and node ids share the existing id
namespace.

`S#two-side-review` maps to:

```text
MEMORY_SKILL_two-side-review.md
```

`S#` headings can live anywhere in the ordinary memory tree. Placement should
follow retrieval context: user-behavior skills can live near agent behavior,
project-specific skills can live near that project, and domain workflows can
live near the domain memory that makes them meaningful.

`MEMORY_SKILL_*.md` is covered by the existing `MEMORY_*.md` file pattern. The
implementation should verify that commit validation, isolated-write landing,
sync paths, installer `.gitignore`, and role instructions already treat skill
files as memory files through that pattern.

## Retrieval Flow

Retriever treats `S#` as progressive disclosure.

For ordinary task queries, the retriever may return strongly relevant `S#`
heading lines plus their body paragraphs. The heading body is the compact
description that helps the main agent decide whether the skill applies.

When the main agent asks to see or use a specific skill, retriever opens the
matching `MEMORY_SKILL_<slug>.md` file and returns its contents. Broad recall
should stay compact; full skill bodies are read when the main agent asks for
that skill.

Example flow:

```text
main agent query: "two-side review this plan"
retrieve returns: heading and short body for {S#two-side-review}
main agent decides: this applies
main agent query: "retrieve the full skill two-side-review"
retrieve returns: MEMORY_SKILL_two-side-review.md
```

## Automatic Creation And Refinement

Automatic reviewer and dreamer roles may create or refine `S#` skills when the
evidence supports a reusable instruction asset. Explicit user requests are also
valid update inputs, but they are not required before automatic roles can act.

The governing test is whether future agents would benefit from actionable
guidance rather than another description of the user's preference. A skill
candidate should have a recognizable trigger, stable enough input shape, useful
action or judgment guidance, and a clear output or stopping condition.
Repetition is strong evidence, and a single costly workflow can qualify when it
is plainly likely to recur.

Memory and skill knowledge can coexist. A memory node might record that the user
often asks for support/unsupport reviews. The corresponding skill would explain
how to perform that review.

Automatic roles should refine nearby existing skills when that keeps the model
coherent, and create a new skill when the instruction asset is distinct. Weak
evidence should stay as ordinary memory or uncertain memory rather than becoming
a speculative skill.

## Skill File Shape

Skill files are instruction artifacts, not forms to fill in. There is no rigid
section template.

`MEMORY_SKILL_<slug>.md` should contain enough guidance for an agent to apply the
skill after reading it. The shape can be a few paragraphs, a short playbook,
criteria, a reusable prompt, or pointers to exact commands when the action is
fully determined. The expected output and stopping point should be clear enough
for a future agent to know when it has completed the skill.

Example:

```md
# Two-Side Review

Use this when the user asks for a two-side review or wants a plan evaluated from
opposing perspectives.

Run two passes: first build the strongest supporting case, then look for the
strongest gaps, risks, and counterevidence. Keep both sides grounded in evidence
from the repo or task context. End with a balanced recommendation and open
questions.
```

## Role Changes

The schema should describe `S#`, its id behavior, and its mapping to
`MEMORY_SKILL_<slug>.md`.

The retriever prompt should explain the two-step behavior: return skill headings
and body paragraphs during broad recall, and return the full skill file when the
caller asks for the specific skill.

The update, reviewer, and dreamer prompts should distinguish ordinary memory
from reusable instruction assets. They should support automatic creation and
refinement without pushing agents toward broad, speculative, or overlapping
skills.

The wording should express the governing principle first. Sparse examples can
clarify the idea, but the prompts should avoid checklist-like templates that
future agents might apply mechanically.

## Semantic Upgrade

Add a semantic upgrade note asking dreamer to revisit existing instruction-like
or prompt-like memories. Dreamer should consider converting strong candidates
into `S#` skills when the current memory describes a recurring way to act but
does not give future agents enough instruction to apply it.

The note should guide dreamer to preserve ordinary memory for facts and
preferences, create skills for reusable agent instructions, and avoid converting
weak or one-off memories.

## Tests

Focused tests should cover:

- `S#` headings parse as addressable ids and participate in duplicate-id and
  edge validation.
- `S#slug` maps to `MEMORY_SKILL_<slug>.md`.
- `MEMORY_SKILL_*.md` remains accepted by existing memory-file commit, sync,
  isolated-write, and install behavior.
- Prompt assembly includes the skill semantics for retriever, update, reviewer,
  and dreamer.
- Semantic upgrade discovery includes the new upgrade note.

Prompt tests should check durable invariants rather than exact prose.
