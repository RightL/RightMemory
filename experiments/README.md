# Retrieval experiments

[retrieval_embeddings.py](retrieval_embeddings.py) runs an isolated benchmark on disposable copies of Memory using the canonical graph index and renderer. Production code, prompts, installation, and dependencies are unchanged.

## Embedding retrieval — 2026-09-28

**Result: embedding search is fast, but the tested shortlist followed by the current selector does not improve overall retrieval latency.** Median latency was similar; the mean and slow cases were worse. Direct embedding results are much faster but miss some expected references and have no no-match decision.

### End-to-end comparison

The main table uses the second selector pass, including live Windows-to-lzt242 requests over SSH, GPU search, local rendering, runtime setup/cleanup, and the selector where applicable. Model/index startup is excluded. Each selector row has 32 requests. Direct retrieval has three repetitions per query, or 96 timing samples, with unchanged reference coverage.

| Path | Mean seconds | Median seconds | 95th percentile seconds | All expected references | Correct no-match |
| --- | ---: | ---: | ---: | ---: | ---: |
| Current retriever | 13.460 | 11.832 | 25.613 | 27 of 28 | 4 of 4 |
| 0.6B embeddings, top ten, then current selector | 19.750 | 11.511 | 51.869 | 27 of 28 | 4 of 4 |
| 0.6B embeddings, top ten, directly rendered | 0.092 | 0.089 | 0.101 | 25 of 28 | 0 of 4 |

The assisted selector's median improved by about 2.7%, while its mean increased by about 46.7%. Its longest request took 90.07 seconds for a historical broad-context query, versus 18.45 seconds in the baseline. The 95th percentile uses linear interpolation of sorted times.

The first pass had the same timing direction: baseline mean/median were 14.566/13.101 seconds; assisted mean/median were 20.422/12.704 seconds. Expected-reference coverage was 28 of 28 versus 27 of 28; both rejected all four no-answer queries. This exploratory pass partly overlapped repository tests and used saved rankings. The second pass ran after the tests and included live GPU transport. All 128 selector calls completed without runtime errors.

### Accuracy interpretation

The frozen corpus contains 246 indexed entries; 178 substantive entries are scored. The 32 questions include 28 with expected matches and four with no stored answer. They cover facts, applicable guidance, changed decisions, multiple required facts, six Chinese queries, and one historical retrieval request. Some are related or translated, so they are not 32 independent topics.

Expected and optional source IDs were authored by the investigating agent and frozen before retrieval. They have not been independently human-reviewed. The numbers measure reference coverage, not independently judged answer quality.

Both embedding models missed the same three required IDs at ten candidates: a source-update rule explicitly requested in a multi-context query, and two correction entries in Chinese queries about partial results and animation. The latter two queries retrieved factual memories with overlapping guidance. Missing those correction IDs does not alone prove an incorrect factual answer.

The selector can read beyond its shortlist and sometimes recovered missing references. It also varied between repetitions: the baseline omitted one correction in its second pass; the assisted path omitted different corrections in its two passes. These small differences and provisional labels do not establish general accuracy parity or degradation.

Selecting a heading expands its subtree, so ten ranked entries can produce more than ten delivered items. Direct retrieval returned 12.09 scored items on average, including 10.09 outside the required/optional labels; its largest output had 58,601 characters. The unrelated dentist query expanded to 132 scored items. No rejection threshold was calibrated. The false positives describe the tested fixed-count path, not a limitation proved for every embedding design.

The second-pass current and assisted selectors returned means of 3.09 and 2.75 scored items; means outside the labels were 1.59 and 1.22. Optional labels are incomplete, so unlabelled output is not independently judged irrelevant content or a definitive precision measure.

### Ranking and model comparison

BM25 uses word/identifier tokens and Han characters/bigrams, with saturation 1.5 and length normalization 0.75. The mixed variant combines lexical and embedding ranks with equal reciprocal-rank weights and offset 60. Neither was tuned against these labels. Coverage includes hierarchy expansion.

| Ranking, ten candidates | All expected references | Mean per-query required-ID recall |
| --- | ---: | ---: |
| BM25 | 23 of 28 | 82.1% |
| Qwen3-Embedding-0.6B | 25 of 28 | 95.2% |
| Qwen3-Embedding-4B | 25 of 28 | 95.2% |
| 0.6B + BM25 rank fusion | 26 of 28 | 92.9% |
| 4B + BM25 rank fusion | 24 of 28 | 89.9% |

Mean per-query recall gives equal weight to questions with one or several required IDs. It differs from pooling all IDs into one denominator. Fusion's higher complete-query count but lower recall reflects more missing IDs on its failed questions. Fusion did not consistently improve multilingual retrieval. Results for one, three, five, ten, twenty, and forty candidates are preserved privately.

Both models used previously idle NVIDIA L20 GPUs on lzt242, float16, SDPA attention, left padding, last-token pooling, and normalized vectors. Documents include their canonical graph ancestry; queries have an instruction about stored facts, decisions, and applicable guidance. No document was truncated.

| Model | Pinned revision | Warm query encoding median | Document encoding | Model load | Peak PyTorch allocation |
| --- | --- | ---: | ---: | ---: | ---: |
| Qwen3-Embedding-0.6B | `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` | 27.1 ms | 1.863 s | 1.148 s | 1.64 GiB |
| Qwen3-Embedding-4B | `5cf2132abc99cad020ac570b19d031efec650f2b` | 43.3 ms | 5.904 s | 3.133 s | 7.96 GiB |

Queries run individually: five repetitions for 0.6B and three for 4B. Document batches have eight and four entries. PyTorch was `2.12.0+cu130`; Transformers was `5.12.0`. Downloads and snapshot construction are excluded from these startup/encoding times. The 4B model was evaluated for ranking and encoding, not another full selector integration. Model usage follows the [official Qwen3 documentation](https://github.com/QwenLM/Qwen3-Embedding).

### Why smaller input did not mean faster retrieval

Median supplied snapshot/request text shrank from 53,713 to 5,773 characters. In the live pass, median provider-reported final-request input was 34,580 tokens for the baseline versus 21,158 for the assisted path; median uncached input was 1,045 versus 7,808. The existing full-prefix cache was already effective. These counters describe final provider requests, not complete billing across all internal tool steps.

Slow assisted requests grew beyond their initial shortlist. The 90-second request ended with 50,900 input tokens, and some outputs contain IDs absent from candidate coverage. Cache reuse and broader follow-up reads are plausible contributors; their individual causal effects were not isolated.

### Controls and limits

- Base RightMemory revision: `9ebfa16`. Selector: `gpt-5.6-luna`, reasoning effort `high`, matching the installed configuration.
- Every query has a new logical session. The baseline retains its shared prefix conversation; the assisted path sends its varying candidates in one new turn. Role instructions, source-root `AGENTS.md`, progressive reads, parsing, and rendering remain in place.
- Two passes use shuffled orders with seeds 42 and 137. Each method runs serially in a separate root, while the methods overlap in wall time. This avoids contention on the root lock.
- Memory, Pursuit, F# details, and Agent Corrections are included. Synchronization is disabled in the copies. Pending submissions, memory changes, earlier conversation history, M#/S# resources, and external MF#/MQ# sources are outside this experiment. Unsupported linked sources are refused.
- A stored password was redacted identically in both copies. Private memory, labels, selections, and traces are ignored under `tmp/embedding-retrieval/` and are not committed.
- Corpus SHA-256: `98a9a5d4b37293e4ad72701dea4432fb5cae3181c3106aa504d555bcf40bb8f5`.
- Case-file SHA-256: `592e580b39ba071218096d98ff5cfc315a11408924c28b3073d3bffd617428a2`.

### Reproduction and evidence

The ordinary repository environment runs snapshot, rendering, and selector commands. GPU commands additionally need PyTorch, Transformers, and NumPy; these are optional experiment dependencies.

Private evidence under `tmp/embedding-retrieval/`:

- `corpus.json`, `cases.json`, `root/`, `root-dense/`: frozen data and labels.
- `rankings.json`, `rankings-4b.json`: ranks, scores, and encoding times.
- `direct-live.json`: 96 end-to-end direct measurements.
- `baseline.jsonl`, `dense10-selector.jsonl`: selections, scores, and timings. Assisted rows with `repeat: 1` use the live endpoint.
- `state-baseline/`, `state-dense/`: runtime traces and experiment-owned provider state.
- `metrics.json`, `metrics-4b.json`: per-query scoring. The delivered `metrics.json` also contains `measured_summary`; rerunning the basic summarizer rebuilds its scoring rows.

Start a fresh snapshot:

```sh
python experiments/retrieval_embeddings.py snapshot \
  --source <memory-root> --out tmp/embedding-retrieval
```

Supply `cases.json` with a `cases` array. Entries have `id`, `category`, `query`, `required`, and optionally `optional` source-ID lists. Freeze labels before running. The harness validates labels and matching corpus/case hashes.

Copy the harness, corpus, and cases to the GPU host, choose an idle GPU, and run:

```sh
CUDA_VISIBLE_DEVICES=<idle-gpu> python retrieval_embeddings.py embed \
  --corpus corpus.json --cases cases.json \
  --revision 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3 --out rankings.json

CUDA_VISIBLE_DEVICES=<idle-gpu> python retrieval_embeddings.py serve \
  --corpus corpus.json --revision 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3 \
  --port 18762
```

The server binds to loopback. Forward port 18762 over SSH. Local direct measurements use:

```sh
python experiments/retrieval_embeddings.py direct \
  --corpus tmp/embedding-retrieval/corpus.json \
  --cases tmp/embedding-retrieval/cases.json \
  --root tmp/embedding-retrieval/root \
  --embedding-url http://127.0.0.1:18762 \
  --out tmp/embedding-retrieval/direct-live.json --repeats 3
```

Use `select --method baseline` for the current retriever, or `select --method dense --top-k 10 --embedding-url http://127.0.0.1:18762` for live assisted retrieval. Both require `--corpus`, `--cases`, `--root`, `--state`, `--out`, and take `--repeats`. `--rankings` substitutes saved rankings for the live endpoint. Keep each root copy's Markdown identical. These commands call the configured external model with the existing login.

Reusing an output file skips completed case/repeat pairs and rejects mismatched corpus, labels, method, or candidate count. Offline scoring:

```sh
python experiments/retrieval_embeddings.py summarize \
  --corpus tmp/embedding-retrieval/corpus.json \
  --cases tmp/embedding-retrieval/cases.json \
  --rankings tmp/embedding-retrieval/rankings.json \
  --selector tmp/embedding-retrieval/baseline.jsonl \
             tmp/embedding-retrieval/dense10-selector.jsonl \
  --out tmp/embedding-retrieval/metrics.json
```

### Verification and retained resources

The full suite completed: 1,494 tests, 44 skips, no failures or errors. Five focused numerical checks cover duplicates, optional labels, empty-answer denominators, rank fusion, and stable ties. Syntax compilation passed. These checks verify implementation boundaries, not the human meaning of labels.

The experiment remains on local branch `lzt/embedding-retrieval`. Both GPU allocations and the temporary SSH tunnel were released; duplicate server staging files were removed. Model caches are recorded in lzt242's cleanup-candidate file. Private local evidence and provider state are recorded in Local's cleanup-candidate file. Preserve needed ignored evidence separately before archiving the managed worktree.
