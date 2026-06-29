---
name: rightmemory-onboarding-interview
description: Use when a user wants to initialize or onboard a RightMemory memory root, create reviewable MEMORY.md or MEMORY_*.md drafts, convert project/user context into RightMemory schema, or asks for an onboarding interview before writing memory files.
---

# RightMemory Onboarding Interview

## Goal

Run a short, evidence-aware interview that turns a user's durable project, role, environment, and agent-behavior context into reviewable RightMemory `MEMORY*.md` drafts.

The interview should reduce user burden: inspect provided paths and links, infer what can be inferred from evidence, and ask only questions whose answers will change future agent behavior.

## Procedure

1. Read the RightMemory schema and example first when available:
   - `skills/rightmemory-schema.md`
   - `MEMORY.example.md`
2. Ask where to write drafts only if the user has not specified a location. Default to `./tmp` for reviewable drafts. Do not overwrite a real memory root unless the user explicitly asks.
3. If the user asks what the interview will cover, show a compact preview before starting.
4. Start with the highest-yield question:

```md
What projects, domains, or responsibilities should this memory cover, and what paths, links, docs, or servers can I inspect for each one?
```

5. Inspect provided paths, repos, docs, or links before asking follow-up questions. Use bounded evidence collection or subagents only when the environment and user/project instructions allow it.
6. Ask one practical follow-up at a time. Prefer concrete facts over preferences:
   - responsibility boundaries;
   - project relationships and integration paths;
   - repos, services, libraries, APIs, CLIs, schemas, prompts, runbooks, and deployment units;
   - local and remote environments;
   - downstream callers or collaborators;
   - rules future agents should follow;
   - unresolved questions that should be asked later only when relevant.
7. Stop asking when enough durable memory can be drafted. If the user rejects a question as useless, accept that signal, skip that category, and continue from existing evidence.
8. Write reviewable `MEMORY.md` plus optional `MEMORY_<slug>.md` detail files.
9. Re-read the drafts and run a schema sanity check before reporting completion.

## Question Discipline

Ask questions only when the answer cannot be inferred and would change a future agent's action.

Good questions:

- "What projects are you responsible for, and what paths or links can I inspect?"
- "What is your responsibility boundary for these projects?"
- "Are there remote servers, local copies, or special environments future agents should know?"
- "When changing public APIs, who are the downstream callers or reviewers?"

Low-value questions to avoid unless clearly actionable:

- abstract priorities such as "what should agents optimize for most?";
- project status labels that will quickly go stale;
- broad preference surveys;
- anything the repos or docs can answer.

## Evidence Use

When the user provides paths or links, inspect them and summarize only durable facts. Look for:

- README, docs, changelogs, build files, package manifests, tests, examples, and integration guides;
- declared project purpose;
- main languages and frameworks;
- public API surfaces and caller-facing wrappers;
- sync scripts, generated copies, or integration boundaries;
- build/test commands when explicitly documented;
- local and remote paths that future agents may need.

Do not write transcript history, source citations, or "the user said in this session" into memory. Rewrite evidence into durable facts and future-facing rules.

## Draft Structure

Prefer this shape unless the user's domain suggests a better one:

```md
# <Domain Or Project Graph> {#domain-project-graph}

## Owned Projects {#owned-projects}

### <Project> {#project-slug} ->[rel:related-topic]

- `project-purpose` Durable project fact. ->[rel:project-slug]
- `project-local-path` Local path is `...`. ->[loc:dev-workstation, rel:project-slug]

## <Large Topic> {F#large-topic}

Short summary pointing to `MEMORY_large-topic.md`.

# User Context {#user-context}

## Role And Responsibility {#role-and-responsibility}

- `user-responsibility` Durable role fact. ->[rel:domain-project-graph]

# Cross-Session Agent Behavior {#cross-session-agent-behavior}

## Project Work Guidance {#project-work-guidance}

- `agent-rule` Future-facing agent behavior rule. ->[rel:domain-project-graph]

# Open Context Questions {#open-context-questions}

- `q-specific-unknown` Uncertain: Ask this only when it becomes operationally relevant. ->[todo:related-topic]
```

Use detail files when a topic would make the root hard to scan. A file-backed heading such as `{F#large-topic}` maps to sibling file `MEMORY_large-topic.md`; keep only the heading and summary in the current file.

## Schema Rules To Preserve

- Use `MEMORY.md` plus optional sibling `MEMORY_*.md` detail files.
- Use unique heading IDs and node IDs across the memory file set.
- Node lines use ``- `<node-id>` description ->[...]``. If a node has no edges, write `->[]`.
- Headings may omit `->[]` when they have no edges.
- Use `Uncertain:` only for unsettled memory that should be revised later.
- Use `# Open Context Questions` for loose ends, with normal nodes and `todo:` links.
- Do not turn example/template memory into facts about the user.
- Do not add child nodes beneath a terminal `####` file reference.

## Common Mistakes

| Mistake | Correction |
| --- | --- |
| Asking a full questionnaire before inspecting paths | Ask for paths first, inspect, then ask only what remains unknown. |
| Forcing status labels | Skip status unless it affects future action. |
| Asking abstract preference questions | Convert them into concrete future-agent behavior questions or omit them. |
| Writing directly into real memory | Draft in `./tmp` or the requested staging location first. |
| Capturing onboarding chatter | Store durable facts, rules, and open questions instead. |
| Making every topic a detail file | Split only when it improves scanability. |

## Verification

Before saying the draft is ready:

1. Re-read every created `MEMORY*.md` file.
2. Check that file-backed headings point to matching `MEMORY_<slug>.md` files.
3. Check that node lines have `->[...]`.
4. Check that IDs are meaningful and not duplicated.
5. Check that uncertain facts are either in `# Open Context Questions` or clearly prefixed with `Uncertain:`.
6. Confirm the final response lists the created files and any facts that were recorded from user-provided but unverified sources, such as remote server paths not inspected.
