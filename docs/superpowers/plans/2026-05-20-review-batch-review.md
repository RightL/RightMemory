# Review Batch Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Change automatic transcript review from one session per scan to one time-adjacent batch per scan.

**Architecture:** Keep review state keyed by provider session, but make the scanner collect eligible candidates, sort them by transcript modification time, and invoke the reviewer with one ordered batch payload. Keep config compatibility by adding a defaulted `[review].batch_size` field, and update prompts/docs to describe batch review without changing memory file schema.

**Tech Stack:** Python dataclasses, `unittest`, TOML config via `tomllib`, existing RightMemory reviewer runtime.

---

## File Structure

- Modify `rightmemory/config.py`: add `DEFAULT_REVIEW_BATCH_SIZE`, add `ReviewConfig.batch_size`, parse and validate `[review].batch_size`.
- Modify `rightmemory/review.py`: collect eligible candidates, sort by time, build batch payloads, invoke reviewer once per batch, and mark included sessions reviewed on success.
- Modify `rightmemory/prompt.py`: update the reviewer role handoff text from session JSON to transcript batch JSON.
- Modify `rightmemory/prompts/reviewer.md`: minimally update reviewer instructions for ordered batches and batch commit messages.
- Modify `tests/test_config.py`: cover batch size default, parsing, invalid values, and reviewer prompt wording.
- Modify `tests/test_review.py`: cover batch selection, ordering, state updates, failure behavior, filtering, duplicate provider session ids, and mixed-provider payloads.
- Modify `README.md`: document batch review behavior.
- Modify `AGENTS.md`: update the reviewer scan description to match batch behavior. Keep the already-added upgrade safety note.

---

### Task 1: Review Config Batch Size

**Files:**
- Modify: `tests/test_config.py`
- Modify: `rightmemory/config.py`

- [ ] **Step 1: Write failing config tests**

In `tests/test_config.py`, update `test_review_config_sources` to include and assert `batch_size`:

```python
[review]
idle_seconds = 7200
since_days = 14
batch_size = 4
```

Add this assertion after the existing `since_days` assertion:

```python
self.assertEqual(config.batch_size, 4)
```

In `test_review_config_defaults_to_three_day_window`, add:

```python
self.assertEqual(config.batch_size, 3)
```

Add a new test near the existing review config tests:

```python
@patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
def test_review_config_rejects_invalid_batch_size(self):
    config_path = self._write_config(
        """
        [review]
        batch_size = 0
        """
    )

    with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
        with self.assertRaises(ValueError) as caught:
            load_review_config()

    self.assertIn("[review].batch_size must be a positive integer", str(caught.exception))
```

- [ ] **Step 2: Run config tests to verify they fail**

Run:

```bash
python -m unittest tests.test_config.ConfigTests.test_review_config_sources \
  tests.test_config.ConfigTests.test_review_config_defaults_to_three_day_window \
  tests.test_config.ConfigTests.test_review_config_rejects_invalid_batch_size
```

Expected: failure or error because `ReviewConfig` does not expose `batch_size`
and the config parser rejects unknown `[review].batch_size`.

- [ ] **Step 3: Implement config parsing**

In `rightmemory/config.py`, add the default near the other review defaults:

```python
DEFAULT_REVIEW_BATCH_SIZE = 3
```

Change `ReviewConfig` to include:

```python
@dataclass(frozen=True)
class ReviewConfig:
    memory_root: Path = MEMORY_ROOT
    idle_seconds: int = DEFAULT_REVIEW_IDLE_SECONDS
    since_days: int = DEFAULT_REVIEW_SINCE_DAYS
    batch_size: int = DEFAULT_REVIEW_BATCH_SIZE
    sources: list[ReviewSourceConfig] = field(default_factory=list)
```

In `load_review_config()`, allow the new key:

```python
_reject_unknown_keys(section, {"idle_seconds", "since_days", "batch_size", "sources"}, "[review]")
```

After `since_days` validation, add:

```python
batch_size = section.get("batch_size", DEFAULT_REVIEW_BATCH_SIZE)
if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
    raise ValueError("[review].batch_size must be a positive integer")
```

Return the value:

```python
return ReviewConfig(
    memory_root=MEMORY_ROOT,
    idle_seconds=idle_seconds,
    since_days=since_days,
    batch_size=batch_size,
    sources=sources,
)
```

- [ ] **Step 4: Run config tests to verify they pass**

Run the same command from Step 2.

Expected: all three tests pass.

- [ ] **Step 5: Commit config change**

```bash
git add rightmemory/config.py tests/test_config.py
git commit -m "feat: add review batch size config"
```

---

### Task 2: Scanner Batch Selection And State Updates

**Files:**
- Modify: `tests/test_review.py`
- Modify: `rightmemory/review.py`

- [ ] **Step 1: Write failing scanner tests**

In `tests/test_review.py`, update `test_scan_reviews_idle_session_and_updates_state` so it still expects one reviewed session, but now asserts the batch message:

```python
self.assertIn("Normalized transcript batch JSON", calls[0][1])
self.assertIn('"sessions"', calls[0][1])
self.assertIn('"batch_id"', calls[0][1])
```

Replace `test_scan_reviews_one_eligible_session_per_call` with:

```python
def test_scan_reviews_time_adjacent_batch_per_call(self):
    calls = []
    with tempfile.TemporaryDirectory() as tempdir:
        root = Path(tempdir)
        source = root / "codex"
        source.mkdir()
        first = source / "z-first.jsonl"
        second = source / "a-second.jsonl"
        third = source / "m-third.jsonl"
        fourth = source / "b-fourth.jsonl"
        self._write_codex(first, turns=[("first", "a1")], session_id="s1")
        self._write_codex(second, turns=[("second", "a2")], session_id="s2")
        self._write_codex(third, turns=[("third", "a3")], session_id="s3")
        self._write_codex(fourth, turns=[("fourth", "a4")], session_id="s4")
        self._set_mtime(second, 1_000)
        self._set_mtime(first, 2_000)
        self._set_mtime(fourth, 3_000)
        self._set_mtime(third, 4_000)
        scanner = ReviewScanner(
            ReviewConfig(
                memory_root=root,
                idle_seconds=3600,
                sources=[ReviewSourceConfig(kind="codex", path=source)],
            ),
            lambda session_id, message: calls.append((session_id, message)) or "ok",
        )

        first_result = scanner.scan_once(now=10_000)
        second_result = scanner.scan_once(now=10_000)
        state = ReviewStateStore(root).load()

    self.assertEqual(first_result.reviewed, 3)
    self.assertEqual(second_result.reviewed, 1)
    self.assertEqual(len(calls), 2)
    first_message = calls[0][1]
    self.assertIn('"user": "second"', first_message)
    self.assertIn('"user": "first"', first_message)
    self.assertIn('"user": "fourth"', first_message)
    self.assertNotIn('"user": "third"', first_message)
    self.assertLess(first_message.index('"user": "second"'), first_message.index('"user": "first"'))
    self.assertLess(first_message.index('"user": "first"'), first_message.index('"user": "fourth"'))
    self.assertEqual(len(state.sessions), 4)
```

Add this test for configured batch size:

```python
def test_scan_respects_configured_batch_size(self):
    calls = []
    with tempfile.TemporaryDirectory() as tempdir:
        root = Path(tempdir)
        source = root / "codex"
        source.mkdir()
        first = source / "01-first.jsonl"
        second = source / "02-second.jsonl"
        self._write_codex(first, turns=[("first", "a1")], session_id="s1")
        self._write_codex(second, turns=[("second", "a2")], session_id="s2")
        self._set_mtime(first, 1_000)
        self._set_mtime(second, 2_000)
        scanner = ReviewScanner(
            ReviewConfig(
                memory_root=root,
                idle_seconds=3600,
                batch_size=1,
                sources=[ReviewSourceConfig(kind="codex", path=source)],
            ),
            lambda session_id, message: calls.append(message) or "ok",
        )

        result = scanner.scan_once(now=10_000)
        state = ReviewStateStore(root).load()

    self.assertEqual(result.reviewed, 1)
    self.assertEqual(len(calls), 1)
    self.assertIn('"user": "first"', calls[0])
    self.assertNotIn('"user": "second"', calls[0])
    self.assertEqual(len(state.sessions), 1)
```

Update `test_scan_retries_once_then_stops_after_reviewer_failure` so the failed
batch includes both eligible sessions but marks neither reviewed:

```python
self.assertEqual(result.failed, 1)
self.assertEqual(result.retried, 1)
self.assertEqual(result.reviewed, 0)
self.assertEqual(len(calls), 2)
self.assertIn('"user": "fail"', calls[0])
self.assertIn('"user": "review"', calls[0])
self.assertEqual(len(state.sessions), 0)
```

Update `test_scan_skips_duplicate_session_id_from_different_file` to expect one
scan to review one deduplicated provider session:

```python
result = scanner.scan_once(now=10_000)

self.assertEqual(result.reviewed, 1)
self.assertEqual(result.skipped_reviewed, 1)
self.assertEqual(len(calls), 1)
self.assertIn('"user": "first"', calls[0])
```

Add a Claude helper near `_write_codex`:

```python
def _write_claude(self, path: Path, turns: list[tuple[str, str]], session_id: str = "c1") -> None:
    rows = []
    for index, (user, assistant) in enumerate(turns, start=1):
        rows.extend(
            [
                {
                    "type": "user",
                    "sessionId": session_id,
                    "cwd": "/repo",
                    "timestamp": f"t{index}.1",
                    "message": {"role": "user", "content": user},
                },
                {
                    "type": "assistant",
                    "sessionId": session_id,
                    "cwd": "/repo",
                    "timestamp": f"t{index}.2",
                    "message": {"role": "assistant", "stop_reason": "end_turn", "content": assistant},
                },
            ]
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
```

Add a mixed-provider test:

```python
def test_scan_allows_mixed_provider_batch(self):
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
        self._write_codex(codex_transcript, turns=[("codex user", "a1")], session_id="s1")
        self._write_claude(claude_transcript, turns=[("claude user", "a2")], session_id="c1")
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
    self.assertEqual(len(calls), 1)
    self.assertIn('"source": "codex"', calls[0])
    self.assertIn('"source": "claude"', calls[0])
    self.assertIn('"session_id": "s1"', calls[0])
    self.assertIn('"session_id": "c1"', calls[0])
```

- [ ] **Step 2: Run scanner tests to verify they fail**

Run:

```bash
python -m unittest tests.test_review.ReviewScannerTests
```

Expected: failures because scanner still sends a single-session payload and
returns after one reviewed session.

- [ ] **Step 3: Implement candidate collection and batch review**

In `rightmemory/review.py`, import `hashlib`:

```python
import hashlib
```

Add a candidate dataclass near `ReviewScanResult`:

```python
@dataclass(frozen=True)
class ReviewCandidate:
    transcript: TranscriptFile
    normalized: NormalizedSession
    mtime: float
```

Rewrite `ReviewScanner.scan_once()` so it collects candidates before invoking
the reviewer:

```python
def scan_once(self, *, now: float | None = None) -> ReviewScanResult:
    now = time.time() if now is None else now
    state = self.state_store.load()
    sessions = dict(state.sessions)
    candidates: list[ReviewCandidate] = []
    counts = {
        "reviewed": 0,
        "skipped_idle": 0,
        "skipped_old": 0,
        "skipped_reviewed": 0,
        "skipped_internal": 0,
        "skipped_empty": 0,
        "retried": 0,
        "failed": 0,
    }

    for source in self.config.sources:
        for transcript in _discover(source):
            try:
                stat = transcript.path.stat()
            except OSError:
                counts["skipped_empty"] += 1
                continue
            if now - stat.st_mtime > self.config.since_days * SECONDS_PER_DAY:
                counts["skipped_old"] += 1
                continue
            if now - stat.st_mtime < self.config.idle_seconds:
                counts["skipped_idle"] += 1
                continue

            normalized = _parse(transcript)
            if normalized is None or not normalized.turns:
                counts["skipped_empty"] += 1
                continue

            if ProviderSessionStore.is_internal_provider_session(
                self.config.memory_root,
                normalized.source,
                normalized.session_id,
            ):
                counts["skipped_internal"] += 1
                continue

            state_key = _state_key(normalized.source, normalized.session_id)
            if state_key in sessions:
                counts["skipped_reviewed"] += 1
                continue

            candidates.append(
                ReviewCandidate(
                    transcript=transcript,
                    normalized=normalized,
                    mtime=stat.st_mtime,
                )
            )

    sorted_candidates = sorted(
        candidates,
        key=lambda candidate: (
            candidate.mtime,
            candidate.transcript.path.as_posix(),
            candidate.normalized.source,
            candidate.normalized.session_id,
        ),
    )
    unique_candidates = []
    seen_candidate_keys: set[str] = set()
    for candidate in sorted_candidates:
        state_key = _state_key(candidate.normalized.source, candidate.normalized.session_id)
        if state_key in seen_candidate_keys:
            counts["skipped_reviewed"] += 1
            continue
        seen_candidate_keys.add(state_key)
        unique_candidates.append(candidate)

    batch = unique_candidates[: self.config.batch_size]
    if not batch:
        return ReviewScanResult(**counts)

    normalized_batch = [candidate.normalized for candidate in batch]
    if not self._review_with_retry(normalized_batch, counts):
        return ReviewScanResult(**counts)

    reviewed_at = datetime.now(UTC).isoformat()
    for session in normalized_batch:
        sessions[_state_key(session.source, session.session_id)] = ReviewSessionState(
            session_id=session.session_id,
            source=session.source,
            last_reviewed_at=reviewed_at,
        )
    self.state_store.save(ReviewState(sessions=sessions))
    counts["reviewed"] += len(normalized_batch)
    return ReviewScanResult(**counts)
```

Change `_review_with_retry()` to receive a list:

```python
def _review_with_retry(self, payload: list[NormalizedSession], counts: dict[str, int]) -> bool:
    session_id = _review_batch_id(payload)
    message = _review_message(payload)
    for attempt in range(REVIEW_MAX_RETRIES + 1):
        try:
            self.run_reviewer(session_id, message)
            return True
        except Exception:
            if attempt < REVIEW_MAX_RETRIES:
                counts["retried"] += 1
                continue
            counts["failed"] += 1
            return False
    return False
```

Replace `_review_session_id()` with batch id helpers:

```python
def _review_batch_id(sessions: list[NormalizedSession]) -> str:
    identifiers = [f"{session.source}-{session.session_id}" for session in sessions]
    digest = hashlib.sha1("\n".join(identifiers).encode("utf-8")).hexdigest()[:10]
    joined = "-".join(_safe_batch_id_part(identifier) for identifier in identifiers)
    if len(joined) > 80:
        joined = joined[:80].rstrip("._-")
    if joined:
        return f"review-batch-{joined}-{digest}"
    return f"review-batch-{digest}"


def _safe_batch_id_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value).strip("._-")
```

Change `_review_message()` to serialize the batch:

```python
def _review_message(sessions: list[NormalizedSession]) -> str:
    batch_id = _review_batch_id(sessions)
    payload = {
        "batch_id": batch_id,
        "sessions": [session.to_payload() for session in sessions],
    }
    return (
        "Review this normalized provider transcript batch.\n\n"
        "Review the ordered sessions together for durable memory. If nothing is worth saving, "
        "reply exactly: Nothing to save.\n\n"
        "Normalized transcript batch JSON:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
```

- [ ] **Step 4: Run scanner tests to verify they pass**

Run:

```bash
python -m unittest tests.test_review.ReviewScannerTests
```

Expected: all scanner tests pass.

- [ ] **Step 5: Commit scanner change**

```bash
git add rightmemory/review.py tests/test_review.py
git commit -m "feat: review transcript batches"
```

---

### Task 3: Reviewer Prompt And Runtime Handoff Text

**Files:**
- Modify: `rightmemory/prompts/reviewer.md`
- Modify: `rightmemory/prompt.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing prompt tests**

In `tests/test_config.py`, update `test_reviewer_prompt_has_role_prompt`:

Replace:

```python
self.assertIn("Normalized session JSON", prompt)
self.assertIn("Review the session as a whole", prompt)
```

with:

```python
self.assertIn("Normalized transcript batch JSON", prompt)
self.assertIn("Review the batch as a whole", prompt)
self.assertIn("ordered batch", prompt)
self.assertIn("memory: review transcript batch", prompt)
self.assertIn("<source>:<session_id>", prompt)
```

Add a focused assertion for the runtime handoff:

```python
self.assertIn("normalized transcript batch JSON", prompt)
```

- [ ] **Step 2: Run prompt test to verify it fails**

Run:

```bash
python -m unittest tests.test_config.ConfigTests.test_reviewer_prompt_has_role_prompt
```

Expected: failure because the prompt still says `Normalized session JSON`.

- [ ] **Step 3: Update reviewer runtime handoff**

In `rightmemory/prompt.py`, replace the reviewer role text with:

```python
if role == "reviewer":
    return (
        "- The automatic transcript review scanner selected reviewer behavior. Treat the normalized transcript "
        "batch JSON in the caller message as the review input.\n"
        "- Review the ordered batch for durable memory."
    )
```

- [ ] **Step 4: Update reviewer role prompt**

In `rightmemory/prompts/reviewer.md`, apply the approved narrow wording from
the design spec with short line wrapping. The key replacements are:

```markdown
Review an ordered batch of normalized provider chat sessions after they have
gone idle.
```

```markdown
The caller message includes `Normalized transcript batch JSON` with a
`batch_id` and ordered `sessions`.
```

```markdown
Review the batch as a whole.
```

```markdown
Prefer compact behavior or fact nodes over session or batch summaries.
```

```markdown
Use commit title `memory: review transcript batch`. Include the reviewed
sessions in the commit body as `<source>:<session_id>` entries.
```

```markdown
For edits, briefly list touched heading ids or node ids, reviewed sessions, and
any anomalies. For no-op reviews, reply exactly `Nothing to save.`
```

- [ ] **Step 5: Run prompt test to verify it passes**

Run:

```bash
python -m unittest tests.test_config.ConfigTests.test_reviewer_prompt_has_role_prompt
```

Expected: pass.

- [ ] **Step 6: Commit prompt change**

```bash
git add rightmemory/prompt.py rightmemory/prompts/reviewer.md tests/test_config.py
git commit -m "docs: update reviewer batch prompt"
```

---

### Task 4: README And AGENTS Operational Docs

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Update README automatic review text**

In `README.md`, rewrite the automatic review paragraphs so they say:

```markdown
It starts immediately and runs one-batch scans until no eligible work remains,
then sleeps before checking again. A reviewed or failed batch triggers another
immediate scan, so backlog and recovery attempts are not delayed by the
interval.
```

Replace the `scan --once` description with:

```markdown
Each `scan --once` command reviews at most one eligible batch and then exits.
By default a batch contains up to 3 provider sessions.
```

In the review config example, add:

```toml
batch_size = 3
```

Replace the state behavior paragraph with:

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

- [ ] **Step 2: Update AGENTS reviewer scan line**

In `AGENTS.md`, replace the reviewer scan bullet with:

```markdown
- Reviewer scans process one time-adjacent batch of eligible provider sessions per bounded scan. `scan --once` attempts one batch and exits; `watch` repeats batch scans until no eligible work remains. Review state remains session-level: once a provider session has been reviewed, later changes or resumed turns with the same source/session id are skipped unless the review state is cleared.
```

Do not expand the Upgrade Safety section; keep the short approved version.

- [ ] **Step 3: Check docs for stale one-session wording**

Run:

```bash
rg -n "one-session|one eligible session|Normalized session JSON|Review the session as a whole|review <source> transcript <session_id>" README.md AGENTS.md rightmemory tests
```

Expected: no stale hits related to reviewer batch behavior. If a hit remains in
a historical design doc under `docs/superpowers/specs/`, leave it because that
file documents the older design.

- [ ] **Step 4: Commit docs change**

```bash
git add README.md AGENTS.md
git commit -m "docs: describe review batch scans"
```

---

### Task 5: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run focused review and config tests**

Run:

```bash
python -m unittest tests.test_review tests.test_config tests.test_cli
```

Expected: all tests pass.

- [ ] **Step 2: Run syntax check**

Run:

```bash
python -m compileall -q rightmemory tests
```

Expected: no output and exit code 0.

- [ ] **Step 3: Run the full test suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git diff --stat HEAD~4..HEAD
git status --short
```

Expected: the four task commits contain code, tests, prompt, README, and
AGENTS updates. The worktree may still show unrelated pre-existing untracked
files such as `docs/path-location-memory-note.md` and
`docs/user-profile-goal-memory-note.md`; do not stage or remove them.

- [ ] **Step 5: Summarize implementation**

Prepare a concise final summary with:

- config field added: `[review].batch_size`;
- scanner behavior: time-adjacent batch payloads, default size 3;
- state behavior: session-level state preserved, success marks all, failure marks none;
- prompt/docs updated;
- verification commands and results.
