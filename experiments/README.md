# Retrieval experiments

[retrieval_embeddings.py](retrieval_embeddings.py) and [retrieval_reranker.py](retrieval_reranker.py) compare models on frozen, disposable Memory copies. [retrieval_runtime.py](retrieval_runtime.py) verifies the optional embedding backend through the actual CLI and MCP interfaces. Private inputs and raw results remain under ignored `tmp/embedding-retrieval/`.

## Integrated embedding retrieval — 2026-09-29

The optional runtime backend uses Nemotron 3 Embed 1B and Jina reranker v3.5, with forty candidates and at most ten returned entries. The Windows client owns the canonical source index and incremental vector cache; the models run on L20 GPU 1 through a private SSH tunnel. The configured host is a service address, not a hard-coded server dependency.

On the original frozen 32 cases and 178 substantive entries, all **28/28 positive cases** retain every labelled required ID on each of three randomized passes: **84/84 positive measurements**. All 32 cases return identical rankings across repeats. The four no-answer queries also receive ten candidates; deciding their usefulness belongs to the caller.

| Actual runtime measurement | Result |
| --- | ---: |
| MCP measurements | 96 |
| Warm end-to-end median | 1,168.6 ms |
| Warm end-to-end 95th percentile | 1,441.6 ms |
| Maximum returned entries | 10 |
| Cold index plus first request, with models already loaded | 4.94 s |
| CLI dispatcher smoke request | 1.32 s |
| CLI/MCP selected source IDs on the smoke case | Identical |

These measurements include Windows source indexing/validation, cache reads, session bookkeeping, MCP dispatch, SSH transport, and both model stages. They exclude model-service startup and use a disposable root with sync disabled. This is still a small, agent-labelled coverage test, not independent answer-quality evaluation. Live pending submissions, linked sources, imports, cache invalidation, concurrent source changes, and failure behavior are covered by focused tests rather than these frozen labels.

The service uses the same downloaded checkpoints as the model-only experiment. Model content fingerprints are saved in the report. Run against a separately started service:

```sh
python experiments/retrieval_runtime.py \
  --source-root tmp/embedding-retrieval/root \
  --cases tmp/embedding-retrieval/cases.json \
  --url http://127.0.0.1:18766 --repeats 3 \
  --out tmp/embedding-retrieval/runtime-nemotron-jina.json
```

The script creates and removes its own disposable Memory copy and never rewrites the source root. The raw report is `tmp/embedding-retrieval/runtime-nemotron-jina.json`. Normal entries retain their ancestor text; long entries use bounded search passages but still occupy one result slot, with their original source text returned. The backend keeps independent-query session records separately from the existing agent retriever's conversation state.

Validation exercised 1,524 tests: 1,480 pass and 44 are skipped on Windows, counting the final CLI rerun. The full run's only remaining failure was an exact CLI help-text assertion; after retaining the established wording, all 156 CLI tests passed. The 28 embedding tests cover source identity and invalidation, bounded selection, repeated requests, concurrent source edits, service failures, and real CLI/MCP dispatch. Doctor and dashboard checks also pass. Python compilation, JavaScript syntax, the installed service command, and Git whitespace checks pass. Logs are retained beside the private benchmark report.

The test environment uses MCP 2.2.0, Pydantic AI 2.12.0, and Codex SDK 0.147.0. A fresh dependency resolution selected Pydantic AI 2.51.0, which fails the existing DeepSeek `tool_choice` profile assertion on both this branch and unchanged `main` (`9ebfa16`). That independent dependency/test incompatibility remains outside this backend change; production dependency requirements were not pinned to hide it. The GPU service, tunnel, and disposable remote staging directory used for this validation have been removed; downloaded model snapshots remain available.

## Nemotron + Jina reranking — 2026-09-29

**Result: Jina improves the final ten-entry ranking, but a score cutoff does not reliably detect that the requested answer is absent.** These model-only measurements motivated the optional runtime backend measured above.

[retrieval_reranker.py](retrieval_reranker.py) runs Nemotron 3 Embed 1B followed by Jina reranker v3.5 on the frozen corpus. It retrieves forty substantive entries, reranks them against the original complete request, and selects at most ten exact entry IDs. It does not expand headings or selected entries into whole subtrees. The baseline uses the same entry policy, so the limit really means ten memory entries; this differs from the hierarchy-expanding renderer in the earlier embedding experiments.

### Ranking and time

The original 32 cases contain 28 positive cases and four unrelated questions with no stored answer. All 28 positive cases have every labelled required entry in Nemotron's forty candidates. Jina promotes those entries into the final ten.

| Method | Complete positive cases at ten | Chinese complete cases | Median warm local time | 95th percentile |
| --- | ---: | ---: | ---: | ---: |
| Nemotron 1B, direct ten entries | 26/28 | 5/6 | 15.7 ms | 16.3 ms |
| Nemotron 1B, forty candidates, Jina, ten entries | 28/28 | 6/6 | 498.4 ms | 779.8 ms |

The Jina stage alone takes a median 483.1 ms. These are 96 measurements, three per case, on previously idle L20 GPU 0. Both models remain loaded in the same process. Timings include live query encoding, vector scoring, candidate selection, and reranking; they exclude model/index startup, network transport, validation checks, and final rendering. They therefore are not directly comparable to the older full-selector wall times. The benchmark uses bfloat16, SDPA, Torch 2.12.0+cu130, and Transformers 5.12.0. Peak allocated GPU memory for both models is 4.84 GiB. Candidate prompts contain 6,922–12,655 tokens; no query or document is truncated.

All fresh Nemotron rankings exactly reproduce the frozen control, and all three Jina repetitions produce identical rankings. Reversing the forty candidates for every case still gives 28/28 complete cases. This does not establish order invariance: the top-ten ordering changes in every case, and individual scores move by as much as 0.508.

### Empty-result filtering

Fixed-count selection returns entries for every no-answer question. The following Jina score cutoffs are exploratory, evaluated on the same labels rather than calibrated on separate training data.

| Per-entry Jina cutoff | Complete positive cases | Correct empty results, original four negatives | Mean entries returned across 32 cases |
| --- | ---: | ---: | ---: |
| None | 28/28 | 0/4 | 10.00 |
| 0.0 | 27/28 | 3/4 | 4.25 |
| 0.1 | 25/28 | 4/4 | 2.44 |
| 0.4 | 19/28 | 4/4 | 0.81 |

Some supporting memories receive low scores even when another passage clearly answers the main question. A required correction in `g03` scores -0.0698, below the highest original negative score of 0.0403. No single per-entry cutoff can retain all labelled requirements and reject all four original negative queries on this run.

A simpler query-level check—return nothing if the highest score is below 0.1, otherwise return the top ten—appears perfect on the original set: 28/28 positives and 4/4 negatives, including the reversed-order run. To challenge that observation, six additional related-topic questions were authored and their absence-of-answer labels frozen before prediction. A read-only check of all 246 frozen documents found none of the requested exact values: maintenance timing, backup policy, budget, latency target, reranker configuration, and a named on-call owner. Nearby project facts may still be useful context; these cases test absent answers, not universal irrelevance of every related memory.

**The preselected 0.1 query-level check rejects none of these six new questions.** Their maximum Jina scores range from 0.1585 to 0.3544. This overlaps answerable cases: the original multi-part request's best score is 0.3268 and the historical request's is 0.3215. Raising a single query-level cutoff enough to reject all six necessarily rejects some answerable cases. At a per-entry cutoff of 0.4, all ten no-answer cases are empty, but only 19/28 original positive cases retain every expected reference.

These six questions are agent-authored challenge cases, not an independently judged benchmark. They do demonstrate why successful rejection of four unrelated questions was insufficient evidence for adopting a cutoff. Jina is useful for ranking this corpus; a production decision about missing answers remains unvalidated.

### Multiple-query diagnostic

For `m01`, a single original query plus Jina retrieves all three labelled requirements; the source-update rule moves from raw Nemotron rank 35 to Jina rank 4. The complete warm local path takes a median 0.788 seconds for this case.

Reusing the earlier original query plus four manual variants produces 74 unique candidates after taking forty substantive entries per search. Jina returns all three requirements within ten, ranking the source-update rule third, but median time rises to 1.749 seconds. This does not justify query splitting for that case: the single-query path already succeeds. The variants were written after inspecting the earlier failure and are a mechanism diagnostic, not new independent test cases.

### Download, reproduction, and verification

The model downloaded from [ModelScope](https://modelscope.cn/models/jinaai/jina-reranker-v3.5) in about 27 seconds. Its weight SHA-256 and nine supporting files match official Jina revision `e8a93f33f0b22108f8c2364f8484ce3422552fbc`. The model's small custom Python file was inspected before execution; the checkpoint and code were not modified. The [official model card](https://huggingface.co/jinaai/jina-reranker-v3.5) defines the local reranking interface.

The retained snapshot is at `/home/lztt/.cache/modelscope/models/jinaai--jina-reranker-v3.5/snapshots/master`. The harness loads both models from local caches with networking disabled during inference. It uses Nemotron revision `c0c9fea93ea424587517f2c59e20db9f1d6bf615`, the documented `query: ` / `passage: ` prefixes, masked mean pooling, and normalized full-dimensional vectors.

Copy both experiment scripts and the frozen inputs to the GPU host, select an idle GPU, and run:

```sh
CUDA_VISIBLE_DEVICES=<idle-gpu> HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
python retrieval_reranker.py \
  --corpus corpus.json --cases cases.json \
  --rankings rankings-nemotron-1b-control.json \
  --model /home/lztt/.cache/modelscope/models/jinaai--jina-reranker-v3.5/snapshots/master \
  --revision e8a93f33f0b22108f8c2364f8484ce3422552fbc \
  --candidate-k 40 --top-k 10 --repeats 3 --out jina-rerank.json
```

Use `--order reverse --repeats 1` for the order diagnostic. For the merged-search diagnostic, select `--ids m01` and add both `--additional-search cases-m01-decomposed.json rankings-nemotron-1b-m01-decomposed.json` and `--additional-search cases-m01-unscoped.json rankings-nemotron-1b-m01-unscoped.json`. For the new no-answer cases, first run the existing `embed` command with the same Nemotron settings, then pass the resulting case/ranking pair to the reranking harness. The output preserves all scores, exact IDs, timing samples, input hashes, threshold results, and repeat consistency.

Private evidence remains in `tmp/embedding-retrieval/`: `jina-rerank.json`, `jina-rerank-reverse.json`, `jina-rerank-m01-multi.json`, `jina-rerank-related-no-answer.json`, `cases-jina-related-no-answer.json`, `rankings-nemotron-jina-related-no-answer.json`, `jina-rerank-model-verification.json`, and the `jina-rerank-*.log` files. The original corpus and 32-case labels are unchanged.

Syntax checks and six focused numerical boundary checks passed. The repository suite completed 1,494 tests with 44 skips, zero failures, and zero errors. These checks support implementation correctness, not the semantic accuracy of the labels. No production runtime, prompt, installer, or dependency configuration changed.

## Embedding retrieval — 2026-09-28–29

**Result: model choice changes reference coverage. Nemotron 1B is the fastest encoder tested; Jina covers all labelled positive cases within twenty candidates.** Jina plus the current selector shows a modest mean/median improvement in one fresh pass, with a worse slow tail. Direct search returns a fixed count even for absent answers. Nemotron 8B does not improve coverage in this comparison. The small, agent-labelled benchmark does not establish a production winner.

### Expanded model comparison

Seven downloadable models were measured on the same frozen corpus and 32 cases. Six ran on September 28; Nemotron 8B and fresh Jina/Nemotron 1B controls ran on September 29. The table uses the fresh control timings for those two models. The two Qwen reruns and both later control runs exactly reproduce their earlier rankings. These measurements compare specific model revisions and input formats, not all embedding models or independently judged answer quality.

| Model | Complete at 5 | Complete at 10 | Complete at 20 | Chinese complete at 10 | Query encoding median |
| --- | ---: | ---: | ---: | ---: | ---: |
| Qwen3-Embedding-0.6B | 24/28 | 25/28 | 25/28 | 4/6 | 27.4 ms |
| Qwen3-Embedding-4B | 24/28 | 25/28 | 25/28 | 4/6 | 43.7 ms |
| Harrier-0.6B | 25/28 | 25/28 | 27/28 | 4/6 | 26.8 ms |
| Jina v5 small retrieval | 26/28 | 26/28 | 28/28 | 5/6 | 27.8 ms |
| RTriever-4B | 24/28 | 25/28 | 25/28 | 4/6 | 36.8 ms |
| Nemotron 3 Embed 1B BF16 | 25/28 | 26/28 | 26/28 | 5/6 | 18.5 ms |
| Nemotron 3 Embed 8B BF16 | 25/28 | 25/28 | 26/28 | 4/6 | 30.8 ms |

Nemotron 1B is the fastest query encoder in this comparison. Jina is the only tested model that covers every required reference within twenty candidates; Harrier and Nemotron 1B reach 28/28 at forty; Nemotron 8B reaches 27/28 at forty. The two remaining Jina top-ten omissions occur at exact ranks 11 and 17, so the improvement at twenty comes from finding those entries, not a heading expanding the entire memory. Jina already reaches 26/28 at five candidates. These candidate-count choices are exploratory observations on the same labels, not a held-out confirmation.

At ten candidates, mean per-query required-ID recall is 95.2% for both Qwen models, RTriever, and Nemotron 8B, 94.0% for Harrier, and 97.0% for Jina and Nemotron 1B. RTriever is documented for English; its Chinese results are included explicitly. There are only six Chinese cases, some related to English cases. Fixed-count embedding retrieval still returns material for all four no-answer queries; no rejection threshold was trained.

The first five models have Qwen-based designs. Nemotron provides a different, bidirectional Ministral design. Every expanded ranking run used idle L20 GPU 0, 246 indexed entries, five individual encodings per query (160 timing samples per model), SDPA, normalized full-dimensional vectors, and no input truncation. Qwen, Harrier, Jina, and RTriever used float16 and last-token pooling; Nemotron used its documented bfloat16 and mean pooling over valid tokens. Precision differs and is recorded rather than treated as an isolated architecture comparison. Full retrieval was served on idle L20 GPU 1.

Harrier and RTriever use the same task instruction as Qwen. Jina uses its saved `Query: ` / `Document: ` prefixes. Nemotron uses the checkpoint/card prefixes `query: ` / `passage: `, not the release blog table's `document:` shorthand. Transformers 5.12 honors Nemotron's saved `is_causal=false`; no remote custom code or model code override is used. Numeric checks confirmed padding exclusion in mean pooling and finite bounded Nemotron similarity scores.

| Additional model | Pinned revision | Peak PyTorch allocation |
| --- | --- | ---: |
| Harrier-0.6B | `f9b9dc8d367d443f2479d27aa5d8d2850c0774ee` | 1.22 GiB |
| Jina v5 small retrieval | `6856e76bb72982e58de0620458a4e8b3614da340` | 1.64 GiB |
| RTriever-4B | `2133b3d737c602f70b73642944e19ab4b8c0e70c` | 7.96 GiB |
| Nemotron 3 Embed 1B BF16 | `c0c9fea93ea424587517f2c59e20db9f1d6bf615` | 2.59 GiB |
| Nemotron 3 Embed 8B BF16 | `d1f2f25730bbd775b99b29185134bc86653bf2d1` | 15.80 GiB |

Official loading references: [Harrier](https://huggingface.co/microsoft/harrier-oss-v1-0.6b), [Jina retrieval](https://huggingface.co/jinaai/jina-embeddings-v5-text-small-retrieval), [RTriever](https://huggingface.co/yale-nlp/RTriever-4B), [Nemotron 1B](https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16), and [Nemotron 8B](https://huggingface.co/nvidia/Nemotron-3-Embed-8B-BF16).

Qwen3.7 Text Embedding and Flash were excluded because the official documentation exposes API access and no official downloadable weights were found. No new embedding API was used.

### Nemotron 8B follow-up — 2026-09-29

The user supplied a completed ModelScope snapshot of `nv-community/Nemotron-3-Embed-8B-BF16` at `/home/lztt/.cache/modelscope/models/nv-community--Nemotron-3-Embed-8B-BF16/snapshots/master`. All four weight-file SHA-256 hashes match the pinned NVIDIA revision above. Seven supporting files, including the model config, weight index, and tokenizer, are byte-identical to the cached official snapshot. Inference loaded that local path offline, without changing its files or executing remote model code. The earlier unsuccessful download attempts remain recorded privately; they no longer prevent testing.

The full 32-case run used the same harness, frozen input hashes, idle L20 GPU 0, and five individual warm encodings per query. The 8B run uses 4,096-dimensional normalized embeddings, bfloat16, masked mean pooling, SDPA, and the saved bidirectional setting. Peak PyTorch allocation was 15.80 GiB; corpus encoding took 11.365 seconds and loading took 5.027 seconds. Neither startup time is included in warm query timing. The `apply_yarn_scaling` configuration warning is explicitly documented as expected in the [NVIDIA model card](https://huggingface.co/nvidia/Nemotron-3-Embed-8B-BF16#expected-configuration-warning); the saved config was preserved.

Fresh control runs followed sequentially on the same GPU. Every Jina and Nemotron 1B query reproduced its September 28 ranking exactly.

| Model, September 29 run | Complete at 10 | Complete at 20 | Complete at 40 | Query encoding median | Peak allocation |
| --- | ---: | ---: | ---: | ---: | ---: |
| Jina v5 small retrieval | 26/28 | 28/28 | 28/28 | 27.8 ms | 1.64 GiB |
| Nemotron 3 Embed 1B BF16 | 26/28 | 26/28 | 28/28 | 18.5 ms | 2.59 GiB |
| Nemotron 3 Embed 8B BF16 | 25/28 | 26/28 | 27/28 | 30.8 ms | 15.80 GiB |

The 8B model ranks the source-update rule 58th, versus 35th for 1B and 11th for Jina, while ranking the requested packaging and adapter memories first and second. Its other top-ten misses are the correction in the Chinese partial-result question (`cn04`, rank 18) and the correction in the Chinese animation question (`cn05`, rank 27). In both Chinese cases, the overlapping factual contract ranks first. Missing the additional correction ID therefore does not itself demonstrate a wrong answer.

This follow-up measures candidate-reference coverage and local encoding latency, not another full selector comparison. Fixed-count search continues to return candidates for all four no-answer cases. The results do not support choosing 8B over the smaller models for this frozen benchmark; the small, agent-labelled dataset still does not establish a general model ranking.

Private evidence is stored as `nemotron-8b-verification.json`, `rankings-nemotron-8b.json`, `metrics-nemotron-8b.json`, and the `rankings-`/`metrics-` files ending in `jina-small-control` and `nemotron-1b-control`. `model-comparison.json` includes 8B and preserves the fresh controls separately from the earlier timing records. Use the local model path with the existing `embed` command, revision `d1f2f25730bbd775b99b29185134bc86653bf2d1`, query prefix `query: `, document prefix `passage: `, mean pooling, bfloat16, batch size eight, and five repeats to reproduce this run.

### Recovering the source-update rule

The `m01` question requests the source-update rule, Quad packaging details, and the generic-core/SP-adapter boundary. With the original single query, Jina ranks the source-update rule 11th, Nemotron 1B ranks it 35th, and Nemotron 8B ranks it 58th. All three find the project-specific facts. The following manual diagnostic keeps the same frozen corpus, model revisions, input prefixes, and pooling settings; it changes the query alone.

| Query for the source-update rule | Jina rank | Nemotron 1B rank | Nemotron 8B rank |
| --- | ---: | ---: | ---: |
| Original question with all three requests | 11 | 35 | 58 |
| `I will update Quad in dmd_algorithm. Retrieve the source-update rule.` | 1 | 17 | 27 |
| `Retrieve the source-update rule.` | 1 | 1 | 1 |

The other two clause queries retain the same project lead-in and ask for the current packaging boundary or how core code should relate to SP-specific adapters. Their respective target ranks are 2 and 4 for Jina, 1 and 3 for Nemotron 1B, and 1 and 1 for Nemotron 8B. Splitting clauses therefore brings all three target entries within the first five of their respective Jina searches. Both Nemotron models still need the more general source-rule query to bring that rule into their first ten.

The added general query takes a median 26.8 ms to encode with Jina, 17.6 ms with Nemotron 1B, and 36.7 ms with Nemotron 8B, over five warm encodings on idle L20 GPU 0. The first two diagnostic runs are from September 28; the 8B diagnostic is from September 29. These are encoding times only: they exclude producing the query, network transport, merging, final selection, and rendering. Query variants and labels were written after inspecting this known failure, so this is a mechanism diagnostic, not a held-out accuracy improvement. No automatic query generator, reranker, final-answer selector, or no-match behavior was evaluated in this diagnostic. The original 32 benchmark queries and labels remain unchanged.

There is also an existing lexical signal: BM25 puts the source-update rule first for the original question. Equal reciprocal-rank fusion puts it third with Jina and seventh with Nemotron 1B, recovering all three required entries within ten candidates. However, across all 28 positive cases, Jina fusion stays at 26 complete cases while mean required-ID recall falls from 97.0% to 92.9%; Nemotron 1B fusion falls from 26 to 25 complete cases and also reaches 92.9% recall. Several Chinese cases get worse. Fixing this single example does not justify replacing embedding rankings with equal-weight fusion globally.

Primary-source research suggests three useful comparisons:

- **Focused and more general queries alongside the original.** [Question Decomposition for Retrieval-Augmented Generation](https://arxiv.org/html/2507.00355) retrieves separately for subquestions, merges the candidates, and reranks them. Its multi-hop QA results improve, but its query-generation step alone adds 16.7 seconds per query in the reported setup. [Take a Step Back](https://arxiv.org/html/2310.06117) instead adds a higher-level question alongside the original; it reports downstream answer accuracy rather than retrieval recall. Our clause split and project-name removal are simpler manual diagnostics related to these mechanisms, not reproductions of either full method. For RightMemory, the next useful test is whether the caller can express distinct needs in its existing retrieval request, avoiding an additional model round trip. Keep the original query so project-specific constraints remain available, and verify that the final selection still covers each requested part.
- **A wider candidate pool before reranking.** Jina's first twenty and Nemotron 1B's first forty already contain every labelled required reference in this benchmark. A reranker scores each query and candidate together, then selects a smaller set; it cannot recover entries excluded before it runs. [Anthropic's Contextual Retrieval experiments](https://www.anthropic.com/engineering/contextual-retrieval) use this broad-then-narrow pattern. A separate [production-style fusion study](https://arxiv.org/abs/2603.02153) finds that gains in initial recall can disappear after reranking and truncation. Both initial coverage and the final selected references need measurement. No local reranker was tested here.
- **Context for under-specified indexed entries.** Anthropic prepends a short explanation of each chunk's document context before embedding and lexical indexing, reporting fewer missed references. Our index already includes canonical ancestry, and the source-update rule already describes when it applies. Extra generated context is therefore a secondary experiment for this particular miss; it should preserve the stored rule's actual scope rather than invent new applicability.

The strongest local evidence favors testing an additional rule-focused query while retaining the original search. Widening Jina's candidate count to twenty is the simpler measured control. Neither the papers nor this one-case diagnostic establish a production improvement, and the current benchmark explicitly asks for the source-update rule; it does not establish recovery when the user leaves that need unstated.

Private reproduction evidence: `cases-m01-decomposed.json`, `cases-m01-unscoped.json`, and `rankings-{jina,nemotron-1b,nemotron-8b}-m01-{decomposed,unscoped}.json`. Run the existing `embed` command with the corresponding case file and the documented model settings; no harness or production implementation change is required.

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
