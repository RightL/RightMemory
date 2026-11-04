# Recording quality from historical curation batches

## Question

Do more selective recording instructions preserve useful findings while reducing task histories and guidance that a capable agent can already infer from the request, maintained instructions, or readily available project context?

The current revision preserves the positive purpose of learning to assist the user with similar work with less direction, judged in light of what the user was trying to accomplish. Methods, user contributions, result standards, and useful partial experiences remain part of that purpose. The added-value criterion supports selection rather than replacing the learning objective. Curators reassess affected existing records rather than merely append evidence. A submitting agent's retention recommendation remains subject to judgment, distinct from an explicit user memory request. Ordinary Memory admission rules are unchanged. Earlier revisions and their limitations are recorded below.

The historical replay evaluates curator changes. Its submissions are frozen, so it does not measure the effect of the revised submission description. It does not evaluate retrieval or apply a cleanup to live memory.

## Method

Use one actual completed batch for each domain. Recover its ordered candidate manifest and exact prepared worktree base from local task metadata and Git. Preserve historical semantic files byte-for-byte, verify every selected evidence hash, and give the two variants independent copies with matching initial-state hashes. Exclude runtime configuration, coordination state, and Git remotes.

| Domain | Prepared Memory commit | Candidate count |
| --- | --- | ---: |
| Guidance | `23d9f0e517c1d9f6d48e3e3f279e502dd195cb5d` | 13 |
| Work experience and skills | `8f87b5a1301340ac7ca758b6fce95ab4763ca055` | 11 |
| Ordinary Memory | `cdd2e0ed6474ca0d339344893bd08e3484b7a233` | 15 |

The comparison uses canonical prompt and reference files from `06314c6` versus the revised files, with the same runtime and the configured `gpt-6.1-sol` model at `xhigh`. The exact original provider version and all historical runtime conditions are not reconstructed; this is a paired replay, not a claim to reproduce the original output exactly.

Each run gets a fresh Codex thread with workspace writes, network and MCP disabled, and project-document discovery disabled. Original root instructions are supplied explicitly within a common replay boundary. Both variants are restricted to the historical copy; external project artifacts are unavailable. The operating-system write boundary does not prohibit every external read, so the read restriction also depends on the model following the replay instructions. Only fresh replay threads are closed and archived. Live claims, curation completion, publication, and sync are never invoked.

The harness saves source hashes, the exact assembled prompt, initial and final inventories, final response, model usage/timing, and the actual content diff. It detects changes even if the model makes Git commits, checks the canonical domain write scope, and validates the resulting graph. Those checks establish structural correctness, not usefulness. Prompt sources use the same newline normalization as the runtime readers; both raw and normalized hashes are recorded. Historical memory and selected evidence remain byte-exact.

## Review criteria

Before inspecting replay outputs, the intended review is:

- Guidance should avoid adding a register of individual chats' temporary permissions and delegation choices. A demonstrated mistake can still justify a concise, reusable correction.
- Work experience should preserve specific discoveries that would change later work, including the equal-endpoint interpolation failure and the legacy text-encoding finding. It should distinguish a successful small training pipeline from demonstrated quality improvement and retain useful project reference locations.
- Ordinary Memory should preserve material decisions and constraints while keeping task procedures in their owning resources. Neither new brevity nor no-change alone establishes success.
- Across domains, inspect whether updates improve the affected record as a whole, whether source details are necessary to interpret it, and whether uncertainty or scope was lost. Do not use word reduction as a substitute for this review.

## Reproduction

Run with the installed environment that supplies the configured Codex SDK. Input task metadata and all outputs are private and are not included in this repository.

```powershell
python experiments/curation_recording.py `
  --source-memory-repo <private-memory-repository> `
  --historical-task <completed-task-json> `
  --prepared-base <prepared-worktree-commit> `
  --prompt-revision 06314c6 `
  --output-dir tmp/recording-quality/<domain>-baseline `
  --run
```

For the paired run, use a new output directory, `--prompt-revision working-tree`, and `--expect-state-sha256` from the baseline manifest. Omit `--run` to prepare and validate inputs without model execution. The resulting prompt-source hashes identify the exact revised files independently of the branch name.

When masking credentials, pass the same `--redact-values-file <ignored-private-json>` to both variants. This optional file contains exact values to replace; it does not automatically discover credentials or encoded variants. Masking happens before the disposable Git history is created, and the manifest identifies affected files and whether the semantic snapshot remains byte-exact. Selected submissions remain unchanged: a matching value in candidate evidence stops preparation instead of silently altering the batch. The values file is never copied into the model's workspace.

## Initial replay results

All reported model runs use exact historical copies, with the user's explicit approval. Each domain's variants match on initial semantic state, ordered candidate proofs, task input, runtime, model, and reasoning effort. Normalized source differences are confined to the intended role prompt and domain reference.

| Domain | Baseline net words | Selected revision net words | Baseline / revised new files |
| --- | ---: | ---: | ---: |
| Guidance | +456 | -1 | 0 / 0 |
| Work experience and skills | +2,330 | +1,899 | 3 / 1 |
| Ordinary Memory | +362 | +284 | 0 / 0 |

These are changes to the whole allowed state, including deletions, measured by whitespace-separated words. They are not measures of useful information retained. The private run names for the selected revisions are `guidance-reassessed`, `skills-lesson-core`, and `memory-revised`.

### Guidance

The baseline added 456 words, including task-specific delegation choices, feature instructions, and preparation settings. The first revision added only 30 words, but those words extended the existing register of individual chats' LiteWorker bans. A second revision explicitly separated task continuation from reusable guidance; it added 78 words and still retained the new chat-only ban. Neither shorter output was sufficient evidence of success.

The selected `guidance-reassessed` revision also distinguishes a future preference from an instruction directing the current task, and reassesses existing related guidance. It removed the old chat-only ban, did not add the new one, and retained the useful correction about looking up an exact host/account before asking for access details. Its net change was minus one word and minus 71 bytes. This is better selection in this batch, not just compression.

### Work experience and skills

The baseline added 2,330 words and three files. The first revised run added 1,763 words and one file: it consolidated more evidence but still retained version inventories and task histories. A repeat with canonical newline handling added 1,201 words and no files. Those runs had the same semantic instructions; their different outputs show why a single size measurement cannot establish a stable reduction.

The selected `skills-lesson-core` revision promotes the added-value criterion into the core and makes existing related content subject to it. It added 1,899 words, 15,637 bytes, and one file: less than baseline, but more than either intermediate run. It preserved the equal-endpoint interpolation fix, strict legacy-encoding recovery, the limits of the small training run, and the misleading effect of black backgrounds on whole-image error. Useful report and code references survived.

It also retained long runtime-version tables, environment history, detailed screening policy, test totals, and run-completion machinery already owned by project resources. It removed a few old inventory details but did not turn the affected records into concise lessons. This remains a selection failure. The final wording expresses the intended criterion more directly, but this batch does not establish that promoting it into the core improves output. It must not be presented as a demonstrated fix for work-experience growth.

### Ordinary Memory

The baseline added 362 words; the revision added 284. Both reconciled obsolete deployment and specialist state, kept the constrained reasoning-effort decision, and preserved the mesh-seam finding with its untested limits. Both also retained unresolved proposals as questions rather than implemented capabilities.

The revised output kept a particular visual choice scoped to that comparison, whereas the baseline inferred a broader visual priority. It still added a source-code implementation detail already recoverable from the project and rewrote an unrelated open question. The change therefore shows a modest benefit, not a solution to Memory growth or unnecessary editing.

## Correction after intent review

The user identified that the initial revision overfocused on the redundancy concern and weakened earlier intent. In particular, promoting the added-value criterion into the skills core displaced the positive learning purpose, and the generic future-agent wording did not sufficiently connect the lesson to what the user was doing. The correction restores that purpose and the original wording about methods, judgment, user contributions, success criteria, resources, and uncertainty. Useful interactions may again be retained before their broader lesson is clear. Added-value checks remain in Judgment, without a presumed majority-rejection outcome for guidance or skills. Existing-record reassessment remains.

The submission description is corrected in the same direction. No case-specific rule excludes the experiences of agents the user builds; usefulness depends on the user's objective and the future reader. There is no storage or retrieval change.

The `guidance-user-purpose-1` / `-2` and `skills-user-purpose-1` / `-2` runs repeat the exact original batches twice per changed domain. The configured model and effort remain unchanged. Ordinary Memory is not rerun because its assembled instructions have not changed. Source hashes and frozen-state checks distinguish the new variants from the earlier selected revisions.

Before inspecting these outputs, review will check whether they preserve the user's first-frame training choice, the distinction between a working pipeline and demonstrated quality, useful interpolation and encoding discoveries, and uncertainty about unfinished data preparation. Guidance should preserve useful user context without turning a chat-only delegation choice into continuing guidance. A fuller record is not a failure merely because it is longer; copied inventories and routine histories still need independent value. These batches have limited coverage of learning for an agent builder versus learning for a created agent.

All four runs completed with exact initial-state and candidate matches, the same model/effort, valid graphs, and no out-of-domain writes. Within each new pair, prompt-source hashes are identical. Compared with `guidance-reassessed` and `skills-lesson-core`, the normalized source changes are confined to the corresponding domain rules. Execution logic is unchanged; the changed submission-description string is not part of the frozen curator inputs.

| Run | Net words | Net bytes | New files | Changed files | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| `guidance-user-purpose-1` | +115 | +956 | 0 | 1 | 252.9 |
| `guidance-user-purpose-2` | +101 | +859 | 0 | 1 | 271.2 |
| `skills-user-purpose-1` | +2,394 | +19,299 | 2 | 5 | 331.8 |
| `skills-user-purpose-2` | +2,073 | +16,781 | 1 | 3 | 343.7 |

Both guidance runs kept the useful exact-host access correction and its supporting circumstances. Both also extended the register of individual conversations' LiteWorker stop instructions. They correctly avoided inferring a general delegation ban, but continued to record chat-specific continuation instructions already governed by the conversation and ordinary instruction following. These outputs regress against the earlier `guidance-reassessed` result, which removed that register. This comparison does not isolate wording effects from model variation; the boundary remains unreliable.

Skills run 1 preserves the user's first-frame-plus-text choice, the separate source-screening correction, and the distinction between a working training pipeline and demonstrated adaptation quality. It retains the interpolation and legacy-encoding findings with useful qualifications. However, it creates two substantial case documents and adds environment inventories, validation counts, observer history, and detailed screening procedure alongside links to their project resources. More explicit purpose has not by itself resolved duplication or growth. Its useful findings also appeared in the earlier variants, so retaining them is not proof of an improvement over those variants.

Skills run 2 consolidates the training evidence into the existing case and organizes the validation method into four stages, making the useful procedure easier to follow. It preserves the user's choices, the important discoveries, and the limits of partial results. Independent review found no clear factual error or important scope loss in the inspected changes. The runtime case still copies installed versions and historical deployment details; the other cases still carry observer history and test-count inventories. It preserves useful coverage and gives one clearer method presentation, without demonstrating a reliable improvement over `skills-lesson-core` in what gets selected.

The corrected wording restores the intended learning purpose, but these simulations do not demonstrate a reliable improvement in recording selection. They do not justify treating fewer words as the objective or making another untested change to the core merely to shrink these outputs.

## Limits and verification

These are development replays on one batch per domain, with iterative changes informed by the same batches. The corrected guidance and skills variants have two runs each; earlier variants generally have one. There is no held-out evaluation or evidence yet about accumulation over several update cycles. The unavailable external project resources may encourage self-contained duplication in all variants. The submission wording change needs observation on new work; frozen historical submissions cannot test it. No live memory cleanup, installation, or retrieval change is part of this experiment.

All fourteen outputs passed graph validation and remained within their domain's allowed paths. Eight focused replay safety tests passed during the initial implementation. The intent correction changes agent-facing text only; syntax compilation and whitespace checks passed, and the full suite was not rerun. A sandboxed safety-test rerun during the initial implementation hit Windows temporary-directory permission errors; the same eight tests passed outside that sandbox. Tests do not assert agent-facing wording or semantic quality.

The full repository suite ran 1,505 tests with 33 skips, four assertion failures, and two errors. All six failures/errors reproduced on an unchanged `06314c6` source snapshot using the same installed RightMemory Python environment: three Pursuit grammar assertions, one learning-inventory ordering assertion, a DeepSeek profile error, and a Windows web-process connection error. Earlier baseline runs used the system Python instead; their Windows passes do not establish the cause of the venv failure. The Windows failure's cause remains uninvestigated. The full suite included the first five safety tests; the three subsequently added masking checks were covered in the focused eight-test run.
