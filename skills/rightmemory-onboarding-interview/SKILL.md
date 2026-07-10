---
name: rightmemory-onboarding-interview
description: Use when a user wants to initialize or onboard a RightMemory memory root, create reviewable MEMORY.md or MEMORY_*.md drafts, convert project/user context into RightMemory schema, or asks for an onboarding interview before writing memory files.
---

# RightMemory Onboarding Interview

Run a short, evidence-aware interview that produces reviewable RightMemory drafts from durable project, user, environment, and agent-behavior context.

## Workflow

1. Read `skills/rightmemory-schema.md` and `MEMORY.example.md` when available. Treat the schema as authoritative and the example as illustrative, never as user facts.
2. Use the requested staging location, or `./tmp` by default. Do not overwrite a real memory root without explicit permission.
3. If the user asks what the interview covers, give a compact preview before starting.
4. Begin with the highest-yield question:

   > What projects, domains, or responsibilities should this memory cover, and what paths, links, docs, or servers can I inspect for each one?

5. Inspect supplied paths, repositories, docs, and links before asking follow-ups. Check durable sources such as READMEs, manifests, tests, API or integration docs, runbooks, and build or deployment files. Use bounded evidence collection only when permitted by the environment and project instructions.
6. Ask one practical follow-up at a time, and only when the answer cannot be inferred and would change future agent behavior. Focus on responsibility boundaries, project relationships, environments, integration surfaces, downstream callers, durable operating rules, and operationally relevant unknowns.
7. Stop when the evidence and answers support a useful draft. If the user rejects a question as unhelpful, skip that category and continue from existing evidence.
8. Write `MEMORY.md` and only the `MEMORY_<slug>.md` detail files needed for scanability. Re-read and validate every draft before reporting completion.

## Memory Content

- Record reusable facts, relationships, preferences, decisions, and future-facing rules. Rewrite evidence as durable memory; do not preserve transcript narration, onboarding chatter, or source citations.
- Avoid broad preference surveys, quickly stale status labels, and questions already answered by available evidence.
- Organize only the domains supported by the evidence. Common domains are a project or responsibility graph, `# User Context`, `# Cross-Session Agent Behavior`, and `# Open Context Questions`.
- Put loose, actionable unknowns in `# Open Context Questions` as normal nodes with `todo:` edges. Use `Uncertain:` only for tentative claims worth retaining and revising later.
- Use `{F#slug}` only when moving content to `MEMORY_slug.md` improves root-file scanability. Keep the heading and its summary in the current file, but not the moved child content. A terminal `####` file reference may have a summary but no child nodes or headings.

## Schema And Completion Checks

Before saying the drafts are ready:

- follow the canonical schema, including meaningful, unique heading and node IDs across the file set and `→[...]` on every node line;
- confirm every file-backed heading maps to its matching sibling detail file;
- confirm questions and uncertain claims use the correct forms;
- list the created files and identify user-provided facts that remain unverified, such as an inaccessible remote path.
