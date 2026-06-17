# Review Prefix Dedupe Design

## Goal

Prevent automatic transcript review from repeatedly reviewing forked Codex chats that share the same normalized history. When several eligible sessions are prefix duplicates, RightMemory should review the longest representative and mark the shorter covered sessions as done after that representative succeeds.

## Context

The current review scanner filters provider transcripts by modification time, idle time, parseability, internal-provider status, exact `source:session_id` review state, and batch policy. It does not compare transcript content across different provider session ids. Forked Codex chats can therefore produce several eligible JSONL files where each shorter session is a prefix of a longer fork. A local sample of recent Codex history showed real prefix chains across distinct session ids, so exact session-id dedupe is not enough.

This design intentionally stays narrower than append-aware transcript review. It does not store reviewed turn ranges, reopen previously reviewed sessions, or send only new suffixes. It dedupes the current eligible candidate set before review.

## Behavior

For each eligible normalized session, compute a stable sequence of turn hashes from its normalized turns. A candidate `$A$` is a duplicate of candidate `$B$` when `$A$` and `$B$` have the same provider source and the full hash sequence for `$A$` is a prefix of the hash sequence for `$B$`. If multiple candidates are duplicates, keep the longest representative.

After a representative review succeeds:

- save normal review state for the representative;
- save review state for each skipped prefix duplicate covered by that representative;
- count only the representative in `reviewed`;
- count each skipped prefix duplicate in a new `skipped_duplicate` result field.

If the reviewer fails, no representative or duplicate alias is marked reviewed. This preserves the current failure rule: a failed batch marks no provider sessions reviewed.

## Candidate Selection

The scanner keeps the existing eligibility filters first. After exact `source:session_id` uniqueness, it runs prefix dedupe:

1. Compute `turn_hashes` for each candidate from normalized `(user, assistant)` turn text.
2. Sort candidates by longer turn count first, then newer modification time, then path, source, and session id for deterministic ties.
3. Walk candidates from longest to shortest.
4. Keep a candidate when it is not a prefix duplicate of any already-kept candidate from the same source.
5. Attach a skipped candidate as an alias to the first kept candidate whose hash sequence starts with the skipped candidate's full sequence.

Exact duplicate content is treated as a prefix duplicate both ways. The ordering decides the representative, so equal-content duplicates keep the newest deterministic representative.

Provider source is part of the dedupe boundary. Codex and Claude sessions should not dedupe each other even if their normalized text happens to match.

## Batching

Batch selection should use only representatives. For `review watch`, the full-batch gate should compare the number of deduped representatives against `[review].batch_size`. Duplicate aliases should not help fill a full batch because they are not sent to the reviewer.

For `review scan --once`, partial-batch behavior stays the same, except the batch is selected from representatives.

## Result Reporting

Add `skipped_duplicate` to `ReviewScanResult` and formatted scan output.

`reviewed` remains the number of provider sessions actually reviewed by the model as representatives. It should not include duplicate aliases. Dreamer and Insight trigger increments should continue to use `reviewed`, so skipped duplicates do not inflate downstream maintenance triggers.

`skipped_duplicate` is the number of eligible sessions skipped because their normalized turns were covered by a reviewed representative. These sessions are still recorded in review state after representative success so they do not reappear in later scans.

## State Compatibility

Keep the existing review state schema for reviewed sessions. No migration is required because skipped duplicates are persisted as ordinary reviewed `source:session_id` entries.

The new `skipped_duplicate` counter is runtime output only. It does not require state changes.

## Tests

Add scanner tests for:

- a shorter prefix session is skipped, the longest representative is sent to the reviewer, and both sessions are marked reviewed after success;
- exact duplicate content keeps one representative and marks the duplicate reviewed;
- duplicate aliases are not marked reviewed when the reviewer fails;
- duplicate detection is provider-local;
- `review watch` full-batch gating uses representative count, not representative plus alias count;
- `reviewed` counts representatives only, while `skipped_duplicate` counts aliases;
- deterministic tie-breaking for equal content.

## Documentation

Update the README automatic review section to explain that RightMemory suppresses fork-style prefix duplicate transcripts by reviewing the longest eligible representative, marking shorter covered sessions reviewed after success, and reporting them under `skipped_duplicate`.

## Out Of Scope

This change does not:

- detect archived Codex chats;
- store persistent content fingerprints across historical review state;
- review only the new suffix of a resumed or forked session;
- dedupe across different provider sources;
- change normalized transcript parsing.
