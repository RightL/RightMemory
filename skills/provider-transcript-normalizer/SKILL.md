---
name: provider-transcript-normalizer
description: Add provider transcript normalization support.
---

# Provider Transcript Normalizer

Use this when adding or fixing RightMemory support for a provider's chat
history files, such as Codex, Claude, Cursor, or another coding agent.

## Goal

Do the one-time integration work so RightMemory can later scan that provider
automatically. This skill is about reading provider transcripts and producing a
small common session shape. It is not about deciding what should be remembered.

## Normalized Session Shape

Normalize one reviewable session at a time:

```json
{
  "source": "codex",
  "session_id": "abc",
  "project": "/path/or/null",
  "started_at": "2026-05-16T05:17:53Z",
  "ended_at": "2026-05-16T06:02:10Z",
  "turns": [
    {
      "user": "user-visible message",
      "assistant": "assistant-visible final reply"
    }
  ]
}
```

Keep this shape provider-neutral. Do not include tool calls, hidden reasoning,
raw refs, streaming deltas, large metadata, or provider-specific event objects.

## Procedure

1. Locate the provider's transcript storage.
2. Inspect a small raw sample to identify sessions, timestamps, project path,
   user messages, assistant replies, and completion status.
3. Implement or update a provider adapter that emits normalized sessions.
4. Add source configuration so RightMemory can scan the provider later without
   the main agent calling it.
5. Add small fixture transcripts and tests for the adapter.
6. Verify repeated scans are deterministic and reviewed sessions are not processed again.

## Boundaries

- Do not write `MEMORY.md` or `MEMORY_*.md`.
- Do not decide what is memory-worthy.
- Do not invoke reviewer, curator, or dreamer behavior.
- Do not expose raw provider metadata in the normalized payload.
- Do not treat interrupted or partial turns as completed.

## Pitfalls

- Streaming transcripts often contain duplicate deltas; collapse them into one
  final assistant reply.
- Tool results can be huge and noisy; omit them for the first version.
- Provider formats drift; keep parsing isolated behind provider adapters.
- RightMemory reviews a session once. Keep the normalized payload focused on the
  whole completed session, and let the scanner state decide whether it has
  already been processed.

## Verification

- Fixture with one completed session normalizes to one session payload.
- Fixture with an interrupted or partial turn omits that turn.
- Fixture with streaming assistant deltas emits only the final visible reply.
- Running normalization twice on unchanged input produces identical output.
