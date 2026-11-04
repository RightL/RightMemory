# Installed maintenance and explicit cleanup instructions

## Question

How does the installed maintenance workflow behave with a minimal request, and can explicit cleanup instructions improve accumulated memory? A size reduction alone does not establish a better memory.

The first experiments supplied substantial additional deletion and compaction instructions. They are an enhanced-prompt variant, not a baseline of the existing skills or installed maintenance behavior. The user identified this validity problem, prompting a separate installed, minimal-request baseline. The variants also use different canonical prompt versions and invocation paths; their differences cannot be attributed to the added scope alone.

## Frozen setup

The experiment uses the clean committed current Memory state at `bef7d40f6080f1cc99bb390846e0ab448a37e223`, captured on November 4, 2026. This is a new snapshot of current state, not an output of the earlier historical update replays. Three independent disposable copies share the same initial semantic hash. Pending candidates and coordination/runtime state are omitted; maintenance does not process submissions.

The enhanced variant uses a synthetic production-shaped input with `mode=maintain`, an empty candidate list, and a whole-domain scope. Beyond the user's deletion emphasis, that scope adds judgments about independent future value, rewriting useful material, preserving decisions and uncertainty, and giving existing text no presumption of value. It sets no deletion quota, but it is still additional behavioral guidance.

All model runs explicitly use `gpt-6-astra` with `xhigh` effort. The enhanced variant's initial prompt revision is `538aa7b`, the corrected local branch from the intent review. The installed baseline uses the installed package's actual definitions, which differ from that branch. Each baseline run retains exact copies and hashes of the installed definitions and the generated native task instructions.

| Domain | Files before | Words before | Bytes before | Private run name |
| --- | ---: | ---: | ---: | --- |
| Work experience and skills | 27 | 21,946 | 179,709 | `maintenance-skills-astra-current` |
| Guidance and correction cases | 3 | 5,722 | 44,819 | `maintenance-guidance-astra-current` |
| Ordinary Memory | 3 | 12,271 | 109,831 | `maintenance-memory-astra-current` |

Counts cover each domain's writable content, not the entire copy. Words are whitespace-separated counts. Pursuit is read-only.

## Skill and prompt paths

The installed entry skills are `maintain-work-skills`, `maintain-agent-guidance`, and `maintain-rightmemory`. Their files match `538aa7b`; the installed three curator prompts and three domain-core references do not. The skills prepare an isolated maintenance task and direct the agent to its returned instructions; completion publishes the result. They allow delegating that native task to another agent.

For the installed baseline, the coordinating agent reads the skills and installed domain references, then runs the actual installed `curate begin` command with `--mode maintain --scope "all existing content"`. Astra receives the returned native instructions without added deletion or compaction directions. Each domain has an independent frozen copy, synchronization disabled, no remote, and no pending evidence. The native completion command is authorized to publish only into that disposable root. The output records actual completion status and preserves any unfinished candidate separately. The source root's own `AGENTS.md` is retained; execution restrictions prevent live-root changes and external service access.

The baseline uses short temporary root paths because deeply nested experiment paths exceed Windows Git limits. Its installed SDK/runtime package versions are both `0.163.0`, with matching binary hashes across runs. A process-local binary-resolution workaround uses the installed pinned executable and helper directory without editing the installation. Installed definition hashes agree across the three runs and are checked again at completion. External project artifacts remain unavailable.

The working instructions come from `skills-curator.md`, `guidance-curator.md`, or `update.md`, the corresponding shared domain rules, schema and applicable references, and the maintenance scope. The installed `SKILL.md` wrapper is followed by the coordinating agent, rather than independently injected into Astra's delegated task. The ordinary Memory skill already explicitly mentions removing trivial or misleading context. The role prompts also permit removal while retaining "smallest coherent change" wording.

The enhanced variant uses `experiments/curation_recording.py` directly. It assembles the canonical prompt without the native begin/completion lifecycle and adds the stronger scope. The baseline instead uses installed native preparation/completion and the harness's existing isolated model runner through a private orchestration script. No production executable code changed. Both paths record real content changes, use fresh model threads, disable MCP/network access, and constrain writes to disposable state. Read confinement additionally relies on instructions.

## Review

Inspect whole-record removals and the resulting content, not merely edited line counts. Relevant preservation checks include the user's agent-building and evaluation intent, meaningful methods and partial experiences, accepted project decisions, qualified experimental conclusions, useful resource locations, and cross-document references. Routine task histories, repeated explanations, stale inventories, and already-owned project detail do not gain value merely by already being stored.

The root agent reviews ordinary Memory and Pursuit directly. Other reviewers may inspect experiences and guidance. Reports distinguish observed omissions from demonstrated loss of important meaning; unavailable project resources limit claims that a removed detail remains recoverable elsewhere.

## Installed minimal-request baseline

`maintenance-skills-astra-installed-minimal`, `maintenance-guidance-astra-installed-minimal`, and `maintenance-memory-astra-installed-minimal` completed with no whole-record deletions. Their only requested content scope was **all existing content**. Each starts with the same semantic hash as the enhanced runs, `93381e1c2baf40430a40521823a573b401b51d663b35d2b34575f4622da4b625`.

| Domain | Words before → after | Result |
| --- | ---: | --- |
| Work experience and skills | 21,946 → 21,946 | Explicit no-change judgment after reviewing 25 experiences and two skills |
| Guidance and correction cases | 5,722 → 5,783 | Narrow consistency repairs; every case retained |
| Ordinary Memory | 12,271 → 12,342 | Narrow reconciliation and some paragraph shortening; three nodes added, none removed |

The guidance edits usefully correct an unconditional upstream-pull rule and an overbroad worktree-directory convention. Ordinary Memory incorporates later H3 dark/static/color decisions, the user's near-static interpretation, an unresolved timestamp-unit question, and the shared results-browser locator. These additions are not evidence of useless growth: they correct real stale or missing context. They also leave the accumulated collection essentially intact. The root reviewed the full ordinary Memory diff and its relevant source guidance; the other reviewer reviewed the full guidance diff. Skills had no semantic diff.

All model turns finished, but sandbox restrictions prevented Git metadata writes and native completion. After reviewing the candidates, the root agent committed exactly the model-authored changes and retried the supplied completion commands inside the disposable roots. All three now have completed receipts and published inventories matching their candidates exactly. The operator made no content edits. Initial failed model publication status is preserved alongside the retry receipts, rather than counted as an autonomous end-to-end success.

This baseline also exposes another weakness of the first enhanced output: its heavily compacted ordinary Memory retains the superseded H3 initial screening policy, while its guidance/skills outputs retain later decisions. The minimal Memory run correctly reconciles that discrepancy. Shortening and factual maintenance therefore need separate assessment.

## Paired maintenance-purpose change

Revision `0628650` puts the cleanup direction into the three maintained role prompts, rather than the user's experiment request:

> **Maintenance reassesses what should remain within the requested scope against the shared core.** Use deletion and compaction as primary actions, preserving the meaning needed for future assistance.

It also limits "smallest coherent change" guidance to updates. The shared learning cores remain unchanged. The earlier experimental incoming-reference and user-choice guard sentences are removed from the current role prompts; their attempts and limitations remain recorded below.

`maintenance-<domain>-astra-maintain-purpose` pairs this change with the installed minimal baseline. It starts from the same exact frozen state, uses the same `all existing content` request, Astra/xhigh, installed shared cores and other instructions, native lifecycle, and SDK/runtime `0.163.0`. The experiment applies only the maintenance paragraph and update-only smallest-change wording to the role text in the generated native instructions, leaving the actual installation untouched. It saves both the original generated instructions and the exact role diff. Prompt comparison after normalizing task/root paths confirms that other instructions are unchanged.

The paired runs completed their model turns and produced the following raw candidates:

| Domain | Initial words | Installed minimal baseline | Maintenance-purpose candidate | Reduction from initial |
| --- | ---: | ---: | ---: | ---: |
| Work experience and skills | 21,946 | 21,946 | 18,435 | 16.0% |
| Guidance and correction cases | 5,722 | 5,783 | 4,844 | 15.3% |
| Ordinary Memory | 12,271 | 12,342 | 9,284 | 24.3% |

The change elicits real compaction with a minimal request: 7,376 fewer words across the three domains, or 18.5% of the original collection. Skills rewrites four experiences, principally removing repeated chronology, inventories, and test ledgers. Guidance removes several whole subordinate examples but retains all fifteen broad correction patterns. Ordinary Memory removes or merges fourteen node records and adds a user-context heading. No whole file is deleted. This is a bounded improvement, not the aggressive experience-level selection originally expected: all 25 experience files and both skills remain, along with some chat-specific permission history.

The reviewed methods and decisions generally survive, including assignment-based specialist context, the backpack scope error, tool-owned interactions, matched-input evaluation limits, and caller contracts. Ordinary Memory now reconciles the later H3 screening policy while compacting it, and the CloudPlatform bilingual partial-output choice remains in both ordinary context and the retained correction case. The root reviewed changed ordinary Memory text and removed-node meaning; the second reviewer read all four changed skills records, both changed guidance files, and their removed passages.

Review rejected one deletion. The guidance candidate dropped the user's clarification that lettering lessons are removable correction records, and that the design should accommodate other scenarios and contributed skills without claiming a settled replacement architecture. A bounded trace located the October 14 point-cloud harness discussion and found no explicit later supersession. The idea was absent from the other retained domains. Later project history is not exhaustively verified, so review conservatively restored the original 33-word case rather than accepting its loss.

Raw Astra candidates remain unchanged for scoring. The reviewed guidance copy contains 4,877 words; the reviewed combined collection contains 32,596 words, 18.4% less than the original. `review-adjustments.diff` records the sole operator content change. It is not evidence that Astra preserved that choice unaided, and no case-specific safeguard was added to the maintained prompt.

All three model turns again encountered Git metadata restrictions. After review, the root agent committed the candidates and retried the native completion commands inside their disposable roots. All tasks have completed receipts. A combined reviewed copy passes canonical validation (251 IDs across ten graph files); no removed Memory ID remains mentioned in its Markdown, and no files were deleted. Structural checks do not certify meaning or external project links.

These runs test the narrow maintenance change against the installed definitions, not every earlier recording-quality change on the local branch together. They provide one paired observation per domain, not a repeatability estimate. No live memory or installation received these changes.

## Enhanced-prompt results

The initial three Astra runs completed. Each passed the canonical graph validator and changed only its permitted domain. Across the three domains, stored text fell from 39,939 to 18,075 whitespace-separated words, a 54.7% reduction. This measures stored volume, not the percentage of memory that was useless.

| Domain | Words before → after | Reduction | Whole-record changes |
| --- | ---: | ---: | --- |
| Work experience and skills | 21,946 → 9,551 | 56.5% | Five experience files deleted; two of these were merged into retained experiences |
| Guidance and correction cases | 5,722 → 1,785 | 68.8% | Removed whole correction examples and the accumulated permission/delegation register |
| Ordinary Memory | 12,271 → 6,739 | 45.1% | 46 existing graph IDs removed, one added; many removed entries were consolidated into retained entries |

The ordinary Memory index went from 211 to 166 addressable items. The removed IDs represent 45 nodes and one heading. One existing node became a heading, and a new node consolidates the user's algorithm-development responsibilities. These counts distinguish deletion of records from discarded meaning; they are not 46 independently proven useless facts. No Pursuit changes occurred.

The cleanup does more than shorten sentences. It removes one-off repair and execution histories, redundant runtime inventories, and duplicate project reports. The skills run removes three reports primarily covered by project or ordinary Memory records, and merges the worker-status and domain-learning-output records into the delegation and recovery experiences. Guidance removes examples that no longer supply an independent correction.

Useful distinctions survived the reviewed outputs. The delegation experience retains the user's aim of reducing reading for both main and specialists according to their assignments, along with the observed back-to-backpack scope error. Mesh-assisted work retains the limited acceptance of curves without point sampling or fill. Ordinary Memory preserves the separation between agent-building choices and application-agent behavior, H3 movement versus the separate fixed-position workflow, incomplete training evidence, caller ownership/partial-result contracts, and unmerged research versus deployed code. It retains the H3 Native-label correction and the user's text-only feedback choice even though their duplicate experience files were deleted.

The review also found a meaningful loss: ordinary Memory and guidance both removed the user's CloudPlatform choice to return every safely parsed description or bilingual keyword, permit unequal keyword counts, and avoid guessing incomplete output. A future agent could reasonably default to the stricter behavior the user rejected. The resulting memory loses that project-specific decision; uninspected project code does not establish its preservation elsewhere. This is an observed combined-output omission, not proof that either curator relied on the other retaining it. A compact ordinary Memory contract would retain the useful choice while allowing the duplicate guidance example to disappear.

The root reviewed ordinary Memory before/after and removed-node text; a separate reviewer inspected all resulting guidance, all removed correction cases, all five deleted experience originals, eleven retained experiences, both retained skills, and selected original records/diffs. External project artifacts and the remaining nine experiences were not comprehensively reviewed. A project report being the appropriate source does not prove that every removed detail is recoverable from it without checking that report.

### Incoming-reference defect and targeted iteration

Combining the three outputs in a fourth disposable directory exposed a real defect: ordinary Memory still points to the deleted Quad comparison experience. The same incoming reference was available during skills maintenance. The canonical graph validator passed because this is a plain-text resource path, not a graph edge. The skills model's final claim that it found no broken local links was therefore too broad.

The deletion judgment itself was reasonable: the removed record mainly indexed a project-owned comparison and its historical checks. The failure was leaving a dependent reader without a usable target and failing to report that dependency. Preserving the entire report solely because it has an incoming reference would retain the duplication.

Revision `5433b24` adds this operational guidance to `skills-curator.md`, leaving the learning core, maintenance scope, and other prompts unchanged:

> Check incoming references when moving or deleting resources. Keep them usable within the allowed paths; when another domain needs an edit, retain a compact, useful target and report the remaining change.

`maintenance-skills-astra-references` reruns skills maintenance from the same frozen initial state with Astra at xhigh. The scope and inputs are byte-identical to the first skills run. It is a full fresh maintenance pass, not a repair turn given the first result. It reduced skills from 21,946 to 8,280 words (62.3%) and deleted six files, but **the reference correction failed**: it still deletes the Quad target and additionally deletes a point-native comparison experience referenced from ordinary Memory. The model again overstates its local-link validation. The comparison's meaningful conclusion survives in ordinary Memory, but the pointer is unusable. Combined guidance/experience output also contains stale section locators and a claim that a removed guidance preference remains there.

Revision `c2d9961` added a separate experimental Memory edit-planning bullet distinguishing consequential user choices from implementation detail. Its intended replay, `maintenance-memory-astra-choices`, failed before model execution because the installed runtime package initializer was temporarily unavailable during another task's upgrade. It made no semantic changes and supplies no evidence about that wording. Further enhanced runs were deferred to establish the minimal baseline first. Both experimental guard additions were superseded by `0628650`; neither is part of the final maintenance-purpose change.

The initial combined output and its recorded defect remain unchanged for comparison. No live memory or installed runtime has been changed by this experiment.

### What this establishes

The enhanced request can elicit substantial deletion while retaining many useful distinctions, but it also produces meaningful over-deletion and broken dependencies. The existing "smallest coherent change" wording did not prevent a large cleanup under that request. These observations do not justify weakening the learning purpose merely to force further shrinkage, and they do not establish that the installed maintenance skills clean up this way unaided.

The enhanced runs explicitly requested deletion as a primary action; the installed baseline does not. Their canonical definitions and invocation paths also differ, and an installed runtime upgrade occurred between them. Compared with the earlier historical update experiments, the mode, model, and source state differ as well. These separate effects are not isolated. One pass cannot establish stable long-term size, zero useful loss, or improved retrieval quality.

The controlled baseline/purpose pair is stronger evidence for the narrow maintained-prompt change: state, request, model, installed cores, runtime and execution configuration match. It shows that explicit maintenance purpose improves cleanup, but mostly through compaction. Selecting fewer experiences and preserving consequential user intent remain unresolved judgment problems. Stronger deletion language alone is not established as the remedy; the earlier enhanced outputs are evidence of the losses that can accompany more aggressive shrinkage.

## Reproduction

For the installed baseline, first prepare a byte-exact disposable copy of the frozen semantic state, preserve its root instructions, disable synchronization, and remove remote configuration. Follow the installed maintenance skill and use:

```powershell
rightmemory curate --root <disposable-root> begin `
  --domain <memory|guidance|skills> --mode maintain --scope "all existing content"
```

Delegate the returned native instructions to Astra at xhigh within that copy, using the returned completion command only for the disposable root. Retain the native manifest, exact generated prompt, before/after inventories, completion receipt, and any unfinished candidate. Private outputs use the `maintenance-<domain>-astra-installed-minimal` names.

For the paired purpose experiment, start again from the identical frozen state and same minimal request. Apply only the maintained maintenance paragraph and update-only smallest-change wording to the role portion of the generated native instructions. Keep the installed shared references and native control text unchanged. Save `prompt-original.txt`, `prompt.txt`, `effective-role.md`, and `role-patch.diff`; normalize only root paths and task IDs when comparing the original generated prompt to its baseline. Private output names are `maintenance-<domain>-astra-maintain-purpose`. The private orchestration script, full model outputs, inventories and audit files are retained with the experiment evidence.

For the enhanced variant, create an ignored private JSON input with an `input` object containing `domain`, `mode: "maintain"`, the additional cleanup `scope`, and `candidates: []`. This is a synthetic experiment request, not a historical execution record. Use a new output directory per run:

```powershell
python experiments/curation_recording.py `
  --source-memory-repo <private-current-memory-repository> `
  --historical-task <private-maintenance-input-json> `
  --prepared-base bef7d40f6080f1cc99bb390846e0ab448a37e223 `
  --prompt-revision 538aa7b `
  --output-dir tmp/recording-quality/maintenance-<domain>-astra-current `
  --model gpt-6-astra --reasoning-effort xhigh --run
```

The CLI flag retains its historical-replay name, but its input validator supports both Update and Maintain. Private inputs, full memory, outputs, and diffs are not committed or published.
