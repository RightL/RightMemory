# Agent Guidance Inbox Design

## Goal

Capture settled, potentially reusable agent guidance and user redirections more readily without letting unreviewed guidance influence future agents.

## Routing

`rightmemory-auto-orchestrator` continues to send ordinary Memory and Pursuit evidence to Update. Settled agent guidance or redirection that may help similar future work goes to a separate inbox unless the user explicitly asks RightMemory to remember it or follow it in future; those explicit requests continue through Update.

The approval-gated `rightmemory-orchestrator` keeps its existing proposal → approval → Update flow. An explicit request to submit, save, remember, or follow evidence in future counts as approval.

## Inbox

The inbox is the optional root file `AGENT_GUIDANCE_INBOX.md`. It is Git-tracked and synchronized but is not Memory, Pursuit, or Agent Corrections and is never exposed to Retrieve, shared views, or ordinary model roles.

Each pending entry has a stable generated id, session provenance, submission timestamp, and free-form Markdown evidence. Capture only when the resulting direction is clear and may be useful in similar future work; do not capture unresolved discussion or obviously one-off local adjustments.

The inbox contains pending entries only. Accepted, rejected, or already-covered entries are removed; Git history provides audit history.

## CLI

Version one adds only:

`rightmemory guidance submit --session <session-id> "<evidence>"`

The command deterministically appends one entry, creates the inbox on first use, performs an isolated atomic Git write, and does not invoke Update or any model role.

## Review

A new `review-agent-guidance-inbox` skill explicitly reviews pending entries against current Cross-Session Agent Behavior and Agent Corrections. It groups related evidence, proposes add/merge/replace/remove decisions and destinations, waits for user approval, then directly curates approved semantic changes and inbox removals in one validated commit. Deferred and unreviewed entries remain pending.

## Runtime and Sync

The inbox has a small mechanical parser/validator for its heading, stable unique ids, `Session`, and `Submitted` provenance; evidence bodies remain semantically opaque.

It is added to sync and root Git allowlists. Existing roots require no semantic migration; the first submit may force-add the optional file. Concurrent additions merge by entry id; deletion versus unchanged content preserves the deletion; conflicting edits to the same id remain a conflict.

The inbox is excluded structurally rather than by prompt wording: it is not a graph root, Retrieve source, shared-view source, or model-role read path.

## Scope

Automatic Reviewer and `review-rightmemory-session` are unchanged. Canonical Memory, Pursuit, and Agent Correction rules are unchanged. Update and Retrieve prompts are unchanged.
