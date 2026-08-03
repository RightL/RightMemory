---
name: rightmemory-orchestrator
description: "Use when the user explicitly chooses full RightMemory orchestration: conditionally retrieve relevant durable Memory or live Pursuit context, then submit task-state, durable-context, and explicit-correction candidates so the unified updater can maintain both document trees."
---

# RightMemory Orchestrator CLI

## Access Rules

- The RightMemory root is `{{MEMORY_ROOT}}`; do not read or edit files there directly unless the user explicitly permits direct access.
- Pick one stable session id for this agent conversation and reuse it for every retrieve and update call.

## Conditional Retrieval

- Retrieve with `rightmemory retrieve --session <stable-session-id> "<memory need>"` when earlier context may materially improve the work.
- Describe the context needed from the user's intent instead of blindly forwarding the user's message.
- For factual, project, or domain context, retrieve when the conversation lacks background needed to work well; skip retrieval when the request is self-contained.
- Retrieve preference, workflow, and behavior guidance more proactively when it will shape collaboration, implementation, verification, communication, or completion choices. Treat phase and topic changes as strong triggers.
- Before drafting, designing, or implementing, review Agent Correction Memory. Use `rightmemory agent-corrections writing` for expression or presentation, or `rightmemory agent-corrections design` for reasoning, decisions, actions, or behavior. Run only the relevant command; do not run both by default.
- Give the retrieve command up to 3 minutes to return. Await or poll that command; do not run a separate blocking wait, explore files, or advance the task while it is pending.
- The retriever skips unchanged content already returned in this session. When the user needs matching content repeated, add `--include-returned` for that call; do not change the session id.
- Treat Memory as durable context and Pursuit as live intent or continuity. Retrieval output is authoritative source Markdown selected by the model and rendered by RightMemory, not a model-written summary.
- A selected F# heading includes its parsed detail subtree. A local M#, S#, or MF# heading does not by itself expand linked content; local M# evidence uses source ranges and local S# expands only as a complete instruction. Imported MF# graph content uses ids scoped to `MF#<view-id>`, including F# detail items; direct MF ranges are invalid. Imported M# ranges and complete S# instructions use qualified sources such as `MF#<view-id>/M#<id>` and `MF#<view-id>/S#<id>`.
- Returned content may include weaker matches. Apply only what fits.
- Briefly tell the user which guidance you decide to follow as it becomes relevant.
- If retrieved guidance is stale, wrong, too broad, or misleading, include the correction in the next candidate.
- Treat retrieved open-context questions as questions rather than facts. If current task context already answers one, include its id and answer in the next candidate; do not investigate solely because it appeared.
- Treat `Provider question context` for an `MQ#` heading as an optional external ask opportunity. When it would materially help, call `rightmemory shared-view ask <mq-id> "<question>"` using the actual task context. If unavailable, continue with local context and tell the user.

## Task-State Candidates

- When non-trivial work actually begins, submit a concise task-state candidate with `rightmemory update submit --session <stable-session-id> "<candidate>"`.
- Give each task a concise stable label within the conversation, and reuse it in later submissions for that task. The session id is conversation provenance, not task identity.
- If retrieval found a related Pursuit, identify it. Otherwise, make no claim about whether one exists; the updater owns matching and storage judgment.
- Submit another candidate when the task completes, becomes blocked or waiting, changes direction, or reaches a handoff-worthy state, even when the task seemed too small to justify a start candidate.
- Submit completion whenever work reaches a terminal state, including initially small work and work that produced no durable Memory.
- State the current task evidence; do not instruct the updater to create, keep, or remove a Pursuit.
- Proceed without waiting for or pulling the update result.

## Durable Context And Corrections

- Task-state submission does not make ordinary work Memory-worthy. Include possible durable content only when it should change how a future agent acts, decides, interprets context, or avoids a repeated mistake.
- Prefer natural project artifacts such as commits, design documents, code comments, reports, logs, and project-local notes. Do not mirror them into Memory unless a compact Memory entry adds retrieval value beyond the artifact.
- Do not submit ordinary bug causes, implementation steps, experiment runs, generated artifact lists, or task results as durable context merely because work occurred.
- For recurring artifact families, prefer one compact lookup rule or durable conclusion over one candidate per artifact.
- Include uncertainty and surrounding context when a user preference, workflow, behavior, project fact, constraint, decision, or domain interpretation may be durable but is not settled.
- For an explicit user correction to ordinary agent work, submit the concrete event: what the user requested, what the agent proposed or did, what the user rejected, and what was accepted. Do not choose the final correction category or force persistence.
- For stale or wrong retrieved state, identify it well enough for the updater to find and say whether the evidence supports revision, narrowing, or deletion.

## Update Status

- When the user asks for update status or results, call `rightmemory update pull --session <stable-session-id>`.
- To cancel a candidate that is still pending, call `rightmemory update undo --session <stable-session-id> <candidate-id>`.
