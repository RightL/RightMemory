# Improvement Idea Decisions

This document records RightMemory improvement ideas that were proposed during
brainstorming, along with the user's decision and rationale. Future agents
should read it before proposing more project improvements, especially when the
task is open-ended brainstorming.

Use this document as a prior, not as a ban list. Rejected ideas should not be
repeated as generic proposals, but do not invent reasons for a rejection when
the user did not give one. If a rationale is not recorded, treat the rejection
as a decision without an explained reason. Accepted entries describe the
direction that actually survived the discussion; future proposals should build
from those decisions rather than restating the rejected shape.

Add entries after the user has made a decision. Keep speculative brainstorms out
of this file until they have been accepted, rejected, or replaced by a smaller
decision.

## 2026-06-04 Brainstorm

Context: A GPT-5.5 xhigh subagent was asked to brainstorm ways to push
RightMemory forward. The parent agent filtered those ideas and presented a
shortlist. The user rejected the proposed roadmap as a set of future directions,
but accepted a smaller operational change prompted by the review-session concern.

### Accepted

#### Prolong transcript review idle cooldown to 6 hours

Decision: Prolong the default review session idle cooldown from 1 hour to 6
hours. In config terms, `[review].idle_seconds` moves from `3600` to `21600`.

Rationale: The user said this change reduces the chance of one situation raised
by the append-aware transcript review proposal: automatic review can process a
provider session before the session is truly done and later resumes.

Implementation note: The default lives in `rightmemory/config.py`, the existing
README review config example should show `idle_seconds = 21600`, and config
tests should assert the six-hour default.

### Rejected

#### Memory health and graph inspection command

Proposal: Add a read-only graph/status inspection surface that reports ids,
edges, dangling references, file counts, largest headings, connected areas, and
similar active-memory health signals.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.

#### General `rightmemory doctor`

Proposal: Expand `rightmemory doctor` beyond `doctor agent-cli` into a broad
diagnostic command for watchers, async queues, sync, profiles, locks, isolated
worktrees, validation, and recovery hints.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.

#### Prompt behavior regression fixtures

Proposal: Add model-free tests around role prompt behavior and durable
invariants, avoiding exact prompt prose snapshots.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.

#### Append-aware transcript review

Proposal: Track reviewed transcript ranges, content hashes, or turn counts so a
resumed provider session can have newly appended content reviewed after the
earlier part was already marked reviewed.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded. Related accepted decision: prolong the review idle
cooldown to 6 hours.

#### Dry-run or preview for semantic writes

Proposal: Run update, reviewer, dreamer, or pruner in an isolated worktree and
show the proposed diff/report without landing the commit.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.

#### MCP adapter over the daemon

Proposal: Wrap the existing CLI or JSON-over-stdio daemon with an MCP server.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.

#### Provider-specific expansion

Proposal: Add first-class Gemini or other provider integrations.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.

#### Local embedding index

Proposal: Add an auxiliary embedding index for larger memory recall.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.

#### UI dashboard

Proposal: Build a visual dashboard for operational state, demos, or recovery.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.

#### Hosted sync service

Proposal: Add a hosted service for memory sync.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.

#### Inline timestamps or confidence scores on every node

Proposal: Add per-node lifecycle metadata such as timestamps or confidence.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.

#### Broad edge-type expansion

Proposal: Add many more graph edge types.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.

#### Large prompt example catalogs

Proposal: Add many examples to prompts or docs so future agents have more cases
to imitate.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.

#### Legacy or fallback compatibility layers

Proposal: Add compatibility paths for older behaviors or fallback modes.

Decision: Rejected by the user in this brainstorm.

Rationale: Not recorded.
