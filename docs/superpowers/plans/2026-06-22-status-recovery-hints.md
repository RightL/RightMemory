# Status Recovery Hints Implementation Plan

> **For agentic workers:** Execute inline in the current session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `rightmemory status` print concise recovery hints for every actionable issue shape it already knows about.

**Architecture:** Keep status collection factual and read-only. Add a pure formatter helper in `rightmemory/status.py` that classifies existing issue strings with ordered explicit patterns, dedupes hint lines, and appends a `Recovery` section after `Recent Issues`.

**Tech Stack:** Python standard library, `unittest`, existing RightMemory CLI/status modules.

---

## File Structure

- Modify: `rightmemory/status.py`
  - Add ordered recovery-hint pattern matching.
  - Keep collection functions unchanged.
  - Keep `format_status_dashboard()` as the only user-facing output integration point.
- Modify: `tests/test_status.py`
  - Add focused formatter tests for known issue shapes, dedupe, and clean output.
- Modify: `docs/superpowers/specs/2026-06-22-status-recovery-hints-design.md`
  - Already updated to clarify ordered pattern matching and the async stale-worker hint.

## Task 1: Add Failing Formatter Tests

**Files:**
- Modify: `tests/test_status.py`

- [x] **Step 1: Add comprehensive recovery formatter tests**

Add tests near the existing `test_format_status_dashboard_*` tests:

```python
    def test_format_status_dashboard_renders_recovery_hints_for_known_issue_shapes(self):
        dashboard = DashboardStatus(
            root=Path("/memory/root"),
            git=GitStatus(summary="dirty: 1 path", issue="dirty worktree: 1 path"),
            issues=[
                "git unavailable: not a git repository",
                "review: stale pid 456",
                "dreamer: running outside manager",
                "review: status error: ValueError: bad state",
                "pruner: rightmemory pruner check failed: RuntimeError: boom",
                "sync config error: ValueError: bad sync config",
                "dreamer trigger error: ValueError: dreamer trigger points must be a number",
                "insight trigger error: ValueError: insight trigger points must be a number",
                "update worker: stale pid 4",
                "update worker: state error: ValueError: async update worker state must contain string field: status",
                "update: state error: JSONDecodeError: Expecting value",
                "update: manual: manual recovery required: permanent boom",
                "update: manual-two: manual recovery required: another boom",
                "update: retrying: retrying after error: temporary boom",
                "update: agent-1: error: boom",
                "managed watches: status error: RuntimeError: collector failed",
                "dreamer: status error: RuntimeError: collector failed",
                "insight: status error: RuntimeError: collector failed",
                "update: status error: RuntimeError: collector failed",
            ],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn("Recovery", output)
        expected_hints = [
            "git: inspect with `git status --short`; resolve local changes before automatic writes continue",
            "git: inspect the configured memory root and repair Git before retrying",
            "review: run `rightmemory watch restart review`",
            "dreamer: stop the foreground process directly, then run `rightmemory watch start dreamer`",
            "review: rerun `rightmemory status`; inspect watch state if it persists",
            "pruner: inspect the shown log path, then run `rightmemory watch restart pruner` when appropriate",
            "sync: fix `rightmemory.toml`, then rerun `rightmemory status`",
            "dreamer: inspect `.runtime/dreamer/trigger-state.json`",
            "insight: inspect `.runtime/insight/trigger-state.json`",
            "update worker: inspect `.runtime/async/update/`; run `rightmemory update retry` only for manual recovery",
            "update worker: inspect `.runtime/async/update/_worker/state.json`",
            "update: inspect `.runtime/async/update/` for malformed session JSON",
            "update manual recovery: run `rightmemory update retry`",
            "update retrying: automatic retry is pending; inspect with `rightmemory update pull --session retrying`",
            "update agent-1: inspect with `rightmemory update pull --session agent-1`",
            "managed watches: rerun `rightmemory status`; inspect watch state if it persists",
            "dreamer: rerun `rightmemory status`; inspect dreamer state if it persists",
            "insight: rerun `rightmemory status`; inspect insight state if it persists",
            "update: rerun `rightmemory status`; inspect async update state if it persists",
        ]
        for hint in expected_hints:
            self.assertIn(f"  {hint}", output)
        self.assertEqual(output.count("update manual recovery: run `rightmemory update retry`"), 1)

    def test_format_status_dashboard_omits_recovery_when_no_hints_exist(self):
        dashboard = DashboardStatus(
            root=Path("/memory/root"),
            git=GitStatus(summary="clean on main @ abc1234"),
            issues=["plain informational issue without known recovery"],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn("Recent Issues", output)
        self.assertNotIn("Recovery", output)
```

- [x] **Step 2: Run focused tests and confirm failure**

Run:

```bash
rtk python -m unittest tests.test_status.StatusDashboardTests.test_format_status_dashboard_renders_recovery_hints_for_known_issue_shapes tests.test_status.StatusDashboardTests.test_format_status_dashboard_omits_recovery_when_no_hints_exist
```

Expected: the first test fails because `Recovery` is not rendered yet.

## Task 2: Implement Recovery Hint Formatting

**Files:**
- Modify: `rightmemory/status.py`

- [x] **Step 1: Add imports and helper functions**

Add `shlex` import and pure helper functions below `format_status_dashboard()`:

```python
import shlex
```

```python
def _dashboard_issues(status: DashboardStatus) -> list[str]:
    issues = list(status.issues)
    if status.git.issue:
        issues.insert(0, status.git.issue)
    return issues


def _recovery_hints(status: DashboardStatus) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()
    for issue in _dashboard_issues(status):
        hint = _recovery_hint_for_issue(issue)
        if hint and hint not in seen:
            hints.append(hint)
            seen.add(hint)
    return hints
```

- [x] **Step 2: Add ordered issue classification**

Add `_recovery_hint_for_issue()` with explicit ordering:

```python
def _recovery_hint_for_issue(issue: str) -> str | None:
    if issue.startswith("dirty worktree:"):
        return "git: inspect with `git status --short`; resolve local changes before automatic writes continue"
    if issue.startswith("git unavailable:"):
        return "git: inspect the configured memory root and repair Git before retrying"
    if issue.startswith("sync config error:"):
        return "sync: fix `rightmemory.toml`, then rerun `rightmemory status`"
    if issue.startswith("dreamer trigger error:"):
        return "dreamer: inspect `.runtime/dreamer/trigger-state.json`"
    if issue.startswith("insight trigger error:"):
        return "insight: inspect `.runtime/insight/trigger-state.json`"
    if issue.startswith("update worker: stale pid "):
        return "update worker: inspect `.runtime/async/update/`; run `rightmemory update retry` only for manual recovery"
    if issue.startswith("update worker: state error:"):
        return "update worker: inspect `.runtime/async/update/_worker/state.json`"
    if issue.startswith("update: state error:"):
        return "update: inspect `.runtime/async/update/` for malformed session JSON"
    if issue.startswith("managed watches: status error:"):
        return "managed watches: rerun `rightmemory status`; inspect watch state if it persists"
    if issue.startswith("dreamer: status error:"):
        return "dreamer: rerun `rightmemory status`; inspect dreamer state if it persists"
    if issue.startswith("insight: status error:"):
        return "insight: rerun `rightmemory status`; inspect insight state if it persists"
    if issue.startswith("update: status error:"):
        return "update: rerun `rightmemory status`; inspect async update state if it persists"
    update_hint = _update_recovery_hint(issue)
    if update_hint:
        return update_hint
    return _watch_recovery_hint(issue)
```

- [x] **Step 3: Add update and watch pattern helpers**

Add small helpers:

```python
def _update_recovery_hint(issue: str) -> str | None:
    match = re.match(r"^update: ([^:]+): manual recovery required:", issue)
    if match:
        return "update manual recovery: run `rightmemory update retry`"
    match = re.match(r"^update: ([^:]+): retrying after error:", issue)
    if match:
        session = shlex.quote(match.group(1))
        return f"update {match.group(1)}: automatic retry is pending; inspect with `rightmemory update pull --session {session}`"
    match = re.match(r"^update: ([^:]+): error:", issue)
    if match:
        session = shlex.quote(match.group(1))
        return f"update {match.group(1)}: inspect with `rightmemory update pull --session {session}`"
    return None


def _watch_recovery_hint(issue: str) -> str | None:
    for name in MANAGED_WATCH_TARGETS:
        prefix = f"{name}: "
        if not issue.startswith(prefix):
            continue
        detail = issue[len(prefix):]
        if detail.startswith("stale pid "):
            return f"{name}: run `rightmemory watch restart {name}`"
        if detail == "running outside manager":
            return f"{name}: stop the foreground process directly, then run `rightmemory watch start {name}`"
        if detail.startswith("status error:"):
            return f"{name}: rerun `rightmemory status`; inspect watch state if it persists"
        if _looks_like_failure(detail):
            return f"{name}: inspect the shown log path, then run `rightmemory watch restart {name}` when appropriate"
    return None
```

- [x] **Step 4: Append the Recovery section**

Update `format_status_dashboard()` to use `_dashboard_issues()` and append
deduped hints:

```python
    issues = _dashboard_issues(status)
    if issues:
        lines.append("")
        lines.append("Recent Issues")
        lines.extend(f"  {issue}" for issue in issues)
    hints = _recovery_hints(status)
    if hints:
        lines.append("")
        lines.append("Recovery")
        lines.extend(f"  {hint}" for hint in hints)
```

- [x] **Step 5: Run focused tests and confirm pass**

Run:

```bash
rtk python -m unittest tests.test_status.StatusDashboardTests.test_format_status_dashboard_renders_recovery_hints_for_known_issue_shapes tests.test_status.StatusDashboardTests.test_format_status_dashboard_omits_recovery_when_no_hints_exist
```

Expected: both tests pass.

## Task 3: Verify Status Behavior

**Files:**
- Modify: `tests/test_status.py`
- Modify: `rightmemory/status.py`

- [x] **Step 1: Run the status test module**

Run:

```bash
rtk python -m unittest tests.test_status
```

Expected: all status tests pass.

- [x] **Step 2: Run compile check**

Run:

```bash
rtk python -m compileall -q rightmemory tests
```

Expected: exits with status `0`.

- [x] **Step 3: Smoke test live status output**

Run:

```bash
rtk rightmemory status
```

Expected: output still renders the existing dashboard and, when current issues
are present, includes a `Recovery` section with deduped hints.

## Task 4: Commit Hygiene and Self-Review Gates

**Files:**
- Modify: `docs/superpowers/specs/2026-06-22-status-recovery-hints-design.md`
- Create: `docs/superpowers/plans/2026-06-22-status-recovery-hints.md`
- Modify: `rightmemory/status.py`
- Modify: `tests/test_status.py`

- [x] **Step 1: Inspect diff**

Run:

```bash
rtk git diff
rtk git status --short
```

Expected: only the four intended files changed, plus unrelated pre-existing
untracked files left untouched.

- [x] **Step 2: Amend all work into one commit**

Run:

```bash
rtk git add docs/superpowers/specs/2026-06-22-status-recovery-hints-design.md docs/superpowers/plans/2026-06-22-status-recovery-hints.md rightmemory/status.py tests/test_status.py
rtk git commit --amend -m "Add status recovery hints"
```

Expected: one commit contains the design update, plan, implementation, and
tests.

- [x] **Step 3: Run inline self-review gates**

Run inline self-review gates:

- spec-compliance review against `docs/superpowers/specs/2026-06-22-status-recovery-hints-design.md`;
- code-quality review against the final diff;
- fix any Critical or Important findings, verify again, and amend the commit.
