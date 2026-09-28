# Retrieval experiments

[retrieval_embeddings.py](retrieval_embeddings.py) runs an isolated benchmark on disposable copies of Memory using the canonical graph index and renderer. Production code, prompts, installation, and dependencies are unchanged.

## Embedding retrieval — 2026-09-28

**Result: model choice changes reference coverage. Nemotron 1B is the fastest encoder tested; Jina covers all labelled positive cases within twenty candidates.** Jina plus the current selector shows a modest mean/median improvement in one fresh pass, with a worse slow tail. Direct search is fast but still needs a no-match decision. The small, agent-labelled benchmark does not establish a production winner.

### Expanded model comparison

Six downloadable models were measured on the same frozen corpus and 32 cases. The two Qwen models were rerun; their rankings exactly match the original run. These measurements compare specific model revisions and input formats, not all embedding models or independently judged answer quality.

| Model | Complete at 5 | Complete at 10 | Complete at 20 | Chinese complete at 10 | Query encoding median |
| --- | ---: | ---: | ---: | ---: | ---: |
| Qwen3-Embedding-0.6B | 24/28 | 25/28 | 25/28 | 4/6 | 27.4 ms |
| Qwen3-Embedding-4B | 24/28 | 25/28 | 25/28 | 4/6 | 43.7 ms |
| Harrier-0.6B | 25/28 | 25/28 | 27/28 | 4/6 | 26.8 ms |
| Jina v5 small retrieval | 26/28 | 26/28 | 28/28 | 5/6 | 27.9 ms |
| RTriever-4B | 24/28 | 25/28 | 25/28 | 4/6 | 36.8 ms |
| Nemotron 3 Embed 1B BF16 | 25/28 | 26/28 | 26/28 | 5/6 | 18.6 ms |

Nemotron 1B is the fastest query encoder in this comparison. Jina is the only tested model that covers every required reference within twenty candidates; Harrier and Nemotron reach 28/28 at forty. The two remaining Jina top-ten omissions occur at exact ranks 11 and 17, so the improvement at twenty comes from finding those entries, not a heading expanding the entire memory. Jina already reaches 26/28 at five candidates. These candidate-count choices are exploratory observations on the same labels, not a held-out confirmation.

At ten candidates, mean per-query required-ID recall is 95.2% for both Qwen models and RTriever, 94.0% for Harrier, and 97.0% for Jina and Nemotron. RTriever is documented for English; its Chinese results are included explicitly. There are only six Chinese cases, some related to English cases. Fixed-count embedding retrieval still returns material for all four no-answer queries; no rejection threshold was trained.

The first five models have Qwen-based designs. Nemotron provides a different, bidirectional Ministral design. Every expanded ranking run used idle L20 GPU 0, 246 indexed entries, five individual encodings per query (160 timing samples per model), SDPA, normalized full-dimensional vectors, and no input truncation. Qwen, Harrier, Jina, and RTriever used float16 and last-token pooling; Nemotron used its documented bfloat16 and mean pooling over valid tokens. Precision differs and is recorded rather than treated as an isolated architecture comparison. Full retrieval was served on idle L20 GPU 1.

Harrier and RTriever use the same task instruction as Qwen. Jina uses its saved `Query: ` / `Document: ` prefixes. Nemotron uses the checkpoint/card prefixes `query: ` / `passage: `, not the release blog table's `document:` shorthand. Transformers 5.12 honors Nemotron's saved `is_causal=false`; no remote custom code or model code override is used. Numeric checks confirmed padding exclusion in mean pooling and finite bounded Nemotron similarity scores.

| Additional model | Pinned revision | Peak PyTorch allocation |
| --- | --- | ---: |
| Harrier-0.6B | `f9b9dc8d367d443f2479d27aa5d8d2850c0774ee` | 1.22 GiB |
| Jina v5 small retrieval | `6856e76bb72982e58de0620458a4e8b3614da340` | 1.64 GiB |
| RTriever-4B | `2133b3d737c602f70b73642944e19ab4b8c0e70c` | 7.96 GiB |
| Nemotron 3 Embed 1B BF16 | `c0c9fea93ea424587517f2c59e20db9f1d6bf615` | 2.59 GiB |

Official loading references: [Harrier](https://huggingface.co/microsoft/harrier-oss-v1-0.6b), [Jina retrieval](https://huggingface.co/jinaai/jina-embeddings-v5-text-small-retrieval), [RTriever](https://huggingface.co/yale-nlp/RTriever-4B), and [Nemotron 1B](https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16).

Nemotron 8B BF16 revision `d1f2f25730bbd775b99b29185134bc86653bf2d1` has public weights but remains unmeasured. The original Xet and HTTP attempts stalled. A further retry resumed the existing partial files, tried 24 parallel HTTP ranges, and retried native Xet with one file at a time, sixteen connections, and sequential writes. Parallel ranges received only 44.6 MB after 210 seconds; Xet reported about 58.6 MB after seven minutes without completing a shard. Local HTTP and a mirror probe were also slow. About 2.83 GB is still missing from the largest retained partial files, which cannot be considered verified model weights until complete and hash-checked. All retry jobs were stopped; GPU 0 returned to its pre-experiment 19 MiB allocation. Download evidence is retained privately as `nemotron-8b-retry.json` and retry logs. Qwen3.7 Text Embedding and Flash were excluded because the official documentation exposes API access and no official downloadable weights were found. No new embedding API was used.

### Recovering the source-update rule

The `m01` question requests the source-update rule, Quad packaging details, and the generic-core/SP-adapter boundary. With the original single query, Jina ranks the source-update rule 11th and Nemotron 1B ranks it 35th. Both find the project-specific facts. The following manual diagnostic keeps the same frozen corpus, model revisions, input prefixes, and pooling settings; it changes the query alone.

| Query for the source-update rule | Jina rank | Nemotron 1B rank |
| --- | ---: | ---: |
| Original question with all three requests | 11 | 35 |
| `I will update Quad in dmd_algorithm. Retrieve the source-update rule.` | 1 | 17 |
| `Retrieve the source-update rule.` | 1 | 1 |

The other two clause queries retain the same project lead-in and ask for the current packaging boundary or how core code should relate to SP-specific adapters. Their respective target ranks are 2 and 4 for Jina, and 1 and 3 for Nemotron. Splitting clauses therefore brings all three target entries within the first five of their respective Jina searches. Nemotron still needs the more general source-rule query to bring that rule into its first ten.

The added general query takes a median 26.8 ms to encode with Jina and 17.6 ms with Nemotron 1B, over five warm encodings on idle L20 GPU 0. These are encoding times only: they exclude producing the query, network transport, merging, final selection, and rendering. Query variants and labels were written after inspecting this known failure, so this is a mechanism diagnostic, not a held-out accuracy improvement. No automatic query generator, reranker, final-answer selector, or no-match behavior was evaluated in this diagnostic. The original 32 benchmark queries and labels remain unchanged.

There is also an existing lexical signal: BM25 puts the source-update rule first for the original question. Equal reciprocal-rank fusion puts it third with Jina and seventh with Nemotron, recovering all three required entries within ten candidates. However, across all 28 positive cases, Jina fusion stays at 26 complete cases while mean required-ID recall falls from 97.0% to 92.9%; Nemotron fusion falls from 26 to 25 complete cases and also reaches 92.9% recall. Several Chinese cases get worse. Fixing this single example does not justify replacing embedding rankings with equal-weight fusion globally.

Primary-source research suggests three useful comparisons:

- **Focused and more general queries alongside the original.** [Question Decomposition for Retrieval-Augmented Generation](https://arxiv.org/html/2507.00355) retrieves separately for subquestions, merges the candidates, and reranks them. Its multi-hop QA results improve, but its query-generation step alone adds 16.7 seconds per query in the reported setup. [Take a Step Back](https://arxiv.org/html/2310.06117) instead adds a higher-level question alongside the original; it reports downstream answer accuracy rather than retrieval recall. Our clause split and project-name removal are simpler manual diagnostics related to these mechanisms, not reproductions of either full method. For RightMemory, the next useful test is whether the caller can express distinct needs in its existing retrieval request, avoiding an additional model round trip. Keep the original query so project-specific constraints remain available, and verify that the final selection still covers each requested part.
- **A wider candidate pool before reranking.** Jina's first twenty and Nemotron 1B's first forty already contain every labelled required reference in this benchmark. A reranker scores each query and candidate together, then selects a smaller set; it cannot recover entries excluded before it runs. [Anthropic's Contextual Retrieval experiments](https://www.anthropic.com/engineering/contextual-retrieval) use this broad-then-narrow pattern. A separate [production-style fusion study](https://arxiv.org/abs/2603.02153) finds that gains in initial recall can disappear after reranking and truncation. Both initial coverage and the final selected references need measurement. No local reranker was tested here.
- **Context for under-specified indexed entries.** Anthropic prepends a short explanation of each chunk's document context before embedding and lexical indexing, reporting fewer missed references. Our index already includes canonical ancestry, and the source-update rule already describes when it applies. Extra generated context is therefore a secondary experiment for this particular miss; it should preserve the stored rule's actual scope rather than invent new applicability.

The strongest local evidence favors testing an additional rule-focused query while retaining the original search. Widening Jina's candidate count to twenty is the simpler measured control. Neither the papers nor this one-case diagnostic establish a production improvement, and the current benchmark explicitly asks for the source-update rule; it does not establish recovery when the user leaves that need unstated.

Private reproduction evidence: `cases-m01-decomposed.json`, `cases-m01-unscoped.json`, and `rankings-{jina,nemotron-1b}-m01-{decomposed,unscoped}.json`. Run the existing `embed` command with the corresponding case file and the documented model settings; no harness or production implementation change is required.

### Expanded live comparison

Jina at twenty candidates was selected for a live check because it covers all required references at that count. This changes both the embedding model and candidate count relative to the initial Qwen prototype. A fresh baseline and Jina each ran all 32 requests once, in identical shuffled order (seed 311), separate state directories, with the same selector model and source-root instructions. Their wall-clock windows partially overlapped. Repository tests had finished before these calls. The direct row has three repeats per case.

| Path | Mean seconds | Median seconds | 95th percentile seconds | All expected references | Correct no-match |
| --- | ---: | ---: | ---: | ---: | ---: |
| Current retriever, fresh pass | 100.596 | 97.872 | 163.197 | 26/28 | 4/4 |
| Jina, top twenty, then current selector | 90.687 | 82.501 | 166.528 | 27/28 | 4/4 |
| Jina, top twenty, directly rendered | 0.072 | 0.070 | 0.079 | 28/28 | 0/4 |

The assisted path's mean was 9.8% lower and median 15.7% lower, while its 95th percentile was 2.0% higher. Its maximum was 219.31 seconds versus 177.95. This one pass does not establish a reliable speedup. Overall latency was much higher than the earlier pass for both methods; compare within this round rather than combining the old and new wall times. Service conditions were not controlled, and the individual cause of that shift was not isolated.

All 64 new selector calls completed without errors. The baseline missed one expected correction in each of two cases; Jina's selector omitted one correction that was present in its candidate context. Thus perfect candidate coverage did not translate into perfect final reference coverage. These are source-ID labels, including some overlapping guidance, not independently adjudicated answer correctness.

Private evidence is in `tmp/embedding-retrieval/`: `rankings-<model>.json`, `metrics-<model>.json`, `model-comparison.json`, `baseline-expanded.jsonl`, `jina20-selector.jsonl`, `direct-jina20.json`, and `expanded-live-summary.json`. These files contain private context and remain uncommitted.

The experiment harness now accepts `--query-prefix`, `--document-prefix`, `--pooling last|mean`, `--dtype float16|bfloat16`, and `--max-length` for `embed` and `serve`. For Jina supply `--query-prefix "Query: " --document-prefix "Document: "`; for Nemotron supply `--query-prefix "query: " --document-prefix "passage: " --pooling mean --dtype bfloat16`. Use the model IDs and pinned revisions above. Other models retain the original query instruction and unprefixed documents. Live results record the model and revision.

Final verification used the installed RightMemory runtime Python and `python -m tests --jobs 8`: 1,494 tests, 44 skips, no failures or errors. Syntax compilation passed. No production runtime or dependency files changed.

### Initial Qwen live comparison

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
