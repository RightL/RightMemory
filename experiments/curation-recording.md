# Recording quality from historical curation batches

## Question

Do more selective recording instructions preserve useful findings while reducing task histories and guidance that a capable agent can already infer from the request, maintained instructions, or readily available project context?

The revision keeps the established domain purposes and ordinary Memory admission rules. It promotes the existing work-experience value criterion into the core, extends that criterion to guidance, and asks curators to reassess affected existing records rather than merely append evidence. A submitting agent's retention recommendation is subject to the same value judgment; an explicit user memory request remains distinct. Repeated appeals to preserve material before raw candidates disappear are removed. The submission description also asks for the information the future agent would otherwise miss.

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

## Results

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

### Limits and verification

These are development replays on one batch per domain, with iterative changes informed by the same batches. There is no held-out evaluation, repeated final-variant sampling, or evidence yet about accumulation over several update cycles. The unavailable external project resources may encourage self-contained duplication in both variants. The submission wording change needs observation on new work; frozen historical submissions cannot test it. No live memory cleanup, installation, or retrieval change is part of this experiment.

All ten outputs passed graph validation and remained within their domain's allowed paths. Eight focused replay safety tests and syntax checks passed. A sandboxed safety-test rerun hit Windows temporary-directory permission errors; the same eight tests passed outside that sandbox. Tests do not assert agent-facing wording or semantic quality.

The full repository suite ran 1,505 tests with 33 skips, four assertion failures, and two errors. All six failures/errors reproduced on an unchanged `06314c6` source snapshot using the same installed RightMemory Python environment: three Pursuit grammar assertions, one learning-inventory ordering assertion, a DeepSeek profile error, and a Windows web-process connection error. Earlier baseline runs used the system Python instead; their Windows passes do not establish the cause of the venv failure. The Windows failure's cause remains uninvestigated. The full suite included the first five safety tests; the three subsequently added masking checks were covered in the focused eight-test run.
