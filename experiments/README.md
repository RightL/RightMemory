# Retrieval experiments

The scripts in this directory support controlled retrieval experiments using fully synthetic, non-personal datasets.

- `retrieval_embeddings.py` prepares datasets and compares embedding retrieval.
- `retrieval_reranker.py` compares reranking configurations.
- `retrieval_runtime.py` exercises retrieval through the CLI and MCP interfaces.

Use each script's `--help` output for its supported arguments. Use disposable roots and keep generated datasets, labels, results, and runtime state outside version control.

Never upload, commit, push, or publish personal memory or personal examples. This includes excerpts, paraphrases, derived benchmark queries, source identifiers, private paths, and reports derived from real personal data. Examples and fixtures must be invented from scratch, not anonymized from a real person's memory or conversations.
