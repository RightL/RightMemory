# Recording quality from historical curation batches

## Question

Do more selective recording instructions preserve useful findings while reducing task histories and guidance that a capable agent can already infer from the request, maintained instructions, or readily available project context?

The revision keeps the established domain purposes and ordinary Memory admission rules. It highlights the essential purpose and selection judgment, extends the existing work-experience criterion to guidance, treats candidate wording as evidence rather than retention instructions, and removes repeated appeals to preserve material before raw candidates disappear. The submission description also asks for the information the future agent would otherwise miss.

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

The harness saves source hashes, the exact assembled prompt, initial and final inventories, final response, model usage/timing, and the actual content diff. It detects changes even if the model makes Git commits, checks the canonical domain write scope, and validates the resulting graph. Those checks establish structural correctness, not usefulness.

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

Historical input preparation and validation succeeded for all three domains, both with exact semantic state and with explicit credential masking. Eight focused safety checks passed. No replay result or quality improvement has yet been established.

The full repository suite ran 1,505 tests with 33 skips, four assertion failures, and two errors. All four assertion failures (three Pursuit grammar assertions and one learning-inventory ordering assertion) and the DeepSeek profile error reproduced on an unchanged `06314c6` source snapshot. The remaining Windows web-process connection failure did not recur in four isolated baseline runs; its cause remains unconfirmed. The eight current replay safety tests and syntax checks passed. The full suite included the first five safety tests; the three subsequently added masking checks were covered in the focused eight-test run. No assertions inspect canonical prompt wording.
