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

## 2026-06-16 Memory Cleanup Brainstorm

Context: The user inspected `~/.rightmemory` and observed that active memory
contains too much low-value material: generated artifact inventories, transient
experiment rows, git-history breadcrumbs, stale runtime state, duplicated
project documentation, over-specific preferences, and reflection logs that are
not active operating memory. The discussion focused on whether this is a
Dreamer problem, a missing Cleaner role, or an intake-routing problem.

### Observed Current Memory Problems

The inspection found several recurring classes of low-value active memory:

- Generated artifact inventories: entries that mainly list GLB folders,
  `results/`, `outputs/`, manifests, contact sheets, checkpoint files, log
  folders, stopped container names, hashes, or other generated artifacts. These
  are usually better kept in run manifests, output directories, or project docs.
- Git-history breadcrumbs: entries that mainly record commit hashes, branch
  names, worktree names, verification before a commit, or implementation
  chronology that normal Git history already stores.
- Raw experiment rows and parameter sweeps: entries that preserve exact
  settings, timings, counts, and intermediate ablation rows. These should
  usually compress into the durable conclusion, current best setting, rejected
  direction, and a pointer to the project report.
- Over-detailed rejected-experiment graveyards: negative results are useful,
  but many entries keep too much tactical detail. A better active-memory shape
  is the rejected family, the reason it failed, and a report pointer.
- Transient environment and runtime state: cache presence, one-time Docker
  states, stopped containers, stale worker details, uninstalled tools, or
  current branch cleanliness can become wrong quickly. Keep only reusable
  troubleshooting patterns or current invariants.
- Duplicated project documentation: memory repeats README, runbook,
  `DESIGN_NOTES.md`, specs, or project-local workflow details that are easy to
  rediscover by inspecting the repo. Global memory should keep at most the
  durable decision or pointer.
- Over-specific preference memories: some entries preserve one incident in too
  much detail when a broader principle already exists. These should merge into
  principle-level preferences when possible.
- Insight-style essays in the active recall surface: reflective logs can be
  valuable, but they should not behave like ordinary operational memory unless
  they were explicitly distilled into a durable rule, risk, or decision.
- Stale open questions: unresolved questions can remain after they become
  answered, obsolete, or too speculative. Dreamer should keep this section
  compact and current.

The general diagnosis is that RightMemory is being used as active memory,
experiment notebook, artifact index, git log, project documentation mirror, and
reflection journal at the same time. Cleanup should separate those functions
without losing hard-to-reproduce conclusions.

### Deferred

#### New Cleaner role

Proposal: Add a separate `cleaner` role that would specialize in removing,
compressing, or relocating low-value active memory, leaving Dreamer to focus on
organization and consolidation.

Decision: Deferred. Do not implement a new Cleaner role yet.

Rationale: A separate role sounds clean, but it risks splitting responsibility
too finely. Dreamer would organize memory, Cleaner would judge memory quality,
Pruner would age memory out, and the boundaries could become fuzzy. The
discussion did not establish that a new role is the main missing piece.

Preferred direction if revisited: first try making Dreamer more staged and
cleanup-aware before adding another role.

#### Dreamer staged cleanup

Proposal: Keep one Dreamer role, but make its cleanup process more explicit and
progressive:

1. Organize pass: improve readability, merge duplicates, move large detail into
   `MEMORY_<slug>.md`, fix graph edges, and compress long traces.
2. Triage pass: classify suspicious active memory as keep-global,
   compress-to-pointer, likely project-local, graveyard, delete-later, or open
   question.
3. Cleanup pass: apply safe actions directly and move borderline material into
   a graveyard before deletion.

Decision: Deferred, but this is the favored cleanup direction over creating a
new Cleaner role.

Rationale: The user chose a conservative cleanup stance first, then noted that
having a graveyard makes automatic cleanup safer and allows Dreamer to be more
assertive without requiring a manual operator hint for every cleanup cycle. The
important behavior is not dramatic deletion; it is shrinking and clarifying the
active surface over time while preserving a reversible path.

Design notes for later:

- Dreamer should clean existing accumulated noise. It should not inspect project
  folders or decide where to write project-local documentation.
- Direct deletion should stay narrow: duplicate graph junk, exact duplicate
  memory, or clearly obsolete material.
- Low-value but nontrivial material should usually be compressed or moved to a
  graveyard first.
- Project-local-looking memory can be compressed to a pointer or marked as a
  locality issue, but Dreamer should not try to move it into project files.
- Actual removal from graveyard can be handled later by a repeat-cycle rule,
  pruner interaction, or explicit future design.

#### Memory locality and intake policy

Proposal: Strengthen `memory-orchestrator` so intake decides whether a fact
belongs in global RightMemory, a project-local file, a project profile, or
nowhere. This would prevent future low-value project-discoverable facts from
being submitted to global memory.

Decision: Deferred as a separate, more complex problem.

Rationale: The user identified this as important but explicitly scoped it out
of the current cleanup discussion. The current focus is existing accumulated
noise, not the future intake-routing policy.

Design notes for later:

- Some memories are easy to rediscover by exploring the project folder, reading
  repo docs, or checking git history. Those should usually not become global
  RightMemory updates.
- Some memories are hard to rediscover because they encode user preference,
  cross-project behavior, subtle local pitfalls, or hard-won conclusions. Those
  are better global-memory candidates.
- The orchestrator-side policy may eventually route information to project docs,
  project profiles, global memory, or no durable storage.
- Dreamer should only clean active memory. Intake locality belongs to
  `memory-orchestrator` and later update-routing design.

## 2026-06-23 Active Memory Budget And Role Split Brainstorm

Context: The user revisited active-memory cleanup after observing that
retrieve becomes too slow when memory grows, and that accumulated active memory
contains too much duplicate, project-local, stale, or low-value material. The
discussion replaced the earlier generation-count-only pruning idea with an
active-memory budget model.

### Accepted Direction

#### Size-budget active memory pruning

Proposal: Bound active memory by the combined estimated size of all active
`MEMORY*.md` files. When active memory size $S_{\text{active}}$ exceeds a
configured maximum $B_{\max}$, run budget maintenance until the active surface
falls to about $\frac{3}{4}B_{\max}$. The comparison boundary should be chosen
from Git history by finding a memory-affecting commit where active memory size
was near that target, then pruning candidates can be selected similarly to the
current pruner: material unchanged since that boundary is older and less
reinforced than material added or changed afterward.

Decision: Accepted as the preferred direction to explore next. Prefer this over
making prune due solely after a fixed number of active-memory commits.

Rationale: Size is the actual pressure on retrieve latency and model context.
Git history still gives a simple aging signal, but commit count should select a
historical comparison boundary rather than be the primary trigger.

Design notes for later:

- Use a deterministic estimated-token budget. Exact provider tokenizer counts
  are not required for budget pressure; a stable estimate is sufficient. The
  default good-enough estimator can count Latin/ASCII characters, CJK
  characters, and other Unicode characters, then compute:

  $$
  T_{\text{est}} =
  \left\lceil
  \frac{C_{\text{latin}}}{4}
  + C_{\text{cjk}}
  + \frac{C_{\text{other}}}{2}
  \right\rceil
  $$

  Here $C_{\text{latin}}$ includes ordinary ASCII/Latin text, digits,
  whitespace, and Markdown syntax; $C_{\text{cjk}}$ includes Chinese,
  Japanese, and Korean characters; and $C_{\text{other}}$ includes remaining
  Unicode characters. This is a stable pruning pressure metric, not exact model
  billing-token accounting.
- The target after maintenance is intentionally below the maximum to avoid
  rerunning maintenance immediately after the next small update.
- Material deleted from active memory, and detail lost during compression, should
  be preserved in colder historical storage, potentially a later RAG-backed
  archive.
- Historical archive recall should not automatically reactivate facts into
  active memory. Reactivation should still go through ordinary memory update.

#### Split organizer, compactor, and pruner responsibilities

Proposal: Separate active-memory maintenance into three clear responsibilities:
an organizer, a compactor, and a pruner. The current Dreamer role mixes
organization and compression, so it may need to be renamed or narrowed.

Decision: Accepted as the role split direction.

Rationale: Cleanup has become too broad for one semantic role. Organizing the
memory tree, compressing bulky content, and deleting budget-expired content
have different success criteria and should not be blurred.

Design notes for later:

- The organizer role owns active-memory structure: headings, ids, graph edges,
  duplicate merging, stale open questions, and moving facts to clearer local
  positions. This may be the current Dreamer narrowed in scope, or Dreamer may
  be renamed if "dreaming" no longer describes the role.
- The compactor role owns meaning-preserving compression. It should shrink
  bulky active content into concise summaries and write archive records for
  details that leave the active surface.
- The pruner role owns budget enforcement and deletion only. It should select
  candidates from the size boundary and reinforcement logic, but it should not
  perform semantic compaction itself.
- Pruner may decide that an item should be compacted instead of deleted, but the
  compaction work belongs to the compactor role.
- Keep the division visible in prompts, tests, commit subjects, and final
  reports so future changes do not silently merge the roles again.

## 2026-06-16 Hub Transport Brainstorm

Context: The user questioned whether the planned HTTP Shared View Hub is needed
if Git cannot make only selected branches visible, and proposed using encrypted
Git branches or encrypted Git-hosted artifacts as a way to reuse GitHub, GitLab,
or similar widely available public-network infrastructure.

### Deferred

#### Encrypted Git branch transport for shared views

Proposal: Publish encrypted shared-view snapshots through a normal Git remote,
potentially using a branch, tag, or artifact-style commit as the transport. A
consumer would accept an invitation containing the Git location plus decryption
material or a local key reference, fetch the encrypted snapshot, decrypt it
locally, and retrieve from the local decrypted cache.

Decision: Deferred by the user. Do not implement this yet.

Rationale: GitHub, GitLab, and similar Git hosts are already public-network
infrastructure that many users can access without running a VPS, reverse proxy,
HTTPS setup, or always-on provider machine. This makes encrypted Git transport
appealing as a low-friction internet-capable mode.

Design note: Treat this as a possible shared-view transport adapter, not as a
settled replacement for the HTTP Shared View Hub. The HTTP hub still covers
stable invitations, accepted-connection tokens, inbox interactions, audit
records, revocation workflows, immutable published versions, and server-side
hosted retrieval. Encrypted Git transport would likely trade those richer hub
semantics for simpler deployment and should be evaluated honestly if revisited.

## 2026-06-16 Shared View Redesign Brainstorm

Context: The user reviewed the current shared-view design and concluded that it
conflates file sharing, provider-root filesystem access, hosted retrieval, and
provider-side questions. The accepted direction is to split shared views into
HTTP-only mirrored file views and provider question views, with no direct
provider-root reads.

### Deferred

#### Bidirectional shared-view interactions

Proposal: Turn shared-view interactions into a bidirectional conversation or
threading system so providers can reply to consumer notes and consumers can
track follow-up state.

Decision: Deferred. Do not implement bidirectional interactions as part of the
`MF#` / `MQ#` redesign.

Rationale: The redesigned model separates live provider answers from async
feedback. `MQ#` handles synchronous provider questions, while `shared-view note`
remains explicit one-way consumer-to-provider feedback. Adding replies,
threads, or queued answers would blur that boundary and move RightMemory toward
a chat/task system before the basic shared-view split is clean.

Design note: Revisit this later as its own product design. The open question is
whether provider responses should be modeled as threaded interactions, ordinary
provider view updates, explicit follow-up notes, or a separate collaboration
surface.

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
