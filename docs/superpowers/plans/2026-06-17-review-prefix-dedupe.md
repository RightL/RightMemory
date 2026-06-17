# Review Prefix Dedupe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Suppress fork-style duplicate transcript reviews by reviewing only the longest provider-local prefix representative.

**Architecture:** Keep the existing review-state schema and scanner flow. Add a prefix-dedupe stage after ordinary eligibility and exact `source:session_id` uniqueness, before full-batch gating and batch selection. The stage selects representatives for model review and records shorter prefix aliases as reviewed only after their representative succeeds.

**Tech Stack:** Python dataclasses, `unittest`, existing RightMemory review scanner, existing normalized transcript model.

---

## File Structure

- Modify `rightmemory/review.py`: add `skipped_duplicate`, add prefix-dedupe helper data structures, apply dedupe before batching, and mark aliases reviewed after representative success.
- Modify `tests/test_review.py`: add focused scanner tests for prefix aliases, exact duplicates, failure behavior, provider-local boundaries, full-batch gating, result counters, and deterministic ties.
- Modify `README.md`: document review prefix dedupe behavior and the new `skipped_duplicate` counter.

No state migration file is needed. Skipped aliases are persisted as ordinary reviewed session entries.

---

### Task 1: Result Counter And Prefix Success Test

**Files:**
- Modify: `tests/test_review.py`
- Modify: `rightmemory/review.py`

- [ ] **Step 1: Write failing tests for prefix dedupe success and output formatting**

In `tests/test_review.py`, add these tests inside `ReviewScannerTests`, after `test_scan_respects_configured_batch_size`:

```python
    def test_scan_reviews_longest_prefix_duplicate_and_marks_alias_reviewed(self):
        calls = []
        callback_calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            short = source / "01-short.jsonl"
            long = source / "02-long.jsonl"
            self._write_codex(short, turns=[("u1", "a1")], session_id="short")
            self._write_codex(long, turns=[("u1", "a1"), ("u2", "a2")], session_id="long")
            self._set_mtime(short, 1_000)
            self._set_mtime(long, 2_000)

            def on_review_success(count: int) -> None:
                saved = ReviewStateStore(root).load()
                callback_calls.append((count, len(saved.sessions)))

            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or "ok",
                on_review_success=on_review_success,
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(result.skipped_duplicate, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('"session_id": "long"', calls[0])
        self.assertNotIn('"session_id": "short"', calls[0])
        self.assertIn('"user": "u2"', calls[0])
        self.assertIn("codex:long", state.sessions)
        self.assertIn("codex:short", state.sessions)
        self.assertEqual(callback_calls, [(1, 2)])

    def test_scan_result_format_includes_skipped_duplicate(self):
        result = ReviewScanResult(reviewed=1, skipped_duplicate=2)

        formatted = result.format()

        self.assertIn("reviewed: 1", formatted)
        self.assertIn("skipped_duplicate: 2", formatted)
```

Also update the import at the top of `tests/test_review.py`:

```python
from rightmemory.review import ReviewScanResult, ReviewScanner, ReviewStateStore
```

- [ ] **Step 2: Run the focused failing tests**

Run:

```bash
rtk python -m unittest \
  tests.test_review.ReviewScannerTests.test_scan_reviews_longest_prefix_duplicate_and_marks_alias_reviewed \
  tests.test_review.ReviewScannerTests.test_scan_result_format_includes_skipped_duplicate
```

Expected: failure because `ReviewScanResult` has no `skipped_duplicate` field and the scanner sends both sessions or picks the old behavior.

- [ ] **Step 3: Add the result counter field**

In `rightmemory/review.py`, update `ReviewScanResult`:

```python
@dataclass(frozen=True)
class ReviewScanResult:
    reviewed: int = 0
    skipped_duplicate: int = 0
    waiting_for_batch: int = 0
    skipped_idle: int = 0
    skipped_old: int = 0
    skipped_reviewed: int = 0
    skipped_internal: int = 0
    skipped_empty: int = 0
    retried: int = 0
    failed: int = 0

    def format(self) -> str:
        return (
            f"reviewed: {self.reviewed}\n"
            f"skipped_duplicate: {self.skipped_duplicate}\n"
            f"waiting_for_batch: {self.waiting_for_batch}\n"
            f"skipped_idle: {self.skipped_idle}\n"
            f"skipped_old: {self.skipped_old}\n"
            f"skipped_reviewed: {self.skipped_reviewed}\n"
            f"skipped_internal: {self.skipped_internal}\n"
            f"skipped_empty: {self.skipped_empty}\n"
            f"retried: {self.retried}\n"
            f"failed: {self.failed}"
        )
```

Update the `counts` dictionary in `ReviewScanner.scan_once()`:

```python
        counts = {
            "reviewed": 0,
            "skipped_duplicate": 0,
            "waiting_for_batch": 0,
            "skipped_idle": 0,
            "skipped_old": 0,
            "skipped_reviewed": 0,
            "skipped_internal": 0,
            "skipped_empty": 0,
            "retried": 0,
            "failed": 0,
        }
```

- [ ] **Step 4: Add prefix-dedupe data structures and helpers**

In `rightmemory/review.py`, add this dataclass after `ReviewCandidate`:

```python
@dataclass(frozen=True)
class ReviewCandidateDedupeResult:
    representatives: list[ReviewCandidate]
    aliases_by_representative: dict[str, list[ReviewCandidate]] = field(default_factory=dict)
```

Add these helper functions after `_parse()`:

```python
def _dedupe_prefix_candidates(candidates: list[ReviewCandidate]) -> ReviewCandidateDedupeResult:
    kept: list[tuple[ReviewCandidate, tuple[str, ...]]] = []
    aliases_by_representative: dict[str, list[ReviewCandidate]] = {}

    for candidate in sorted(candidates, key=_prefix_dedupe_order_key):
        candidate_hashes = _turn_hashes(candidate.normalized)
        representative: ReviewCandidate | None = None
        for kept_candidate, kept_hashes in kept:
            if candidate.normalized.source != kept_candidate.normalized.source:
                continue
            if _is_hash_prefix(candidate_hashes, kept_hashes):
                representative = kept_candidate
                break
        if representative is None:
            kept.append((candidate, candidate_hashes))
            continue
        aliases_by_representative.setdefault(_candidate_state_key(representative), []).append(candidate)

    representatives = sorted((candidate for candidate, _hashes in kept), key=_scan_order_key)
    return ReviewCandidateDedupeResult(
        representatives=representatives,
        aliases_by_representative=aliases_by_representative,
    )


def _scan_order_key(candidate: ReviewCandidate) -> tuple[float, str, str, str]:
    return (
        candidate.mtime,
        candidate.transcript.path.as_posix(),
        candidate.normalized.source,
        candidate.normalized.session_id,
    )


def _prefix_dedupe_order_key(candidate: ReviewCandidate) -> tuple[int, float, str, str, str]:
    return (
        -len(candidate.normalized.turns),
        -candidate.mtime,
        candidate.transcript.path.as_posix(),
        candidate.normalized.source,
        candidate.normalized.session_id,
    )


def _candidate_state_key(candidate: ReviewCandidate) -> str:
    return _state_key(candidate.normalized.source, candidate.normalized.session_id)


def _turn_hashes(session: NormalizedSession) -> tuple[str, ...]:
    hashes = []
    for turn in session.turns:
        payload = json.dumps(
            {"user": turn.user, "assistant": turn.assistant},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        hashes.append(hashlib.sha256(payload.encode("utf-8")).hexdigest())
    return tuple(hashes)


def _is_hash_prefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    return len(shorter) <= len(longer) and longer[: len(shorter)] == shorter
```

- [ ] **Step 5: Apply dedupe before batching and mark aliases after success**

In `ReviewScanner.scan_once()`, replace the current `sorted_candidates` assignment with:

```python
        sorted_candidates = sorted(candidates, key=_scan_order_key)
```

After the `unique_candidates` loop and before the full-batch gate, add:

```python
        deduped = _dedupe_prefix_candidates(unique_candidates)
        representatives = deduped.representatives
```

Change the full-batch gate to use `representatives`:

```python
        if require_full_batch and len(representatives) < self.config.batch_size:
            counts["waiting_for_batch"] += len(representatives)
            return ReviewScanResult(**counts)
```

Change batch selection:

```python
        batch = representatives[: self.config.batch_size]
```

After `_review_with_retry()` succeeds, build the reviewed candidate list:

```python
        reviewed_candidates = []
        for candidate in batch:
            reviewed_candidates.append(candidate)
            reviewed_candidates.extend(
                deduped.aliases_by_representative.get(_candidate_state_key(candidate), [])
            )
```

Replace the state-save loop:

```python
        for candidate in reviewed_candidates:
            session = candidate.normalized
            sessions[_state_key(session.source, session.session_id)] = ReviewSessionState(
                session_id=session.session_id,
                source=session.source,
                last_reviewed_at=reviewed_at,
            )
```

Keep `reviewed` representative-only and count only successful aliases:

```python
        counts["reviewed"] += len(normalized_batch)
        counts["skipped_duplicate"] += len(reviewed_candidates) - len(normalized_batch)
```

Keep the callback representative-only:

```python
        if self.on_review_success is not None:
            self.on_review_success(len(normalized_batch))
```

- [ ] **Step 6: Run the focused tests**

Run:

```bash
rtk python -m unittest \
  tests.test_review.ReviewScannerTests.test_scan_reviews_longest_prefix_duplicate_and_marks_alias_reviewed \
  tests.test_review.ReviewScannerTests.test_scan_result_format_includes_skipped_duplicate
```

Expected: both tests pass.

- [ ] **Step 7: Commit the first slice**

```bash
rtk git add rightmemory/review.py tests/test_review.py
rtk git commit -m "feat: dedupe prefix review candidates"
```

---

### Task 2: Duplicate Edge Cases And Failure Semantics

**Files:**
- Modify: `tests/test_review.py`
- Modify: `rightmemory/review.py`

- [ ] **Step 1: Add failing edge-case tests**

In `tests/test_review.py`, add these tests after `test_scan_reviews_longest_prefix_duplicate_and_marks_alias_reviewed`:

```python
    def test_scan_exact_duplicate_keeps_newest_representative(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            older = source / "01-older.jsonl"
            newer = source / "02-newer.jsonl"
            turns = [("u1", "a1"), ("u2", "a2")]
            self._write_codex(older, turns=turns, session_id="older")
            self._write_codex(newer, turns=turns, session_id="newer")
            self._set_mtime(older, 1_000)
            self._set_mtime(newer, 2_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or "ok",
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(result.skipped_duplicate, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('"session_id": "newer"', calls[0])
        self.assertNotIn('"session_id": "older"', calls[0])
        self.assertIn("codex:older", state.sessions)
        self.assertIn("codex:newer", state.sessions)

    def test_scan_exact_duplicate_same_mtime_uses_path_tiebreaker(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            first = source / "01-first.jsonl"
            second = source / "02-second.jsonl"
            turns = [("u1", "a1")]
            self._write_codex(first, turns=turns, session_id="first")
            self._write_codex(second, turns=turns, session_id="second")
            self._set_mtime(first, 1_000)
            self._set_mtime(second, 1_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or "ok",
            )

            result = scanner.scan_once(now=10_000)

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(result.skipped_duplicate, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('"session_id": "first"', calls[0])
        self.assertNotIn('"session_id": "second"', calls[0])

    def test_scan_does_not_mark_duplicate_alias_when_reviewer_fails(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            short = source / "01-short.jsonl"
            long = source / "02-long.jsonl"
            self._write_codex(short, turns=[("u1", "a1")], session_id="short")
            self._write_codex(long, turns=[("u1", "a1"), ("u2", "a2")], session_id="long")
            self._set_mtime(short, 1_000)
            self._set_mtime(long, 2_000)

            def fail(session_id: str, message: str) -> str:
                calls.append(message)
                raise RuntimeError("review failed")

            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                fail,
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 0)
        self.assertEqual(result.skipped_duplicate, 0)
        self.assertEqual(result.failed, 1)
        self.assertEqual(len(calls), 2)
        self.assertIn('"session_id": "long"', calls[0])
        self.assertEqual(state.sessions, {})
```

- [ ] **Step 2: Run the edge-case tests**

Run:

```bash
rtk python -m unittest \
  tests.test_review.ReviewScannerTests.test_scan_exact_duplicate_keeps_newest_representative \
  tests.test_review.ReviewScannerTests.test_scan_exact_duplicate_same_mtime_uses_path_tiebreaker \
  tests.test_review.ReviewScannerTests.test_scan_does_not_mark_duplicate_alias_when_reviewer_fails
```

Expected: pass if Task 1 implementation is complete. If the exact duplicate or failure test fails, adjust `_prefix_dedupe_order_key()` or move `skipped_duplicate` counting so aliases are counted only after reviewer success.

- [ ] **Step 3: Fix exact duplicate or failure behavior if needed**

If the newest representative test fails, ensure `_prefix_dedupe_order_key()` uses negative mtime before path:

```python
def _prefix_dedupe_order_key(candidate: ReviewCandidate) -> tuple[int, float, str, str, str]:
    return (
        -len(candidate.normalized.turns),
        -candidate.mtime,
        candidate.transcript.path.as_posix(),
        candidate.normalized.source,
        candidate.normalized.session_id,
    )
```

If the failure test reports `skipped_duplicate: 1`, move this line so it runs only after `_review_with_retry()` succeeds:

```python
counts["skipped_duplicate"] += len(reviewed_candidates) - len(normalized_batch)
```

- [ ] **Step 4: Run the edge-case tests again**

Run the same command from Step 2.

Expected: all three tests pass.

- [ ] **Step 5: Commit edge-case coverage**

```bash
rtk git add rightmemory/review.py tests/test_review.py
rtk git commit -m "test: cover review prefix dedupe edges"
```

---

### Task 3: Provider Boundary And Full-Batch Gate

**Files:**
- Modify: `tests/test_review.py`
- Modify: `rightmemory/review.py`

- [ ] **Step 1: Add tests for provider-local dedupe and full-batch representative count**

In `tests/test_review.py`, add these tests after `test_scan_allows_mixed_provider_batch`:

```python
    def test_scan_prefix_dedupe_is_provider_local(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            codex_source = root / "codex"
            claude_root = root / "claude"
            claude_project = claude_root / "-repo"
            codex_source.mkdir()
            claude_project.mkdir(parents=True)
            codex_transcript = codex_source / "codex.jsonl"
            claude_transcript = claude_project / "claude.jsonl"
            self._write_codex(codex_transcript, turns=[("shared", "answer")], session_id="codex-short")
            self._write_claude(
                claude_transcript,
                turns=[("shared", "answer"), ("claude extra", "answer")],
                session_id="claude-long",
            )
            self._set_mtime(codex_transcript, 1_000)
            self._set_mtime(claude_transcript, 2_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[
                        ReviewSourceConfig(kind="codex", path=codex_source),
                        ReviewSourceConfig(kind="claude", path=claude_root),
                    ],
                ),
                lambda session_id, message: calls.append(message) or "ok",
            )

            result = scanner.scan_once(now=10_000)

        self.assertEqual(result.reviewed, 2)
        self.assertEqual(result.skipped_duplicate, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn('"source": "codex"', calls[0])
        self.assertIn('"source": "claude"', calls[0])

    def test_scan_full_batch_gate_uses_representative_count_after_dedupe(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            short = source / "01-short.jsonl"
            long = source / "02-long.jsonl"
            self._write_codex(short, turns=[("u1", "a1")], session_id="short")
            self._write_codex(long, turns=[("u1", "a1"), ("u2", "a2")], session_id="long")
            self._set_mtime(short, 1_000)
            self._set_mtime(long, 2_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    batch_size=2,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or "ok",
            )

            result = scanner.scan_once(now=10_000, require_full_batch=True)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 0)
        self.assertEqual(result.skipped_duplicate, 0)
        self.assertEqual(result.waiting_for_batch, 1)
        self.assertEqual(calls, [])
        self.assertEqual(state.sessions, {})
```

- [ ] **Step 2: Run the provider and full-batch tests**

Run:

```bash
rtk python -m unittest \
  tests.test_review.ReviewScannerTests.test_scan_prefix_dedupe_is_provider_local \
  tests.test_review.ReviewScannerTests.test_scan_full_batch_gate_uses_representative_count_after_dedupe
```

Expected: pass if dedupe checks `normalized.source` and the full-batch gate uses representative count.

- [ ] **Step 3: Fix provider or full-batch behavior if needed**

If the provider-local test fails, verify the source guard exists in `_dedupe_prefix_candidates()`:

```python
            if candidate.normalized.source != kept_candidate.normalized.source:
                continue
```

If the full-batch test fails, verify the gate uses `representatives`:

```python
        if require_full_batch and len(representatives) < self.config.batch_size:
            counts["waiting_for_batch"] += len(representatives)
            return ReviewScanResult(**counts)
```

- [ ] **Step 4: Run the provider and full-batch tests again**

Run the same command from Step 2.

Expected: both tests pass.

- [ ] **Step 5: Commit provider and full-batch coverage**

```bash
rtk git add rightmemory/review.py tests/test_review.py
rtk git commit -m "test: cover review dedupe batching boundaries"
```

---

### Task 4: Documentation And Regression Suite

**Files:**
- Modify: `README.md`
- Modify: `tests/test_review.py`
- Modify: `rightmemory/review.py`

- [ ] **Step 1: Update README automatic review behavior**

In `README.md`, replace this paragraph in the review section:

```markdown
If `[[review.sources]]` is omitted, RightMemory checks the default Codex and
Claude locations. By default it considers transcript files modified in the last
3 days, then reviews time-adjacent eligible sessions in batches of up to 3.
Review state is stored under `<memory-root>/.runtime/review/state.json` and
records reviewed provider sessions by source and session id. A successful batch
marks every included provider session reviewed; a failed batch marks none. If
the same provider session later changes or resumes, scanner state treats it as
already reviewed unless you clear the corresponding review state.
```

with:

```markdown
If `[[review.sources]]` is omitted, RightMemory checks the default Codex and
Claude locations. By default it considers transcript files modified in the last
3 days, suppresses provider-local prefix duplicates from forked transcripts,
then reviews time-adjacent eligible representatives in batches of up to 3. When
one eligible transcript is a normalized-turn prefix of a longer transcript from
the same provider, RightMemory reviews the longest representative and records
the shorter covered session under `skipped_duplicate` after representative
success. Review state is stored under `<memory-root>/.runtime/review/state.json`
and records reviewed provider sessions by source and session id. A successful
batch marks every included representative and covered duplicate reviewed; a
failed batch marks none. If the same provider session later changes or resumes,
scanner state treats it as already reviewed unless you clear the corresponding
review state.
```

- [ ] **Step 2: Run the full review scanner test module**

Run:

```bash
rtk python -m unittest tests.test_review
```

Expected: all tests in `tests.test_review` pass.

- [ ] **Step 3: Run focused CLI tests that may assert scan output**

Run:

```bash
rtk python -m unittest tests.test_cli
```

Expected: all tests in `tests.test_cli` pass. If a CLI snapshot-like assertion fails because `skipped_duplicate` is now printed, update that expected output to include the new line.

- [ ] **Step 4: Run syntax verification**

Run:

```bash
rtk python -m compileall -q rightmemory tests
```

Expected: no output and exit code `0`.

- [ ] **Step 5: Commit docs and regression fixes**

```bash
rtk git add README.md rightmemory/review.py tests/test_review.py tests/test_cli.py
rtk git commit -m "docs: describe review prefix dedupe"
```

If `tests/test_cli.py` was not changed, omit it from the `git add` command.

---

### Task 5: Final Verification

**Files:**
- Verify: `rightmemory/review.py`
- Verify: `tests/test_review.py`
- Verify: `README.md`

- [ ] **Step 1: Run the full local test suite**

Run:

```bash
rtk python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 2: Run syntax verification again**

Run:

```bash
rtk python -m compileall -q rightmemory tests
```

Expected: no output and exit code `0`.

- [ ] **Step 3: Inspect the final diff**

Run:

```bash
rtk git status --short
rtk git diff --stat HEAD
```

Expected: no unstaged source changes if all task commits were made. Unrelated pre-existing untracked files such as `.worktree/` and `docs/problems.md` may remain untracked and should not be committed unless the user explicitly asks.

- [ ] **Step 4: If final verification required a fix, commit it**

If Step 1 or Step 2 required a code or test fix, commit only the changed implementation files:

```bash
rtk git add rightmemory/review.py tests/test_review.py tests/test_cli.py README.md
rtk git commit -m "fix: stabilize review prefix dedupe"
```

If no fix was needed, do not create an empty commit.

---

## Self-Review Checklist

- The plan covers the approved prefix rule, longest representative selection, alias state marking after success, failure no-op behavior, provider-local boundaries, full-batch representative count, `reviewed` representative-only semantics, `skipped_duplicate`, README documentation, and verification.
- The plan intentionally excludes archived-chat filtering, persistent content fingerprint state, suffix-only review, and parser changes.
- The plan keeps existing review-state compatibility by writing aliases as normal reviewed sessions.
- The plan avoids committing unrelated untracked files.
