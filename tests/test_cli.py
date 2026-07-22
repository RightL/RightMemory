import io
import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rightmemory.async_update import WORKER_IDLE_POLL_SECONDS, AsyncUpdateState, AsyncUpdateStore
from rightmemory.cli import (
    _async_worker,
    _candidate_reference,
    _daemon_stdio_json,
    _dreamer_watch_once,
    _finish_sync_repair,
    _handle_json_request,
    _insight_watch_once,
    _require_completed_correction_operation,
    _recover_synchronized_update_operations,
    _run_active_sync_reconciler,
    _run_synchronized_review_correction,
    _run_synchronized_update_batch,
    _run_update_review_scan,
    _run_update_review_correction,
    _stored_correction_message,
    _verified_update_review,
    cli_main,
    main,
)
from rightmemory.config import DreamerWatchConfig, InsightWatchConfig, RuntimeConfig, SyncConfig
from rightmemory.dreamer_trigger import DreamerTriggerStore
from rightmemory.doctor import DoctorCheck
from rightmemory.hub.store import HubStore
from rightmemory.insight_trigger import InsightTriggerStore
from rightmemory.isolated_write import IsolatedWriteResult
from rightmemory.semantic_operation import OperationEffect, SemanticOperationStore
from rightmemory.share_results import ShareOperationResult
from rightmemory.shared_view_files import FileViewPullResult
from rightmemory.shared_view_models import SharedViewConnection, SharedViewTarget, load_shared_view_credential, save_connections
from rightmemory.watch import MANAGED_WATCH_TARGETS, WATCH_COMMANDS, _process_command, _write_pid, watch_stop_path
from rightmemory.update_review import (
    COMMENT_END,
    COMMENT_START,
    READY_LABEL,
    UpdateReviewProcessResult,
    UpdateReviewRequest,
    UpdateReviewStore,
    VerifiedUpdateReview,
    parse_review_markdown,
)
from rightmemory.update_queue import UpdateCandidate, UpdateQueueStore


def _verified_review_fixture() -> VerifiedUpdateReview:
    return VerifiedUpdateReview(
        review_id="review-1",
        origin_operation_id="original-operation",
        base_commit="base",
        creation_commit="update",
        write_surface="Memory",
        summary="trusted update summary",
        document_commit="c" * 40,
        document_blob_oid="d" * 40,
        question="",
        question_operation_id=None,
        diff="- before\n+ after",
        changed_paths=("MEMORY.md",),
    )


class FakeRuntime:
    def __init__(self, config=None):
        self.config = config
        self.session_turns = []
        self.last_write_result = None

    def run_turn(self, message: str, *, operation_id=None) -> str:
        return f"handled: {message}"

    def run_session_turn(self, session_id: str, message: str, **_kwargs) -> str:
        self.session_turns.append((session_id, message))
        return f"session {session_id}: {message}"

    def run_cycle(self, session_id: str, operator_hint=None, *, operation_id=None) -> str:
        self.session_turns.append((session_id, operator_hint))
        return f"cycle {session_id}: {operator_hint}"

    def run_prune_turn(self, session_id: str, pruner_config, *, operation_id=None) -> str:
        self.session_turns.append((session_id, f"prune:{pruner_config.memory_root}"))
        return f"prune session {session_id}: {pruner_config.memory_root}"

    def cleanup(self):
        pass


@contextmanager
def _fake_async_worker_process(pid: int = 123):
    # Tests that fake Popen must also satisfy the production cmdline identity check.
    with (
        patch("rightmemory.async_update._process_exists", return_value=True),
        patch("rightmemory.async_update.process_command", return_value="python -m rightmemory.cli update _async-worker"),
    ):
        yield pid


class CliEntrypointTests(unittest.TestCase):
    def test_candidate_reference_preserves_all_digit_uid_prefix(self):
        self.assertEqual(_candidate_reference("12345678"), "12345678")
        self.assertEqual(_candidate_reference("1234567"), 1234567)

    def test_offline_synchronized_queue_yields_to_never_published_local_work(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            store = AsyncUpdateStore(memory_root, "update")
            with patch.object(store, "_start_worker_if_needed"):
                attempted_state = store.submit("agent-a", "attempted")
                local_state = store.submit("agent-b", "local only")
            attempted = attempted_state.pending[0]
            UpdateQueueStore(memory_root).begin_publication(
                attempted.candidate_uid,
                attempted_at=attempted.submitted_at,
            )
            coordinator = Mock()
            coordinator.store = UpdateQueueStore(memory_root)
            coordinator.publish_outbox.side_effect = (
                SimpleNamespace(
                    settled_uids=(),
                    unresolved_uids=(attempted.candidate_uid,),
                    online=False,
                ),
                SimpleNamespace(
                    settled_uids=(attempted.candidate_uid,),
                    unresolved_uids=(),
                    online=True,
                ),
            )
            coordinator.claim_next.side_effect = (
                SimpleNamespace(claim=None, next_attempt_at=None, online=False),
                SimpleNamespace(claim=None, next_attempt_at=None, online=True),
            )
            run_batch = Mock(return_value="processed locally")

            with (
                patch(
                    "rightmemory.cli.load_async_update_config",
                    return_value=SimpleNamespace(
                        target_batch_candidates=1,
                        max_wait_seconds=0,
                    ),
                ),
                patch(
                    "rightmemory.cli.load_sync_config",
                    return_value=SyncConfig(memory_root=memory_root, enabled=True),
                ),
                patch(
                    "rightmemory.cli.GitUpdateQueueCoordinator",
                    return_value=coordinator,
                ),
                patch("rightmemory.cli._recover_synchronized_update_operations"),
                patch("rightmemory.cli._run_async_update_batch", run_batch),
            ):
                result = _async_worker(memory_root, "update")

            attempted_after = store.read("agent-a")
            local_after = store.read("agent-b")

        self.assertEqual(result, 0)
        self.assertEqual(attempted_after.pending, [])
        self.assertEqual(local_after.pending, [])
        run_batch.assert_called_once()
        self.assertIn("local only", run_batch.call_args.args[3])

    def test_remote_lease_wait_yields_to_ready_local_work(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            store = AsyncUpdateStore(memory_root, "update")
            with patch.object(store, "_start_worker_if_needed"):
                store.submit("agent-local", "process before remote lease expires")
            coordinator = Mock()
            coordinator.store = UpdateQueueStore(memory_root)
            coordinator.publish_outbox.return_value = SimpleNamespace(
                settled_uids=(),
                unresolved_uids=(),
                online=True,
            )
            coordinator.claim_next.side_effect = (
                SimpleNamespace(
                    claim=None,
                    next_attempt_at=datetime.now(UTC) + timedelta(hours=6),
                    online=True,
                ),
                SimpleNamespace(claim=None, next_attempt_at=None, online=True),
            )
            run_batch = Mock(return_value="processed locally")

            with (
                patch(
                    "rightmemory.cli.load_async_update_config",
                    return_value=SimpleNamespace(
                        target_batch_candidates=1,
                        max_wait_seconds=0,
                    ),
                ),
                patch(
                    "rightmemory.cli.load_sync_config",
                    return_value=SyncConfig(memory_root=memory_root, enabled=True),
                ),
                patch(
                    "rightmemory.cli.GitUpdateQueueCoordinator",
                    return_value=coordinator,
                ),
                patch("rightmemory.cli._recover_synchronized_update_operations"),
                patch("rightmemory.cli._run_async_update_batch", run_batch),
                patch(
                    "rightmemory.cli.time.sleep",
                    side_effect=AssertionError("ready local work must not wait for a remote lease"),
                ),
            ):
                result = _async_worker(memory_root, "update")

            final_state = store.read("agent-local")

        self.assertEqual(result, 0)
        self.assertEqual(final_state.pending, [])
        run_batch.assert_called_once()

    def test_remote_wait_polls_for_local_work_submitted_during_sleep(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            coordinator = Mock()
            coordinator.store = UpdateQueueStore(memory_root)
            coordinator.publish_outbox.return_value = SimpleNamespace(
                settled_uids=(),
                unresolved_uids=(),
                online=True,
            )
            coordinator.claim_next.side_effect = (
                SimpleNamespace(
                    claim=None,
                    next_attempt_at=datetime.now(UTC) + timedelta(hours=6),
                    online=True,
                ),
                SimpleNamespace(claim=None, next_attempt_at=None, online=True),
            )
            run_batch = Mock(return_value="processed locally")
            sleep_calls = []

            def submit_during_sleep(seconds):
                sleep_calls.append(seconds)
                store = AsyncUpdateStore(memory_root, "update")
                with patch.object(store, "_start_worker_if_needed"):
                    store.submit("agent-local", "arrived during remote wait")

            with (
                patch(
                    "rightmemory.cli.load_async_update_config",
                    return_value=SimpleNamespace(
                        target_batch_candidates=1,
                        max_wait_seconds=0,
                    ),
                ),
                patch(
                    "rightmemory.cli.load_sync_config",
                    return_value=SyncConfig(memory_root=memory_root, enabled=True),
                ),
                patch(
                    "rightmemory.cli.GitUpdateQueueCoordinator",
                    return_value=coordinator,
                ),
                patch("rightmemory.cli._recover_synchronized_update_operations"),
                patch("rightmemory.cli._run_async_update_batch", run_batch),
                patch("rightmemory.cli.time.sleep", side_effect=submit_during_sleep),
            ):
                result = _async_worker(memory_root, "update")

            final_state = AsyncUpdateStore(memory_root, "update").read("agent-local")

        self.assertEqual(result, 0)
        self.assertEqual(final_state.pending, [])
        self.assertEqual(len(sleep_calls), 1)
        self.assertLessEqual(sleep_calls[0], WORKER_IDLE_POLL_SECONDS)
        run_batch.assert_called_once()

    def test_synchronized_batch_releases_lease_when_local_runtime_cannot_load(self):
        coordinator = Mock()
        claim = SimpleNamespace(lease=SimpleNamespace(token="a" * 32))

        with patch("rightmemory.cli.load_config", side_effect=ValueError("missing model")):
            completed = _run_synchronized_update_batch(
                Path("/memory"),
                "update",
                coordinator,
                claim,
            )

        self.assertFalse(completed)
        coordinator.release.assert_called_once_with(claim)

    def test_synchronized_review_no_change_settles_through_queue_finalizer(self):
        candidate = UpdateCandidate(
            uid="a" * 32,
            session_id="review-" + "b" * 64,
            display_id=1,
            message="Keep the stable value.",
            submitted_at="2026-07-22T00:00:00+00:00",
            kind="review",
            review_id="review-" + "b" * 64,
            review_commit="c" * 40,
            review_blob_oid="d" * 40,
        )
        claim = SimpleNamespace(
            review_candidate=candidate,
            lease=SimpleNamespace(token="c" * 32),
            batch_id="update-batch-" + "d" * 64,
            session_id="update-batch-" + "d" * 64,
        )
        coordinator = Mock()
        coordinator.finalize.return_value = "landed"
        events = []

        class CorrectionRuntime:
            def __init__(self, config):
                events.append(("init", config))

            def prepare_session_turn_external(self, session_id, message, **kwargs):
                events.append(("prepare", session_id, message, kwargs))
                return IsolatedWriteResult(
                    output='{"status":"no_change","message":"Already correct."}',
                    commits_landed=0,
                    start_commit="base",
                    operation_id=claim.batch_id,
                    prepared=True,
                )

            def complete_session_turn_external(self, session_id, **kwargs):
                events.append(("complete", session_id, kwargs))

            def cleanup(self):
                events.append(("cleanup",))

        with (
            patch("rightmemory.cli.tracked_review_blob_oid", return_value="d" * 40),
            patch("rightmemory.cli.tracked_review_commit", return_value="c" * 40),
            patch("rightmemory.cli.verify_update_review", return_value=_verified_review_fixture()),
            patch("rightmemory.cli.load_update_corrector_config", return_value="config"),
            patch("rightmemory.cli.RightMemoryRuntime", CorrectionRuntime),
            patch("rightmemory.cli._stored_correction_message", return_value=None),
            patch("rightmemory.cli.SemanticOperationStore.read", return_value=None),
        ):
            completed = _run_synchronized_review_correction(
                Path("/memory"),
                coordinator,
                claim,
            )

        self.assertTrue(completed)
        outcome = coordinator.finalize.call_args.kwargs["review_outcome"]
        self.assertEqual(outcome.status, "resolved")
        self.assertIsNone(coordinator.finalize.call_args.args[1])
        self.assertEqual(events[-2][0], "complete")
        self.assertEqual(events[-1], ("cleanup",))

    def test_synchronized_stale_review_is_superseded_without_running_model(self):
        review_id = "review-" + "b" * 64
        candidate = UpdateCandidate(
            uid="a" * 32,
            session_id=review_id,
            display_id=1,
            message="Stale correction.",
            submitted_at="2026-07-22T00:00:00+00:00",
            kind="review",
            review_id=review_id,
            review_commit="c" * 40,
            review_blob_oid="d" * 40,
        )
        claim = SimpleNamespace(
            review_candidate=candidate,
            lease=SimpleNamespace(token="e" * 32),
            batch_id="update-batch-" + "f" * 64,
            session_id="update-batch-" + "f" * 64,
        )
        coordinator = Mock()
        coordinator.supersede_review.return_value = "terminal"

        with (
            patch("rightmemory.cli.tracked_review_blob_oid", return_value="d" * 40),
            patch("rightmemory.cli.tracked_review_commit", return_value="9" * 40),
            patch("rightmemory.cli.load_update_corrector_config") as load_config,
        ):
            completed = _run_synchronized_review_correction(
                Path("/memory"),
                coordinator,
                claim,
            )

        self.assertTrue(completed)
        coordinator.supersede_review.assert_called_once_with(claim)
        load_config.assert_not_called()

    def test_cli_main_reports_expected_errors_without_traceback(self):
        stderr = io.StringIO()

        with patch("rightmemory.cli.main", side_effect=ValueError("hub request failed: HTTP 404")), patch("sys.stderr", stderr):
            result = cli_main(["shared-view", "accept-invite", "https://hub.example.test/i/revoked"])

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), "error: hub request failed: HTTP 404\n")
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_synchronized_recovery_replays_effects_from_final_operation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            subprocess.run(["git", "add", "MEMORY.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
            start = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            batch_id = "update-batch-" + "a" * 64
            token = "1" * 32
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    f"finalize\n\nRightMemory-Queue-Token: {token}\nRightMemory-Queue-Batch: {batch_id}",
                ],
                cwd=root,
                check=True,
            )
            landed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            store = SemanticOperationStore(root)
            store.begin(
                batch_id,
                {"kind": "semantic-turn", "role": "update", "session_id": batch_id},
                effects=(OperationEffect("session-state"),),
            )
            store.prepare_outcome(
                batch_id,
                output="done",
                start_commit=start,
                changed_paths=(),
                metadata={"candidate_commit": None, "external_finalizer": f"update-queue:{token}"},
            )
            store.complete_no_change(batch_id, landed)
            calls = []

            class RecoveryRuntime:
                def __init__(self, _config):
                    pass

                def complete_session_turn_external(self, session_id, **kwargs):
                    calls.append((session_id, kwargs))
                    store.mark_effect(batch_id, "session-state", "done")

                def cleanup(self):
                    pass

            with (
                patch("rightmemory.cli.load_config", return_value=object()),
                patch("rightmemory.cli.RightMemoryRuntime", RecoveryRuntime),
            ):
                recovered = _recover_synchronized_update_operations(
                    root,
                    SyncConfig(memory_root=root, enabled=True),
                )

        self.assertEqual(recovered, 1)
        self.assertEqual(calls[0][0], batch_id)
        self.assertEqual(calls[0][1]["landed_commit"], landed)

    def test_synchronized_recovery_settles_abandoned_running_operation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            subprocess.run(["git", "add", "MEMORY.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
            batch_id = "update-batch-" + "b" * 64
            store = SemanticOperationStore(root)
            store.begin(
                batch_id,
                {"kind": "semantic-turn", "role": "update", "session_id": batch_id},
            )
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    f"finalize\n\nRightMemory-Queue-Batch: {batch_id}",
                ],
                cwd=root,
                check=True,
            )
            landed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            calls = []

            class RecoveryRuntime:
                def __init__(self, _config):
                    pass

                def supersede_running_session_turn(self, session_id, **kwargs):
                    calls.append((session_id, kwargs))
                    store.supersede_running(
                        batch_id,
                        landed_commit=kwargs["landed_commit"],
                        reason="settled by Git",
                    )

                def cleanup(self):
                    pass

            with (
                patch("rightmemory.cli.load_config", return_value=object()),
                patch("rightmemory.cli.RightMemoryRuntime", RecoveryRuntime),
            ):
                recovered = _recover_synchronized_update_operations(
                    root,
                    SyncConfig(memory_root=root, enabled=True),
                )

            final = store.read(batch_id)

        self.assertEqual(recovered, 1)
        self.assertEqual(calls[0][0], batch_id)
        self.assertEqual(final.phase, "no_change")
        self.assertEqual(final.outcome.landed_commit, landed)

    def test_sync_finisher_loads_runtime_for_pending_state_without_result_id(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            operation_id = f"sync-repair-{'e' * 64}"
            store = SemanticOperationStore(root)
            store.begin(operation_id, {"kind": "sync-repair", "role": "sync-reconciler"})
            store.prepare_outcome(
                operation_id,
                output='{"files":[],"message":"published","status":"synced"}',
                start_commit="base123",
                changed_paths=("MEMORY.md",),
                effects=(OperationEffect("session-state"),),
                metadata={"candidate_commit": "tip456"},
            )
            store.complete_commit(operation_id, "tip456")
            result = type(
                "Result",
                (),
                {"status": "fresh", "message": "fresh", "files": [], "operation_id": None},
            )()

            with (
                patch("rightmemory.cli.load_config", return_value=object()),
                patch("rightmemory.cli.RightMemoryRuntime") as runtime_class,
            ):
                _finish_sync_repair(root, result)

            runtime_class.return_value._finish_sync_repair.assert_called_once_with(result)
            runtime_class.return_value.cleanup.assert_called_once_with()


class ShareCliTests(unittest.TestCase):
    def test_share_create_dispatches_to_create_share(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.create_share", return_value="created share auth-api") as create,
                patch("sys.stdout", stdout),
            ):
                result = main(
                    [
                        "share",
                        "create",
                        "auth-api",
                        "--title",
                        "Auth API",
                        "--provider",
                        "alice",
                        "--hub-url",
                        "https://hub.example.test",
                        "--credential-id",
                        "alice-publish",
                        "--file",
                        "Expose auth API integration context.",
                        "--question",
                        "Let frontend agents ask auth questions.",
                        "--question-base-url",
                        "https://provider.example.test",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertIn("created share auth-api", stdout.getvalue())
        create.assert_called_once()
        self.assertEqual(create.call_args.args[:2], (root, "auth-api"))
        self.assertEqual(create.call_args.kwargs["provider_id"], "alice")
        self.assertEqual(create.call_args.kwargs["file_intent"], "Expose auth API integration context.")

    def test_share_publish_dispatches_to_publish_share(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch(
                    "rightmemory.cli.publish_share",
                    return_value="published share auth-api\ninvitation_url\thttps://hub/i/share/token",
                ) as publish,
                patch("sys.stdout", stdout),
            ):
                result = main(["share", "publish", "auth-api", "--label", "frontend"])

        self.assertEqual(result, 0)
        publish.assert_called_once_with(
            root,
            "auth-api",
            label="frontend",
            expires_at=None,
            git_url=None,
            git_branch=None,
            push=True,
        )
        self.assertIn("https://hub/i/share/token", stdout.getvalue())

    def test_share_publish_no_push_dispatches_to_publish_share(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.publish_share", return_value="published share auth-api") as publish,
                patch("sys.stdout", stdout),
            ):
                result = main(["share", "publish", "auth-api", "--no-push"])

        self.assertEqual(result, 0)
        publish.assert_called_once_with(
            root,
            "auth-api",
            label=None,
            expires_at=None,
            git_url=None,
            git_branch=None,
            push=False,
        )

    def test_share_create_request_dispatches_to_create_share(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch(
                    "rightmemory.cli.create_share",
                    return_value="auth-api provider draft capability=both\n\nBuilder summary:\nBuilt it.\n",
                ) as create,
                patch("sys.stdout", stdout),
            ):
                result = main(
                    [
                        "share",
                        "create",
                        "auth-api",
                        "--provider",
                        "alice",
                        "--hub-url",
                        "https://hub.example.test",
                        "--credential-id",
                        "alice-publish",
                        "--request",
                        "Share auth API context and allow live questions.",
                        "--capability",
                        "both",
                        "--question-base-url",
                        "https://provider.example.test",
                    ]
                )

        self.assertEqual(result, 0)
        create.assert_called_once()
        self.assertIsNone(create.call_args.kwargs["title"])
        self.assertEqual(create.call_args.kwargs["request"], "Share auth API context and allow live questions.")
        self.assertEqual(create.call_args.kwargs["capability"], "both")
        self.assertIn("Built it.", stdout.getvalue())

    def test_share_create_git_request_dispatches_to_create_share(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch(
                    "rightmemory.cli.create_share",
                    return_value="auth-api provider draft capability=file_context\n",
                ) as create,
                patch("sys.stdout", stdout),
            ):
                result = main(
                    [
                        "share",
                        "create",
                        "auth-api",
                        "--provider",
                        "alice",
                        "--request",
                        "Share auth API context.",
                        "--git",
                        "https://github.com/user/repo.git",
                        "--branch",
                        "gh-pages",
                    ]
                )

        self.assertEqual(result, 0)
        create.assert_called_once()
        self.assertIsNone(create.call_args.kwargs["hub_url"])
        self.assertIsNone(create.call_args.kwargs["credential_id"])
        self.assertEqual(create.call_args.kwargs["git_url"], "https://github.com/user/repo.git")
        self.assertEqual(create.call_args.kwargs["git_branch"], "gh-pages")
        self.assertEqual(create.call_args.kwargs["capability"], "auto")

    def test_share_revise_dispatches_to_revise_share(self):
        stdout = io.StringIO()
        expected = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="provider",
            state="draft",
            capability="live_questions",
            builder_final_message="Updated scope.",
            next_action="rightmemory share approve auth-api",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.revise_share", return_value=expected) as revise,
                patch("sys.stdout", stdout),
            ):
                result = main(
                    [
                        "share",
                        "revise",
                        "auth-api",
                        "--capability",
                        "live-questions",
                        "Only answer refresh token questions.",
                    ]
                )

        self.assertEqual(result, 0)
        revise.assert_called_once_with(
            root,
            "auth-api",
            "Only answer refresh token questions.",
            capability="live-questions",
            question_base_url=None,
        )
        self.assertIn("Updated scope.", stdout.getvalue())
        self.assertIn("rightmemory share approve auth-api", stdout.getvalue())

    def test_share_join_dispatches_to_join_share(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.join_share", return_value="joined share auth-api") as join,
                patch("sys.stdout", stdout),
            ):
                result = main(["share", "join", "https://hub.example.test/i/share/token", "--consumer-label", "frontend"])

        self.assertEqual(result, 0)
        join.assert_called_once_with(root, "https://hub.example.test/i/share/token", consumer_label="frontend")
        self.assertIn("joined share auth-api", stdout.getvalue())


class FakeReviewResult:
    def __init__(self, text: str, reviewed: int = 0, failed: int = 0):
        self.text = text
        self.reviewed = reviewed
        self.failed = failed

    def format(self):
        return self.text


def _dreamer_watch_config(
    memory_root: Path | None = None,
    trigger_points: float = 50.0,
    update_candidate_points: float = 1.0,
    check_interval_seconds: int = 3000,
):
    return DreamerWatchConfig(
        memory_root=Path("/unused") if memory_root is None else memory_root,
        trigger_points=trigger_points,
        update_candidate_points=update_candidate_points,
        check_interval_seconds=check_interval_seconds,
    )


def _insight_watch_config(
    memory_root: Path | None = None,
    trigger_points: float = 150.0,
    update_candidate_points: float = 1.0,
    check_interval_seconds: int = 3000,
):
    return InsightWatchConfig(
        memory_root=Path("/unused") if memory_root is None else memory_root,
        trigger_points=trigger_points,
        update_candidate_points=update_candidate_points,
        check_interval_seconds=check_interval_seconds,
    )


def _async_update_config(memory_root: Path, *, target: int = 15, max_wait: int = 86400):
    return type(
        "AsyncUpdateConfig",
        (),
        {
            "memory_root": memory_root,
            "target_batch_candidates": target,
            "max_wait_seconds": max_wait,
        },
    )()


class JsonRequestTests(unittest.TestCase):
    def test_handle_json_request(self):
        response = _handle_json_request(FakeRuntime(), {"message": "hello"})

        self.assertEqual(response, {"type": "assistant", "message": "handled: hello"})

    def test_handle_json_request_records_memory_changing_update_pressure(self):
        memory_root = Path("/memory")
        runtime = FakeRuntime(type("Config", (), {"role": "update", "memory_root": memory_root})())
        runtime.last_write_result = IsolatedWriteResult(
            output="updated",
            commits_landed=1,
            changed_paths=("MEMORY.md", "PURSUITS.md"),
        )

        with patch("rightmemory.cli._record_memory_change_pressure") as pressure:
            response = _handle_json_request(
                runtime,
                {"message": "hello", "operation_id": "daemon-update-1"},
            )

        self.assertEqual(
            response,
            {
                "type": "assistant",
                "message": "handled: hello",
                "operation_id": "daemon-update-1",
            },
        )
        pressure.assert_called_once_with(memory_root)

    def test_automatic_writer_json_request_requires_reusable_operation_id(self):
        runtime = FakeRuntime(type("Config", (), {"role": "dreamer"})())

        with self.assertRaisesRegex(ValueError, "require string field: operation_id"):
            _handle_json_request(runtime, {"message": "dream"})

    def test_json_request_passes_operation_id_to_runtime_and_returns_it(self):
        calls = []

        class Runtime(FakeRuntime):
            def run_turn(self, message: str, *, operation_id=None) -> str:
                calls.append((message, operation_id))
                return "saved"

        runtime = Runtime(type("Config", (), {"role": "dreamer"})())
        response = _handle_json_request(
            runtime,
            {"message": "dream", "operation_id": "daemon-dream-1"},
        )

        self.assertEqual(calls, [("dream", "daemon-dream-1")])
        self.assertEqual(response["operation_id"], "daemon-dream-1")

    def test_handle_json_request_requires_message(self):
        with self.assertRaises(ValueError):
            _handle_json_request(FakeRuntime(), {})

    def test_daemon_stdio_json_handles_json_lines(self):
        stdin = io.StringIO('{"message":"hello"}\n\n{"bad":true}\n')
        stdout = io.StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            result = _daemon_stdio_json(FakeRuntime())

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(result, 0)
        self.assertEqual(lines[0], {"type": "assistant", "message": "handled: hello"})
        self.assertEqual(lines[1]["type"], "error")

    def test_main_loads_retrieve_role(self):
        roles = []

        def fake_load_config(role, **kwargs):
            roles.append(role)
            return object()

        with patch("rightmemory.cli.load_config", fake_load_config), patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime):
            result = main(["retrieve", "daemon", "--stdio-json"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["retrieve"])

    def test_main_loads_dreamer_role(self):
        roles = []

        def fake_load_config(role, **kwargs):
            roles.append(role)
            return object()

        with patch("rightmemory.cli.load_config", fake_load_config), patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime):
            result = main(["dreamer", "daemon", "--stdio-json"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["dreamer"])

    def test_main_loads_reviewer_role(self):
        roles = []

        def fake_load_config(role, **kwargs):
            roles.append(role)
            return object()

        with patch("rightmemory.cli.load_config", fake_load_config), patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime):
            result = main(["reviewer", "daemon", "--stdio-json"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["reviewer"])

    def test_history_command_uses_historian_role(self):
        roles = []
        stdout = io.StringIO()

        def fake_load_config(role, **kwargs):
            roles.append(role)
            return object()

        with (
            patch("rightmemory.cli.load_config", fake_load_config),
            patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
            patch("sys.stdout", stdout),
        ):
            result = main(["history", "--session", "hist-1", "old", "context"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["historian"])
        self.assertIn("session hist-1: old context", stdout.getvalue())

    def test_main_global_profile_selects_registered_root(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            profile_root = Path(tempdir) / "project-memory"
            profile_root.mkdir(parents=True)
            default_root.mkdir()
            (default_root / "profiles.toml").write_text(
                f"[profiles.alpha]\nroot = {json.dumps(str(profile_root))}\n",
                encoding="utf-8",
            )

            def fake_load_config(role, **kwargs):
                self.assertEqual(kwargs.get("memory_root"), profile_root)
                return type("Config", (), {"memory_root": profile_root})()

            with (
                patch("rightmemory.cli.default_memory_root", return_value=default_root),
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("sys.stdout", stdout),
            ):
                result = main(["--profile", "alpha", "retrieve", "--session", "s1", "hello"])

        self.assertEqual(result, 0)
        self.assertIn("session s1: hello", stdout.getvalue())

    def test_main_project_binding_selects_registered_root(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            profile_root = Path(tempdir) / "profile-root"
            project = Path(tempdir) / "project"
            project.mkdir()
            default_root.mkdir()
            (default_root / "profiles.toml").write_text(
                f"[profiles.alpha]\nroot = {json.dumps(str(profile_root))}\n",
                encoding="utf-8",
            )
            (project / ".rightmemory-profile").write_text("alpha\n", encoding="utf-8")

            def fake_load_config(role, **kwargs):
                self.assertEqual(kwargs.get("memory_root"), profile_root)
                return type("Config", (), {"memory_root": profile_root})()

            with (
                patch("rightmemory.cli.default_memory_root", return_value=default_root),
                patch("rightmemory.cli.Path.cwd", return_value=project),
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("sys.stdout", stdout),
            ):
                result = main(["retrieve", "--session", "s1", "hello"])

        self.assertEqual(result, 0)
        self.assertIn("session s1: hello", stdout.getvalue())

    def test_build_file_cli_runs_builder_agent(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.run_file_view_builder", return_value="built file view auth-api-files") as builder,
                patch("sys.stdout", stdout),
            ):
                result = main([
                    "shared-view",
                    "build-file",
                    "auth-api-files",
                    "Expose",
                    "auth",
                    "API",
                    "context",
                    "--title",
                    "Auth API Files",
                    "--hub-url",
                    "https://hub.example.test",
                    "--credential-id",
                    "alice-publish",
                ])

        self.assertEqual(result, 0)
        self.assertEqual(builder.call_args.kwargs["intent"], "Expose auth API context")
        self.assertEqual(builder.call_args.kwargs["hub_url"], "https://hub.example.test")
        self.assertEqual(builder.call_args.kwargs["credential_id"], "alice-publish")
        self.assertIn("built file view", stdout.getvalue())

    def test_build_question_cli_runs_builder_agent(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.run_question_view_builder", return_value="built question view auth-api-ask") as builder,
                patch("sys.stdout", stdout),
            ):
                result = main([
                    "shared-view",
                    "build-question",
                    "auth-api-ask",
                    "Let",
                    "frontend",
                    "agents",
                    "ask",
                    "auth",
                    "questions",
                    "--title",
                    "Auth API Questions",
                ])

        self.assertEqual(result, 0)
        self.assertEqual(builder.call_args.kwargs["intent"], "Let frontend agents ask auth questions")
        self.assertIn("built question view", stdout.getvalue())

    def test_shared_view_refresh_file_invokes_maintenance_entrypoint(self):
        calls = []

        def fake_refresh(memory_root, view_id, *, force=False, publish=False):
            calls.append((memory_root, view_id, force, publish))
            return "refreshed file view auth-api-files"

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.refresh_file_view", side_effect=fake_refresh),
                patch("sys.stdout", stdout),
            ):
                result = main([
                    "shared-view",
                    "refresh-file",
                    "auth-api-files",
                    "--force",
                    "--publish",
                ])

        self.assertEqual(result, 0)
        self.assertEqual(calls, [(root, "auth-api-files", True, True)])
        self.assertIn("refreshed file view auth-api-files", stdout.getvalue())

    def test_shared_view_approve_cli_dispatches_by_type(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.approve_file_view", return_value="approved file view auth-api-files") as approve,
                patch("sys.stdout", stdout),
            ):
                result = main(["shared-view", "approve", "auth-api-files", "--type", "file"])

        self.assertEqual(result, 0)
        approve.assert_called_once_with(root, "auth-api-files")
        self.assertIn("approved file view", stdout.getvalue())

    def test_shared_view_pull_cli_dispatches_file_view(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.pull_file_view", return_value=FileViewPullResult("auth-api-files", "pulled", "file view pulled")) as pull,
                patch("sys.stdout", stdout),
            ):
                result = main(["shared-view", "pull", "auth-api-files"])

        self.assertEqual(result, 0)
        pull.assert_called_once_with(root, "auth-api-files")
        self.assertIn("file view pulled", stdout.getvalue())

    def test_shared_view_status_cli_reports_file_import_state(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            save_connections(
                root,
                {
                    "auth-api-files": SharedViewConnection(
                        heading_id="auth-api-files",
                        view_type="file",
                        ref="rightmemory://mf/auth-api-files",
                        target=SharedViewTarget(
                            kind="http-file",
                            base_url="https://hub.example.test",
                            credential_id="http-auth-api-files",
                        ),
                    )
                },
            )
            package = root / ".runtime" / "shared_views" / "imports" / "auth-api-files"
            imported = package / "dist"
            imported.mkdir(parents=True)
            (package / "view.md").write_text("# Auth API Files\n", encoding="utf-8")
            (package / "recipe.toml").write_text(
                'version = 1\nview_id = "auth-api-files"\nkind = "file"\n',
                encoding="utf-8",
            )
            (package / "rightmemory-shared-view.toml").write_text(
                'version = 2\nview_id = "auth-api-files"\nkind = "file"\n',
                encoding="utf-8",
            )
            (imported / "MEMORY.md").write_text(
                "# Auth API {#auth-api} → []\n\nPublished context.\n",
                encoding="utf-8",
            )
            (imported / "manifest.toml").write_text(
                'version = 2\nview_id = "auth-api-files"\n'
                'document_kind = "rightmemory-memory"\n',
                encoding="utf-8",
            )

            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("sys.stdout", stdout),
            ):
                result = main(["shared-view", "status", "auth-api-files"])

        self.assertEqual(result, 0)
        self.assertEqual(
            stdout.getvalue().strip(),
            "auth-api-files\tfile\thttp-file\timported\tfile view import is available",
        )

    def test_shared_view_ask_cli_dispatches_question_view(self):
        calls = []

        def fake_ask(memory_root, heading_id, question):
            calls.append((memory_root, heading_id, question))
            return "Shared question: auth-api-ask\nStatus: answered\nAnswer: Use token_expires_at.\n"

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.ask_question_view", side_effect=fake_ask),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                result = main(["shared-view", "ask", "auth-api-ask", "How", "do", "tokens", "refresh?"])

        self.assertEqual(result, 0)
        self.assertEqual(calls[0], (root, "auth-api-ask", "How do tokens refresh?"))
        self.assertIn("Status: answered", stdout.getvalue())

    def test_shared_view_publish_question_cli_dispatches_question_publisher(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch(
                    "rightmemory.cli.publish_question_view",
                    return_value="published question view auth-api-ask\ninvitation_url\thttps://hub.example.test/i/invite-token",
                ) as publish,
                patch("sys.stdout", stdout),
            ):
                result = main(
                    [
                        "shared-view",
                        "publish-question",
                        "auth-api-ask",
                        "--hub-url",
                        "https://hub.example.test",
                        "--credential-id",
                        "alice-publish",
                        "--question-base-url",
                        "https://provider.example.test",
                        "--label",
                        "frontend",
                        "--expires-at",
                        "2026-07-01T00:00:00+00:00",
                    ]
                )

        self.assertEqual(result, 0)
        publish.assert_called_once_with(
            root,
            "auth-api-ask",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            question_base_url="https://provider.example.test",
            label="frontend",
            expires_at="2026-07-01T00:00:00+00:00",
        )
        self.assertIn("invitation_url", stdout.getvalue())

    def test_shared_view_invite_cli_dispatches_file_view_inviter(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch(
                    "rightmemory.cli.invite_file_view",
                    return_value="invited file view auth-api-files\ninvitation_url\thttps://hub.example.test/i/invite-token",
                ) as invite,
                patch("sys.stdout", stdout),
            ):
                result = main(
                    [
                        "shared-view",
                        "invite",
                        "auth-api-files",
                        "--hub-url",
                        "https://hub.example.test",
                        "--credential-id",
                        "alice-publish",
                        "--label",
                        "frontend",
                        "--expires-at",
                        "2026-07-01T00:00:00+00:00",
                    ]
                )

        self.assertEqual(result, 0)
        invite.assert_called_once_with(
            root,
            "auth-api-files",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            label="frontend",
            expires_at="2026-07-01T00:00:00+00:00",
        )
        self.assertIn("invitation_url", stdout.getvalue())

    def test_shared_view_legacy_commands_are_removed(self):
        for command in ("define", "build", "export", "publish", "publish-http", "retrieve", "accept"):
            with self.subTest(command=command):
                with self.assertRaises(SystemExit):
                    main(["shared-view", command])

    def test_shared_view_list_cli_prints_connections(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            save_connections(
                root,
                {
                    "auth-api-files": SharedViewConnection(
                        heading_id="auth-api-files",
                        view_type="file",
                        ref="rightmemory://mf/auth-api-files",
                        relationship="human",
                        maintainer="Alice",
                        description="Auth API file context",
                    )
                },
            )

            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("sys.stdout", stdout),
            ):
                result = main(["shared-view", "list"])

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().strip(), "auth-api-files\tfile\thuman\tAlice\tAuth API file context")

    def test_shared_view_note_cli_requires_confirmation_for_human_connection(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            save_connections(
                root,
                {
                    "auth-api-files": SharedViewConnection(
                        heading_id="auth-api-files",
                        view_type="file",
                        ref="rightmemory://mf/auth-api-files",
                        relationship="human",
                        maintainer="Alice",
                    )
                },
            )

            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("sys.stdout", stdout),
            ):
                result = main(["shared-view", "note", "auth-api-files", "Docs", "are", "stale"])

        self.assertEqual(result, 0)
        self.assertIn("confirmation required", stdout.getvalue())

    def test_shared_view_accept_invite_cli_dispatches_http_urls(self):
        stdout = io.StringIO()
        events = []

        class FakeMemoryWriteLock:
            def __init__(self, memory_root):
                self.memory_root = memory_root

            def __enter__(self):
                events.append(("lock_enter", self.memory_root))
                return self

            def __exit__(self, exc_type, exc, traceback):
                events.append(("lock_exit", exc_type))

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            def fake_accept_http(memory_root, invitation_url, **kwargs):
                events.append(("accept_http", memory_root, invitation_url, kwargs["heading_id"]))
                return "accepted shared view remote-auth"

            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.MemoryWriteLock", FakeMemoryWriteLock),
                patch("rightmemory.cli.accept_http_shared_view_invitation", side_effect=fake_accept_http),
                patch("sys.stdout", stdout),
            ):
                result = main([
                    "shared-view",
                    "accept-invite",
                    "https://hub.example.test/i/invite-token",
                    "--heading-id",
                    "remote-auth",
                ])

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            [
                ("lock_enter", root),
                ("accept_http", root, "https://hub.example.test/i/invite-token", "remote-auth"),
                ("lock_exit", None),
            ],
        )
        self.assertIn("accepted shared view remote-auth", stdout.getvalue())

    def test_shared_view_credential_set_stores_secret_in_runtime(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("sys.stdout", stdout),
                patch("sys.stdin", io.StringIO("secret-token\n")),
            ):
                result = main(
                    [
                        "shared-view",
                        "credential",
                        "set",
                        "alice-publish",
                        "--kind",
                        "http-publish",
                        "--hub-url",
                        "https://hub.example.test",
                        "--provider",
                        "alice",
                        "--token-stdin",
                    ]
                )

            credential = load_shared_view_credential(root, "alice-publish")

        self.assertEqual(result, 0)
        self.assertEqual(credential["kind"], "http-publish")
        self.assertEqual(credential["token"], "secret-token")
        self.assertEqual(credential["base_url"], "https://hub.example.test")
        self.assertEqual(credential["provider_id"], "alice")
        self.assertIn("saved shared view credential alice-publish", stdout.getvalue())

    def test_shared_view_credential_set_can_read_token_from_prompt(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.getpass.getpass", return_value="prompt-token"),
                patch("sys.stdout", stdout),
            ):
                result = main(
                    [
                        "shared-view",
                        "credential",
                        "set",
                        "alice-publish",
                        "--kind",
                        "http-publish",
                        "--hub-url",
                        "https://hub.example.test",
                        "--provider",
                        "alice",
                        "--token-prompt",
                    ]
                )

            credential = load_shared_view_credential(root, "alice-publish")

        self.assertEqual(result, 0)
        self.assertEqual(credential["token"], "prompt-token")
        self.assertIn("saved shared view credential alice-publish", stdout.getvalue())

    def test_shared_view_inbox_http_cli_prints_remote_records(self):
        stdout = io.StringIO()
        calls = []

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            def fake_inbox(memory_root, *, hub_url, credential_id, provider_id):
                calls.append((memory_root, hub_url, credential_id, provider_id))
                return [{"view_id": "alice-auth-api", "payload": {"message": "Docs are stale"}}]

            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.list_http_shared_view_inbox", side_effect=fake_inbox),
                patch("sys.stdout", stdout),
            ):
                result = main(
                    [
                        "shared-view",
                        "inbox-http",
                        "--hub-url",
                        "https://hub.example.test",
                        "--credential-id",
                        "alice-publish",
                        "--provider",
                        "alice",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(calls, [(root, "https://hub.example.test", "alice-publish", "alice")])
        self.assertIn('"message": "Docs are stale"', stdout.getvalue())

    def test_hub_init_create_token_revoke_and_status_cli(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            hub_root = Path(tempdir) / "hub"

            with patch("sys.stdout", stdout):
                init_result = main(
                    [
                        "hub",
                        "init",
                        str(hub_root),
                        "--admin-token",
                        "admin-secret",
                        "--public-base-url",
                        "https://hub.example.test",
                    ]
                )
                create_result = main(
                    [
                        "hub",
                        "token",
                        "create",
                        str(hub_root),
                        "--provider",
                        "alice",
                        "--label",
                        "publish",
                    ]
                )
                status_result = main(["hub", "status", str(hub_root)])
                second_init_result = main(["hub", "init", str(hub_root)])
                same_url_init_result = main(
                    [
                        "hub",
                        "init",
                        str(hub_root),
                        "--public-base-url",
                        "https://hub.example.test",
                    ]
                )
                same_admin_init_result = main(
                    [
                        "hub",
                        "init",
                        str(hub_root),
                        "--admin-token",
                        "admin-secret",
                    ]
                )
                with self.assertRaises(ValueError) as public_url_error:
                    main(
                        [
                            "hub",
                            "init",
                            str(hub_root),
                            "--public-base-url",
                            "https://other.example.test",
                        ]
                    )
                with self.assertRaises(ValueError) as admin_token_error:
                    main(["hub", "init", str(hub_root), "--admin-token", "other-secret"])

            lines = stdout.getvalue().splitlines()
            token_id = next(line.split("\t", 1)[1] for line in lines if line.startswith("token_id\t"))
            raw_token = next(line.split("\t", 1)[1] for line in lines if line.startswith("raw_token\t"))
            store = HubStore(hub_root)

            with patch("sys.stdout", stdout):
                revoke_result = main(["hub", "token", "revoke", str(hub_root), token_id])
            admin_ok = store.verify_token("admin-secret", action="admin")
            provider_token_ok = store.verify_token(raw_token, action="publish", provider_id="alice")

        self.assertEqual(init_result, 0)
        self.assertEqual(create_result, 0)
        self.assertEqual(status_result, 0)
        self.assertEqual(second_init_result, 0)
        self.assertEqual(same_url_init_result, 0)
        self.assertEqual(same_admin_init_result, 0)
        self.assertEqual(revoke_result, 0)
        self.assertTrue(admin_ok)
        self.assertFalse(provider_token_ok)
        self.assertIn("initialized\tyes", stdout.getvalue())
        self.assertIn("public_base_url\thttps://hub.example.test", stdout.getvalue())
        self.assertIn("admin_token\tunchanged", stdout.getvalue())
        self.assertIn(
            "already initialized with public_base_url https://hub.example.test",
            str(public_url_error.exception),
        )
        self.assertIn("token rotation is not supported by hub init", str(admin_token_error.exception))

    def test_hub_token_list_prints_revocation_handles_without_raw_tokens(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            hub_root = Path(tempdir) / "hub"
            store = HubStore(hub_root)
            store.initialize(admin_token="admin-secret")
            provider_token = store.create_provider_token("alice", label="publish")

            with patch("sys.stdout", stdout):
                result = main(["hub", "token", "list", str(hub_root)])

        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn(provider_token.token_id, output)
        self.assertIn("publish", output)
        self.assertIn("alice", output)
        self.assertNotIn(provider_token.raw_token, output)
        self.assertNotIn("admin-secret", output)

    def test_hub_serve_runs_uvicorn_app(self):
        with tempfile.TemporaryDirectory() as tempdir:
            hub_root = Path(tempdir) / "hub"
            HubStore(hub_root).initialize(admin_token="admin-secret")

            with patch("rightmemory.cli.uvicorn.run") as run:
                result = main(["hub", "serve", str(hub_root), "--host", "0.0.0.0", "--port", "9876"])

        self.assertEqual(result, 0)
        self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")
        self.assertEqual(run.call_args.kwargs["port"], 9876)

    def test_hub_commands_default_to_rightmemory_hub_root(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            default_hub = Path(tempdir) / "rightmemory-hub"
            with patch("rightmemory.cli.DEFAULT_HUB_ROOT", default_hub):
                with patch("sys.stdout", stdout):
                    init_result = main(
                        [
                            "hub",
                            "init",
                            "--admin-token",
                            "admin-secret",
                            "--public-base-url",
                            "https://hub.example.test",
                        ]
                    )
                    create_result = main(
                        [
                            "hub",
                            "token",
                            "create",
                            "--provider",
                            "alice",
                            "--label",
                            "publish",
                        ]
                    )
                    status_result = main(["hub", "status"])
                    list_result = main(["hub", "token", "list"])

                lines = stdout.getvalue().splitlines()
                token_id = next(line.split("\t", 1)[1] for line in lines if line.startswith("token_id\t"))
                with patch("sys.stdout", stdout):
                    revoke_result = main(["hub", "token", "revoke", token_id])

            store = HubStore(default_hub)
            admin_ok = store.verify_token("admin-secret", action="admin")
            hub_db_exists = (default_hub / "hub.db").is_file()

        output = stdout.getvalue()
        self.assertEqual(init_result, 0)
        self.assertEqual(create_result, 0)
        self.assertEqual(status_result, 0)
        self.assertEqual(list_result, 0)
        self.assertEqual(revoke_result, 0)
        self.assertTrue(admin_ok)
        self.assertTrue(hub_db_exists)
        self.assertIn(f"hub_root\t{default_hub.resolve()}", output)
        self.assertIn("public_base_url\thttps://hub.example.test", output)
        self.assertIn("revoked\t", output)

    def test_hub_explicit_root_still_overrides_default(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            default_hub = Path(tempdir) / "rightmemory-hub"
            explicit_hub = Path(tempdir) / "explicit-hub"
            with patch("rightmemory.cli.DEFAULT_HUB_ROOT", default_hub):
                with patch("sys.stdout", stdout):
                    result = main(
                        [
                            "hub",
                            "init",
                            str(explicit_hub),
                            "--admin-token",
                            "admin-secret",
                        ]
                    )
            explicit_hub_db_exists = (explicit_hub / "hub.db").is_file()
            default_hub_db_exists = (default_hub / "hub.db").exists()

        self.assertEqual(result, 0)
        self.assertTrue(explicit_hub_db_exists)
        self.assertFalse(default_hub_db_exists)
        self.assertIn(f"hub_root\t{explicit_hub.resolve()}", stdout.getvalue())

    def test_hub_serve_uses_default_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            default_hub = Path(tempdir) / "rightmemory-hub"
            HubStore(default_hub).initialize(admin_token="admin-secret")

            with patch("rightmemory.cli.DEFAULT_HUB_ROOT", default_hub):
                with patch("rightmemory.cli.uvicorn.run") as run:
                    result = main(["hub", "serve"])

        self.assertEqual(result, 0)
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run.call_args.kwargs["port"], 8765)

    def test_profile_list_ignores_project_binding(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            profile_root = Path(tempdir) / "profile-root"
            project = Path(tempdir) / "project"
            project.mkdir()
            default_root.mkdir()
            (project / ".rightmemory-profile").write_text("missing\n", encoding="utf-8")
            (default_root / "profiles.toml").write_text(
                f"[profiles.alpha]\nroot = {json.dumps(str(profile_root))}\n",
                encoding="utf-8",
            )

            with (
                patch("rightmemory.cli.default_memory_root", return_value=default_root),
                patch("rightmemory.cli.Path.cwd", return_value=project),
                patch("sys.stdout", stdout),
            ):
                result = main(["profile", "list"])

        self.assertEqual(result, 0)
        self.assertIn(f"alpha\t{profile_root}", stdout.getvalue())

    def test_profile_create_calls_create_profile(self):
        stdout = io.StringIO()
        profile = type("Profile", (), {"name": "alpha", "root": Path("/profiles/alpha")})()

        with (
            patch("rightmemory.cli.default_memory_root", return_value=Path("/default")),
            patch("rightmemory.cli.create_profile", return_value=profile) as create_profile,
            patch("sys.stdout", stdout),
        ):
            result = main(["profile", "create", "alpha", "--root", "/profiles/alpha"])

        self.assertEqual(result, 0)
        create_profile.assert_called_once_with(Path("/default"), "alpha", root=Path("/profiles/alpha"))
        self.assertIn(f"alpha\t{Path('/profiles/alpha')}", stdout.getvalue())

    def test_profile_command_rejects_global_profile_flag(self):
        with self.assertRaises(ValueError) as caught:
            main(["--profile", "alpha", "profile", "list"])

        self.assertIn("--profile is for runtime commands", str(caught.exception))

    def test_top_level_help_does_not_resolve_project_binding(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            project = Path(tempdir) / "project"
            project.mkdir()
            (project / ".rightmemory-profile").write_text("missing\n", encoding="utf-8")

            with (
                patch("rightmemory.cli.Path.cwd", return_value=project),
                patch("rightmemory.cli.resolve_memory_root", side_effect=AssertionError("root should not resolve")),
                patch("sys.stdout", stdout),
            ):
                with self.assertRaises(SystemExit) as caught:
                    main(["--help"])

        self.assertEqual(caught.exception.code, 0)
        self.assertIn("RightMemory", stdout.getvalue())

    def test_prune_command_delegates_due_check_to_pruner_runtime(self):
        stdout = io.StringIO()
        pruner_config = type("PrunerConfig", (), {"memory_root": Path("/memory")})()

        with (
            patch("rightmemory.cli.load_pruner_config", return_value=pruner_config),
            patch("rightmemory.cli.load_config", return_value=object()),
            patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
            patch("sys.stdout", stdout),
        ):
            result = main(["prune"])

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().strip(), f"prune session pruner: {Path('/memory')}")

    def test_prune_command_uses_requested_session(self):
        stdout = io.StringIO()
        pruner_config = type("PrunerConfig", (), {"memory_root": Path("/memory")})()
        roles = []

        def fake_load_config(role, **kwargs):
            roles.append(role)
            return object()

        with (
            patch("rightmemory.cli.load_pruner_config", return_value=pruner_config),
            patch("rightmemory.cli.load_config", fake_load_config),
            patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
            patch("sys.stdout", stdout),
        ):
            result = main(["prune", "--session", "prune-1"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["pruner"])
        self.assertIn(f"prune session prune-1: {Path('/memory')}", stdout.getvalue())

    def test_prune_watch_help_does_not_load_config(self):
        stdout = io.StringIO()

        with (
            patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
            patch("rightmemory.cli.load_pruner_config", side_effect=AssertionError("pruner config should not load")),
            patch("sys.stdout", stdout),
        ):
            with self.assertRaises(SystemExit) as caught:
                main(["prune", "watch", "--help"])

        self.assertEqual(caught.exception.code, 0)
        self.assertIn("rightmemory prune watch", stdout.getvalue())

    def test_prune_watch_rejects_non_positive_interval(self):
        with (
            patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
            patch("rightmemory.cli.load_pruner_config", side_effect=AssertionError("pruner config should not load")),
        ):
            with self.assertRaises(ValueError):
                main(["prune", "watch", "--interval", "0"])

    def test_prune_watch_sleeps_until_interrupted(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            pruner_config = type("PrunerConfig", (), {"memory_root": memory_root})()
            runtime_config = type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_pruner_config", return_value=pruner_config),
                patch("rightmemory.cli.load_config", return_value=runtime_config),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["prune", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(60)
        self.assertIn("rightmemory prune check", stdout.getvalue())
        self.assertIn(f"prune session pruner-watch: {memory_root}", stdout.getvalue())
        self.assertIn("rightmemory pruner watch stopped", stderr.getvalue())

    def test_prune_watch_failure_logs_and_retries(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        class FailingRuntime(FakeRuntime):
            def run_prune_turn(self, session_id: str, pruner_config, *, operation_id=None) -> str:
                raise RuntimeError(f"boom for {session_id}")

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            pruner_config = type("PrunerConfig", (), {"memory_root": memory_root})()
            runtime_config = type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_pruner_config", return_value=pruner_config),
                patch("rightmemory.cli.load_config", return_value=runtime_config),
                patch("rightmemory.cli.RightMemoryRuntime", FailingRuntime),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["prune", "watch", "--interval", "120"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(60)
        self.assertIn("rightmemory prune check", stdout.getvalue())
        self.assertIn("rightmemory prune check failed: RuntimeError: boom for pruner-watch", stderr.getvalue())
        self.assertIn("rightmemory pruner watch stopped", stderr.getvalue())

    def test_prune_watch_stops_after_consecutive_failures(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        operation_ids = []

        class FailingRuntime(FakeRuntime):
            def run_prune_turn(self, session_id: str, pruner_config, *, operation_id=None) -> str:
                operation_ids.append(operation_id)
                raise RuntimeError(f"boom for {session_id}")

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            pruner_config = type("PrunerConfig", (), {"memory_root": memory_root})()
            runtime_config = type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.DEFAULT_WATCH_MAX_CONSECUTIVE_FAILURES", 2),
                patch("rightmemory.cli.load_pruner_config", return_value=pruner_config),
                patch("rightmemory.cli.load_config", return_value=runtime_config),
                patch("rightmemory.cli.RightMemoryRuntime", FailingRuntime),
                patch("rightmemory.cli._sleep_with_refresh_check", return_value=True) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["prune", "watch", "--interval", "120"])

        self.assertEqual(result, 1)
        sleep.assert_called_once()
        self.assertEqual(len(set(operation_ids)), 1)
        self.assertEqual(stdout.getvalue().count("rightmemory prune check"), 2)
        self.assertIn("rightmemory pruner watch stopping after 2 consecutive failed cycles", stderr.getvalue())
        self.assertIn("rightmemory pruner watch stopped", stderr.getvalue())

    def test_main_rejects_old_curator_role(self):
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["curator", "--session", "agent-1", "hello"])

        self.assertEqual(caught.exception.code, 2)

    def test_review_scan_once_runs_scanner(self):
        roles = []
        scan_flags = []
        stdout = io.StringIO()

        class FakeScanner:
            def __init__(self, config, run_reviewer):
                self.config = config
                self.run_reviewer = run_reviewer

            def scan_once(self, *, require_full_batch=False):
                scan_flags.append(require_full_batch)
                return FakeReviewResult("reviewed: 1", reviewed=1)

        def fake_load_config(role, **kwargs):
            roles.append(role)
            return type("Config", (), {"memory_root": memory_root})()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_review_config", return_value=object()),
                patch("rightmemory.cli.load_dreamer_watch_config", return_value=_dreamer_watch_config()),
                patch("rightmemory.cli.load_insight_watch_config", return_value=_insight_watch_config()),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("rightmemory.cli.ReviewScanner", FakeScanner),
                patch("sys.stdout", stdout),
            ):
                result = main(["review", "scan", "--once"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["reviewer"])
        self.assertEqual(scan_flags, [False])
        self.assertEqual(stdout.getvalue().strip(), "reviewed: 1")

    def test_update_review_scan_once_uses_independent_checker(self):
        stdout = io.StringIO()
        scan_result = UpdateReviewProcessResult(processed=1, resolved=1)

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli._run_update_review_scan", return_value=scan_result) as scan,
                patch("sys.stdout", stdout),
            ):
                result = main(["update-review", "scan", "--once"])

        self.assertEqual(result, 0)
        scan.assert_called_once_with(root)
        self.assertIn("processed: 1", stdout.getvalue())
        self.assertIn("resolved: 1", stdout.getvalue())
        self.assertIn("malformed: 0", stdout.getvalue())

    def test_update_review_scan_processes_the_tracked_inbox(self):
        expected = UpdateReviewProcessResult(blank=1)
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch(
                    "rightmemory.cli.load_sync_config",
                    return_value=SimpleNamespace(enabled=True),
                ),
                patch("rightmemory.cli.UpdateReviewStore") as store_class,
            ):
                store_class.return_value.process_ready.return_value = expected
                result = _run_update_review_scan(root)

        self.assertEqual(result, expected)
        store_class.return_value.process_ready.assert_called_once()

    def test_ready_review_is_queued_and_restored_to_its_tracked_document(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=root,
                check=True,
            )
            (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            (root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "MEMORY.md", "PURSUITS.md"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            (root / "MEMORY.md").write_text("# Memory\n\nRemember this.\n", encoding="utf-8")
            operation_id = "update-synchronized-review"
            store = UpdateReviewStore(root)
            record = store.create_review(
                origin_operation_id=operation_id,
                base_commit=base,
                write_surface="Memory",
                summary="Remembered one fact.",
                diff="- old\n+ new",
            )
            subprocess.run(["git", "add", "MEMORY.md", "update_reviews"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-qm",
                    f"memory: update\n\nRightMemory-Operation: {operation_id}",
                ],
                cwd=root,
                check=True,
            )
            path = store.review_path(record.review_id)
            draft = path.read_text(encoding="utf-8")
            start = draft.index(COMMENT_START) + len(COMMENT_START)
            end = draft.index(COMMENT_END, start)
            draft = draft[:start] + "\n\nRemove the snapshot detail.\n\n" + draft[end:]
            draft = draft.replace(f"- [ ] {READY_LABEL}", f"- [x] {READY_LABEL}", 1)
            path.write_text(draft, encoding="utf-8")

            with (
                patch(
                    "rightmemory.cli.load_sync_config",
                    return_value=SimpleNamespace(enabled=True),
                ),
                patch("rightmemory.cli.AsyncUpdateStore.wake_worker") as wake,
            ):
                result = _run_update_review_scan(root)

            queued = UpdateQueueStore(root).outbox_candidates()
            parsed = parse_review_markdown(path.read_text(encoding="utf-8"))
            self.assertEqual(result.submitted, 1)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].kind, "review")
            self.assertEqual(queued[0].message, "Remove the snapshot detail.")
            self.assertFalse(parsed.ready)
            self.assertEqual(parsed.comment.strip(), "")
            wake.assert_called_once()

    def test_sync_disabled_clarification_can_be_answered_on_the_next_scan(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=root,
                check=True,
            )
            (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            (root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "MEMORY.md", "PURSUITS.md"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.strip()
            (root / "MEMORY.md").write_text(
                "# Memory\n\nRemember this.\n",
                encoding="utf-8",
            )
            operation_id = "update-local-review"
            store = UpdateReviewStore(root)
            record = store.create_review(
                origin_operation_id=operation_id,
                base_commit=base,
                write_surface="Memory",
                summary="Remembered one fact.",
                diff="- old\n+ new",
            )
            subprocess.run(["git", "add", "MEMORY.md", "update_reviews"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-qm",
                    f"memory: update\n\nRightMemory-Operation: {operation_id}",
                ],
                cwd=root,
                check=True,
            )
            path = store.review_path(record.review_id)

            def submit(comment: str) -> None:
                draft = path.read_text(encoding="utf-8")
                start = draft.index(COMMENT_START) + len(COMMENT_START)
                end = draft.index(COMMENT_END, start)
                draft = draft[:start] + f"\n\n{comment}\n\n" + draft[end:]
                draft = draft.replace(f"- [ ] {READY_LABEL}", f"- [x] {READY_LABEL}", 1)
                path.write_text(draft, encoding="utf-8")

            responses = iter(
                (
                    '{"status":"needs_input","message":"Which path should remain?"}',
                    '{"status":"no_change","message":"Already correct."}',
                )
            )
            messages = []

            class LocalCorrectionRuntime:
                def __init__(self, _config):
                    self.last_write_result = None

                def run_session_turn(self, _session_id, message, *, operation_id):
                    output = next(responses)
                    messages.append(message)
                    self.last_write_result = IsolatedWriteResult(
                        output=output,
                        commits_landed=0,
                        start_commit=base,
                        landed_commit=base,
                        changed_paths=(),
                        operation_id=operation_id,
                    )
                    return output

                def cleanup(self):
                    pass

            submit("Keep the durable path.")
            with (
                patch(
                    "rightmemory.cli.load_sync_config",
                    return_value=SimpleNamespace(enabled=False),
                ),
                patch("rightmemory.cli.load_update_corrector_config", return_value=object()),
                patch("rightmemory.cli.RightMemoryRuntime", LocalCorrectionRuntime),
                patch("rightmemory.cli._require_completed_correction_operation"),
            ):
                first = _run_update_review_scan(root)
                canonical = parse_review_markdown(path.read_text(encoding="utf-8"))
                self.assertEqual(canonical.comment.strip(), "")
                self.assertIn("Which path should remain?", canonical.question)

                submit("Use `/stable/path`.")
                second = _run_update_review_scan(root)

            self.assertEqual(first.needs_input, 1)
            self.assertEqual(second.resolved, 1)
            self.assertFalse(path.exists())
            self.assertIn("Which path should remain?", messages[1])

    def test_update_review_correction_uses_internal_role_verified_context_and_landed_commit(self):
        calls = []

        class CorrectionRuntime:
            def __init__(self, config):
                calls.append(("init", config))
                self.last_write_result = IsolatedWriteResult(
                    output='{"status":"applied","message":"corrected"}',
                    commits_landed=1,
                    start_commit="base",
                    landed_commit="fix-commit",
                    changed_paths=("MEMORY.md",),
                    operation_id="correction-operation",
                )

            def run_session_turn(self, session_id, message, *, operation_id):
                calls.append(("run", session_id, operation_id, message))
                return '{"status":"applied","message":"corrected"}'

            def cleanup(self):
                calls.append(("cleanup",))

        request = UpdateReviewRequest(
            review_id="review-1",
            document_path=Path("review.md"),
            origin_operation_id="original-operation",
            base_commit="base",
            write_surface="Memory + Pursuit",
            document="# RightMemory Update Review\n\nUNTRUSTED EMBEDDED DIFF\n",
            comment="Human comment here.",
            comment_sha256="a" * 64,
            operation_id="correction-operation",
        )
        config = object()
        operation = _verified_review_fixture()
        with (
            patch("rightmemory.cli.load_update_corrector_config", return_value=config),
            patch("rightmemory.cli._verified_update_review", return_value=operation),
            patch("rightmemory.cli._stored_correction_message", return_value=None),
            patch("rightmemory.cli._require_completed_correction_operation"),
            patch("rightmemory.cli.RightMemoryRuntime", CorrectionRuntime),
        ):
            outcome = _run_update_review_correction(Path("/memory"), request)

        self.assertEqual(outcome.status, "resolved")
        self.assertEqual(outcome.correction_commit, "fix-commit")
        self.assertEqual(calls[0], ("init", config))
        self.assertEqual(calls[1][1:3], ("correction-operation", "correction-operation"))
        self.assertIn("Human comment here.", calls[1][3])
        self.assertIn('"diff": "- before\\n+ after"', calls[1][3])
        self.assertIn("trusted update summary", calls[1][3])
        self.assertNotIn("UNTRUSTED EMBEDDED DIFF", calls[1][3])

    def test_update_review_correction_maps_needs_input_without_commit(self):
        class CorrectionRuntime:
            def __init__(self, config):
                self.config = config
                self.last_write_result = IsolatedWriteResult(
                    output='{"status":"needs_input","message":"Which preference should win?"}',
                    commits_landed=0,
                    landed_commit="update",
                    operation_id="correction-operation",
                )

            def run_session_turn(self, session_id, message, *, operation_id):
                return '{"status":"needs_input","message":"Which preference should win?"}'

            def cleanup(self):
                pass

        request = UpdateReviewRequest(
            review_id="review-1",
            document_path=Path("review.md"),
            origin_operation_id="original-operation",
            base_commit="base",
            write_surface="Memory",
            document="review",
            comment="ambiguous",
            comment_sha256="b" * 64,
            operation_id="correction-operation",
        )
        operation = _verified_review_fixture()
        with (
            patch("rightmemory.cli.load_update_corrector_config", return_value=object()),
            patch("rightmemory.cli._verified_update_review", return_value=operation),
            patch("rightmemory.cli._stored_correction_message", return_value=None),
            patch("rightmemory.cli._require_completed_correction_operation"),
            patch("rightmemory.cli.RightMemoryRuntime", CorrectionRuntime),
        ):
            outcome = _run_update_review_correction(Path("/memory"), request)

        self.assertEqual(outcome.status, "needs_input")
        self.assertEqual(outcome.message, "Which preference should win?")
        self.assertIsNone(outcome.correction_commit)

    def test_update_review_correction_replays_its_first_journal_message(self):
        calls = []

        class CorrectionRuntime:
            def __init__(self, config):
                self.last_write_result = IsolatedWriteResult(
                    output='{"status":"no_change","message":"Same revision."}',
                    commits_landed=0,
                    landed_commit="update",
                    operation_id="correction-operation",
                )

            def run_session_turn(self, session_id, message, *, operation_id):
                calls.append(message)
                return '{"status":"no_change","message":"Same revision."}'

            def cleanup(self):
                pass

        request = UpdateReviewRequest(
            review_id="review-1",
            document_path=Path("review.md"),
            origin_operation_id="original-operation",
            base_commit="base",
            write_surface="Memory",
            document="review",
            comment="Use the earlier choice.",
            comment_sha256="e" * 64,
            operation_id="correction-operation",
            previous_question="A different later question?",
        )
        operation = _verified_review_fixture()
        with (
            patch("rightmemory.cli.load_update_corrector_config", return_value=object()),
            patch("rightmemory.cli._verified_update_review", return_value=operation),
            patch(
                "rightmemory.cli._stored_correction_message",
                return_value="the first durable correction message",
            ),
            patch("rightmemory.cli._require_completed_correction_operation"),
            patch("rightmemory.cli.RightMemoryRuntime", CorrectionRuntime),
        ):
            outcome = _run_update_review_correction(Path("/memory"), request)

        self.assertEqual(outcome.status, "resolved")
        self.assertEqual(calls, ["the first durable correction message"])

    def test_update_review_correction_waits_for_all_durable_effects(self):
        class CorrectionRuntime:
            def __init__(self, config):
                self.last_write_result = IsolatedWriteResult(
                    output='{"status":"no_change","message":"Already correct."}',
                    commits_landed=0,
                    landed_commit="update",
                    operation_id="correction-operation",
                )

            def run_session_turn(self, session_id, message, *, operation_id):
                return '{"status":"no_change","message":"Already correct."}'

            def cleanup(self):
                pass

        request = UpdateReviewRequest(
            review_id="review-1",
            document_path=Path("review.md"),
            origin_operation_id="original-operation",
            base_commit="base",
            write_surface="Memory",
            document="review",
            comment="Check the correction.",
            comment_sha256="d" * 64,
            operation_id="correction-operation",
        )
        operation = _verified_review_fixture()
        with (
            patch("rightmemory.cli.load_update_corrector_config", return_value=object()),
            patch("rightmemory.cli._verified_update_review", return_value=operation),
            patch("rightmemory.cli._stored_correction_message", return_value=None),
            patch(
                "rightmemory.cli._require_completed_correction_operation",
                side_effect=RuntimeError(
                    "update correction has pending operation effects: file-view-publish"
                ),
            ),
            patch("rightmemory.cli.RightMemoryRuntime", CorrectionRuntime),
        ):
            with self.assertRaisesRegex(RuntimeError, "pending operation effects: file-view-publish"):
                _run_update_review_correction(Path("/memory"), request)

    def test_completed_correction_gate_reads_the_durable_receipt(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = SemanticOperationStore(root)
            operation_id = "correction-operation"
            store.begin(
                operation_id,
                {
                    "kind": "semantic-turn",
                    "role": "update-corrector",
                    "session_id": operation_id,
                    "message": "the first durable correction message",
                },
            )
            store.prepare_outcome(
                operation_id,
                output='{"status":"no_change","message":"Already correct."}',
                start_commit="update",
                changed_paths=(),
                effects=(OperationEffect("file-view-publish"),),
            )
            store.complete_no_change(operation_id, "update")

            with self.assertRaisesRegex(RuntimeError, "pending operation effects"):
                _require_completed_correction_operation(root, operation_id)

            store.mark_effect(operation_id, "file-view-publish", "done")
            _require_completed_correction_operation(root, operation_id)
            self.assertEqual(
                _stored_correction_message(root, operation_id),
                "the first durable correction message",
            )

    def test_update_review_correction_rejects_applied_result_without_matching_commit(self):
        class IncorrectRuntime:
            def __init__(self, config):
                self.last_write_result = IsolatedWriteResult(
                    output='{"status":"applied","message":"done"}',
                    commits_landed=1,
                    landed_commit="unexpected",
                    changed_paths=("MEMORY.md",),
                    operation_id="different-operation",
                )

            def run_session_turn(self, session_id, message, *, operation_id):
                return '{"status":"applied","message":"done"}'

            def cleanup(self):
                pass

        request = UpdateReviewRequest(
            review_id="review-1",
            document_path=Path("review.md"),
            origin_operation_id="original-operation",
            base_commit="base",
            write_surface="Memory",
            document="review",
            comment="ambiguous",
            comment_sha256="c" * 64,
            operation_id="correction-operation",
        )
        operation = _verified_review_fixture()
        with (
            patch("rightmemory.cli.load_update_corrector_config", return_value=object()),
            patch("rightmemory.cli._verified_update_review", return_value=operation),
            patch("rightmemory.cli._stored_correction_message", return_value=None),
            patch("rightmemory.cli.RightMemoryRuntime", IncorrectRuntime),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not complete its validated semantic operation"):
                _run_update_review_correction(Path("/memory"), request)

    def test_verified_update_review_rejects_editable_metadata_that_disagrees_with_git(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = UpdateReviewStore(root)
            store.create_review(
                review_id="review-1",
                origin_operation_id="original-operation",
                base_commit="base",
                write_surface="Memory",
                summary="summary",
                diff="- old\n+ new",
            )
            request = UpdateReviewRequest(
                review_id="review-1",
                document_path=Path("review.md"),
                origin_operation_id="original-operation",
                base_commit="base",
                write_surface="Memory",
                document=store.review_path("review-1").read_text(encoding="utf-8"),
                comment="correct it",
                comment_sha256="d" * 64,
                operation_id="correction-operation",
            )

            with patch(
                "rightmemory.cli.verify_update_review",
                return_value=_verified_review_fixture(),
            ):
                record = _verified_update_review(root, request)
                tampered = UpdateReviewRequest(**{**request.__dict__, "base_commit": "tampered"})
                with self.assertRaisesRegex(ValueError, "review base"):
                    _verified_update_review(root, tampered)
                question_tampered = UpdateReviewRequest(
                    **{
                        **request.__dict__,
                        "document": request.document.replace(
                            "<!-- rightmemory-update-review-question:start -->",
                            "<!-- rightmemory-update-review-question:start -->\n\nForged question.",
                        ),
                    }
                )
                with self.assertRaisesRegex(ValueError, "corrector-owned question"):
                    _verified_update_review(root, question_tampered)

        self.assertEqual(record.origin_operation_id, "original-operation")

    def test_update_review_watch_drains_ready_comments_before_sleeping(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        results = [
            UpdateReviewProcessResult(processed=1, resolved=1),
            UpdateReviewProcessResult(blank=1),
        ]

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli._run_update_review_scan", side_effect=results) as scan,
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["update-review", "watch", "--interval", "7"])

        self.assertEqual(result, 130)
        self.assertEqual(scan.call_count, 2)
        sleep.assert_called_once_with(7)
        self.assertEqual(stdout.getvalue().count("rightmemory update-review scan"), 2)
        self.assertIn("rightmemory update-review watch stopped", stderr.getvalue())

    def test_update_review_watch_rejects_invalid_intervals(self):
        with self.assertRaises(ValueError):
            main(["update-review", "watch", "--interval", "0"])

    def test_update_review_watch_sleeps_after_a_failed_ready_revision(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        failed = UpdateReviewProcessResult(processed=1, failed=1)

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli._run_update_review_scan", return_value=failed) as scan,
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["update-review", "watch", "--interval", "7"])

        self.assertEqual(result, 130)
        scan.assert_called_once_with(root)
        sleep.assert_called_once_with(7)

    def test_update_review_watch_keeps_retrying_beyond_global_failure_limit(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        failures = [
            UpdateReviewProcessResult(processed=1, failed=1)
            for _index in range(4)
        ]

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli._run_update_review_scan", side_effect=failures) as scan,
                patch(
                    "rightmemory.cli._sleep_with_refresh_check",
                    side_effect=[True, True, True, KeyboardInterrupt],
                ),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["update-review", "watch", "--interval", "7"])

        self.assertEqual(result, 130)
        self.assertEqual(scan.call_count, 4)
        self.assertEqual([call.args for call in scan.call_args_list], [(root,)] * 4)

    def test_review_scan_does_not_add_pressure_before_unified_update(self):
        stdout = io.StringIO()
        scanner_calls = []

        class FakeScanner:
            def __init__(self, config, run_reviewer):
                scanner_calls.append(True)

            def scan_once(self, *, require_full_batch=False):
                return FakeReviewResult("reviewed: 2", reviewed=2)

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            config = type("Config", (), {"memory_root": memory_root})()
            with (
                patch("rightmemory.cli.load_config", return_value=config),
                patch("rightmemory.cli.load_review_config", return_value=object()),
                patch("rightmemory.cli.load_dreamer_watch_config", side_effect=AssertionError("not used")),
                patch("rightmemory.cli.load_insight_watch_config", side_effect=AssertionError("not used")),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("rightmemory.cli.ReviewScanner", FakeScanner),
                patch("sys.stdout", stdout),
            ):
                result = main(["review", "scan", "--once"])

            trigger = DreamerTriggerStore(memory_root).read()
            insight_trigger = InsightTriggerStore(memory_root).read()

        self.assertEqual(result, 0)
        self.assertEqual(scanner_calls, [True])
        self.assertEqual(trigger.points, 0.0)
        self.assertEqual(insight_trigger.points, 0.0)
        self.assertEqual(stdout.getvalue().strip(), "reviewed: 2")

    def test_doctor_agent_cli_prints_report_and_returns_success(self):
        stdout = io.StringIO()
        checks = [DoctorCheck("role configs", True, "ok")]

        with patch("rightmemory.cli.run_agent_cli_doctor", return_value=checks), patch("sys.stdout", stdout):
            result = main(["doctor", "agent-cli"])

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().strip(), "[ok] role configs - ok")

    def test_doctor_agent_cli_returns_failure_when_a_check_fails(self):
        stdout = io.StringIO()
        checks = [DoctorCheck("role configs", False, "bad")]

        with patch("rightmemory.cli.run_agent_cli_doctor", return_value=checks), patch("sys.stdout", stdout):
            result = main(["doctor", "agent-cli"])

        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue().strip(), "[fail] role configs - bad")

    def test_review_watch_runs_scans_until_interrupted(self):
        roles = []
        scan_flags = []
        stdout = io.StringIO()
        stderr = io.StringIO()
        results = [
            FakeReviewResult("reviewed: 1", reviewed=1),
            FakeReviewResult("reviewed: 0", reviewed=0),
        ]

        class FakeScanner:
            def __init__(self, config, run_reviewer):
                self.config = config
                self.run_reviewer = run_reviewer

            def scan_once(self, *, require_full_batch=False):
                scan_flags.append(require_full_batch)
                return results.pop(0)

        def fake_load_config(role, **kwargs):
            roles.append(role)
            return type("Config", (), {"memory_root": memory_root})()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_review_config", return_value=object()),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("rightmemory.cli.ReviewScanner", FakeScanner),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["review", "watch", "--interval", "5"])
            trigger = DreamerTriggerStore(memory_root).read()

        self.assertEqual(result, 130)
        self.assertEqual(roles, ["reviewer", "reviewer"])
        self.assertEqual(scan_flags, [True, True])
        self.assertEqual(trigger.points, 0.0)
        self.assertIn("rightmemory review scan", stdout.getvalue())
        self.assertIn("reviewed: 1", stdout.getvalue())
        self.assertIn("reviewed: 0", stdout.getvalue())
        self.assertIn("rightmemory review watch stopped", stderr.getvalue())

    def test_review_watch_failed_scan_uses_retry_sleep(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        class FakeScanner:
            def __init__(self, config, run_reviewer):
                self.config = config
                self.run_reviewer = run_reviewer

            def scan_once(self, *, require_full_batch=False):
                return FakeReviewResult("failed: 1", failed=1)

        with tempfile.TemporaryDirectory() as tempdir:
            config = type("Config", (), {"memory_root": Path(tempdir)})()
            with (
                patch("rightmemory.cli.load_config", return_value=config),
                patch("rightmemory.cli.load_review_config", return_value=object()),
                patch("rightmemory.cli.load_dreamer_watch_config", return_value=_dreamer_watch_config()),
                patch("rightmemory.cli.load_insight_watch_config", return_value=_insight_watch_config()),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("rightmemory.cli.ReviewScanner", FakeScanner),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["review", "watch", "--interval", "120"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(60)
        self.assertIn("failed: 1", stdout.getvalue())

    def test_review_watch_stops_after_consecutive_failed_scans(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        class FakeScanner:
            def __init__(self, config, run_reviewer):
                self.config = config
                self.run_reviewer = run_reviewer

            def scan_once(self, *, require_full_batch=False):
                return FakeReviewResult("failed: 1", failed=1)

        with tempfile.TemporaryDirectory() as tempdir:
            config = type("Config", (), {"memory_root": Path(tempdir)})()
            with (
                patch("rightmemory.cli.DEFAULT_WATCH_MAX_CONSECUTIVE_FAILURES", 2),
                patch("rightmemory.cli.load_config", return_value=config),
                patch("rightmemory.cli.load_review_config", return_value=object()),
                patch("rightmemory.cli.load_dreamer_watch_config", return_value=_dreamer_watch_config()),
                patch("rightmemory.cli.load_insight_watch_config", return_value=_insight_watch_config()),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("rightmemory.cli.ReviewScanner", FakeScanner),
                patch("rightmemory.cli._sleep_with_refresh_check", return_value=True) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["review", "watch", "--interval", "120"])

        self.assertEqual(result, 1)
        sleep.assert_called_once()
        self.assertEqual(stdout.getvalue().count("failed: 1"), 2)
        self.assertIn("rightmemory review watch stopping after 2 consecutive failed cycles", stderr.getvalue())
        self.assertIn("rightmemory review watch stopped", stderr.getvalue())

    def test_review_watch_default_interval_is_two_hours(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        class FakeScanner:
            def __init__(self, config, run_reviewer):
                self.config = config
                self.run_reviewer = run_reviewer

            def scan_once(self, *, require_full_batch=False):
                return FakeReviewResult("reviewed: 0", reviewed=0)

        with tempfile.TemporaryDirectory() as tempdir:
            config = type("Config", (), {"memory_root": Path(tempdir)})()
            with (
                patch("rightmemory.cli.load_config", return_value=config),
                patch("rightmemory.cli.load_review_config", return_value=object()),
                patch("rightmemory.cli.load_dreamer_watch_config", return_value=_dreamer_watch_config()),
                patch("rightmemory.cli.load_insight_watch_config", return_value=_insight_watch_config()),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("rightmemory.cli.ReviewScanner", FakeScanner),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["review", "watch"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(7200)

    def test_review_watch_rejects_non_positive_interval(self):
        with patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")):
            with self.assertRaises(ValueError):
                main(["review", "watch", "--interval", "0"])

    def test_agent_cli_cleanup_once_reports_bounded_counts(self):
        stdout = io.StringIO()

        class FakeResult:
            def format(self):
                return "deleted: 2\npending: 1\nskipped: 3\nmalformed: 0"

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.AgentCliThreadCleanup") as cleanup,
                patch("sys.stdout", stdout),
            ):
                cleanup.return_value.run.return_value = FakeResult()
                result = main(["agent-cli", "cleanup", "--once"])

        self.assertEqual(result, 0)
        cleanup.assert_called_once_with(root)
        self.assertEqual(
            stdout.getvalue().strip(),
            "deleted: 2\npending: 1\nskipped: 3\nmalformed: 0",
        )

    def test_watch_start_starts_both_reviews_dreamer_pruner_and_insight_managed_processes(self):
        stdout = io.StringIO()

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            roles = []

            def fake_load_config(role, **kwargs):
                roles.append(role)
                return type("Config", (), {"memory_root": memory_root})()

            def fake_load_sync_config(**kwargs):
                return type("SyncConfig", (), {"memory_root": memory_root, "enabled": False})()

            with (
                patch("rightmemory.cli.default_memory_root", return_value=memory_root),
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.IsolatedWriteSupervisor.cleanup_stale", return_value=None),
                patch(
                    "rightmemory.watch.subprocess.Popen",
                    side_effect=[
                        FakeProcess(101),
                        FakeProcess(102),
                        FakeProcess(103),
                        FakeProcess(104),
                        FakeProcess(105),
                        FakeProcess(106),
                    ],
                ) as popen,
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "start"])

            review_pid = (memory_root / ".runtime" / "watch" / "review.pid").read_text(encoding="utf-8")
            update_review_pid = (memory_root / ".runtime" / "watch" / "update-review.pid").read_text(
                encoding="utf-8"
            )
            dreamer_pid = (memory_root / ".runtime" / "watch" / "dreamer.pid").read_text(encoding="utf-8")
            pruner_pid = (memory_root / ".runtime" / "watch" / "pruner.pid").read_text(encoding="utf-8")
            insight_pid = (memory_root / ".runtime" / "watch" / "insight.pid").read_text(encoding="utf-8")
            cleanup_pid = (memory_root / ".runtime" / "watch" / "agent-cli-cleanup.pid").read_text(
                encoding="utf-8"
            )

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["reviewer", "update", "dreamer", "pruner", "insight"])
        self.assertEqual(popen.call_count, 6)
        self.assertEqual(review_pid, "101\n")
        self.assertEqual(update_review_pid, "102\n")
        self.assertEqual(dreamer_pid, "103\n")
        self.assertEqual(pruner_pid, "104\n")
        self.assertEqual(insight_pid, "105\n")
        self.assertEqual(cleanup_pid, "106\n")
        self.assertIn("review: running pid 101", stdout.getvalue())
        self.assertIn("update-review: running pid 102", stdout.getvalue())
        self.assertIn("dreamer: running pid 103", stdout.getvalue())
        self.assertIn("pruner: running pid 104", stdout.getvalue())
        self.assertIn("insight: running pid 105", stdout.getvalue())
        self.assertIn("sync: disabled", stdout.getvalue())
        self.assertIn("agent-cli-cleanup: running pid 106", stdout.getvalue())

    def test_watch_start_starts_sync_when_enabled(self):
        stdout = io.StringIO()

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            def fake_load_sync_config(**kwargs):
                return type("SyncConfig", (), {"memory_root": memory_root, "enabled": True})()

            with (
                patch("rightmemory.cli.default_memory_root", return_value=memory_root),
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.IsolatedWriteSupervisor.cleanup_stale", return_value=None),
                patch(
                    "rightmemory.watch.subprocess.Popen",
                    side_effect=[
                        FakeProcess(101),
                        FakeProcess(102),
                        FakeProcess(103),
                        FakeProcess(104),
                        FakeProcess(105),
                        FakeProcess(106),
                        FakeProcess(107),
                    ],
                ) as popen,
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "start"])

            sync_pid = (memory_root / ".runtime" / "watch" / "sync.pid").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(popen.call_count, 7)
        self.assertEqual(sync_pid, "106\n")
        self.assertIn("sync: running pid 106", stdout.getvalue())

    def test_watch_start_skips_sync_when_disabled(self):
        stdout = io.StringIO()

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            def fake_load_sync_config(**kwargs):
                return type("SyncConfig", (), {"memory_root": memory_root, "enabled": False})()

            with (
                patch("rightmemory.cli.default_memory_root", return_value=memory_root),
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.IsolatedWriteSupervisor.cleanup_stale", return_value=None),
                patch(
                    "rightmemory.watch.subprocess.Popen",
                    side_effect=[
                        FakeProcess(101),
                        FakeProcess(102),
                        FakeProcess(103),
                        FakeProcess(104),
                        FakeProcess(105),
                        FakeProcess(106),
                    ],
                ) as popen,
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "start"])

        self.assertEqual(result, 0)
        self.assertEqual(popen.call_count, 6)
        self.assertIn("sync: disabled", stdout.getvalue())

    def test_watch_start_passes_selected_profile_root_to_subprocess_env(self):
        stdout = io.StringIO()
        events = []

        class FakeProcess:
            pid = 501

        def fake_popen(command, **kwargs):
            events.append((command, kwargs["env"]["RIGHTMEMORY_ROOT"], kwargs["cwd"]))
            return FakeProcess()

        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            profile_root = Path(tempdir) / "profile-root"
            default_root.mkdir()
            profile_root.mkdir()
            (default_root / "profiles.toml").write_text(
                f"[profiles.alpha]\nroot = {json.dumps(str(profile_root))}\n",
                encoding="utf-8",
            )

            def fake_load_config(role, **kwargs):
                self.assertEqual(kwargs.get("memory_root"), profile_root)
                return type("Config", (), {"memory_root": profile_root})()

            def fake_load_sync_config(**kwargs):
                self.assertEqual(kwargs.get("memory_root"), profile_root)
                return type("SyncConfig", (), {"memory_root": profile_root, "enabled": False})()

            with (
                patch("rightmemory.cli.default_memory_root", return_value=default_root),
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.IsolatedWriteSupervisor.cleanup_stale", return_value=None),
                patch("rightmemory.watch.subprocess.Popen", side_effect=fake_popen),
                patch("sys.stdout", stdout),
            ):
                result = main(["--profile", "alpha", "watch", "start", "review"])

        self.assertEqual(result, 0)
        self.assertEqual(events[0][1], str(profile_root))
        self.assertEqual(events[0][2], str(profile_root))
        self.assertIn("review: running pid 501", stdout.getvalue())

    def test_watch_start_reports_failure_after_attempting_later_targets(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            roles = []

            def fake_load_config(role, **kwargs):
                roles.append(role)
                if role == "reviewer":
                    raise RuntimeError("review unavailable")
                return type("Config", (), {"memory_root": memory_root})()

            def fake_load_sync_config(**kwargs):
                return type("SyncConfig", (), {"memory_root": memory_root, "enabled": True})()

            with (
                patch("rightmemory.cli.default_memory_root", return_value=memory_root),
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.IsolatedWriteSupervisor.cleanup_stale", return_value=None),
                patch(
                    "rightmemory.watch.subprocess.Popen",
                    side_effect=[
                        FakeProcess(201),
                        FakeProcess(202),
                        FakeProcess(203),
                        FakeProcess(204),
                        FakeProcess(205),
                        FakeProcess(206),
                    ],
                ) as popen,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["watch", "start"])

            dreamer_pid = (memory_root / ".runtime" / "watch" / "dreamer.pid").read_text(encoding="utf-8")
            pruner_pid = (memory_root / ".runtime" / "watch" / "pruner.pid").read_text(encoding="utf-8")
            insight_pid = (memory_root / ".runtime" / "watch" / "insight.pid").read_text(encoding="utf-8")
            sync_pid = (memory_root / ".runtime" / "watch" / "sync.pid").read_text(encoding="utf-8")

        self.assertEqual(result, 1)
        self.assertEqual(roles, ["reviewer", "update", "dreamer", "pruner", "insight"])
        self.assertEqual(popen.call_count, 6)
        self.assertEqual(dreamer_pid, "202\n")
        self.assertEqual(pruner_pid, "203\n")
        self.assertEqual(insight_pid, "204\n")
        self.assertEqual(sync_pid, "205\n")
        self.assertIn("review: error: RuntimeError: review unavailable", stderr.getvalue())
        self.assertIn("update-review: running pid 201", stdout.getvalue())
        self.assertIn("dreamer: running pid 202", stdout.getvalue())
        self.assertIn("pruner: running pid 203", stdout.getvalue())
        self.assertIn("insight: running pid 204", stdout.getvalue())
        self.assertIn("sync: running pid 205", stdout.getvalue())

    def test_watch_start_cleans_isolated_worktrees_for_write_targets_not_sync(self):
        stdout = io.StringIO()
        events = []

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

        class FakeSupervisor:
            def __init__(self, memory_root, role):
                self.memory_root = memory_root
                self.role = role

            def cleanup_stale(self):
                events.append(("cleanup", self.role))

        def fake_popen(command, **_kwargs):
            events.append(("start", command[-2]))
            return FakeProcess(300 + len(events))

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(_role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            def fake_load_sync_config(**kwargs):
                return type("SyncConfig", (), {"memory_root": memory_root, "enabled": True})()

            with (
                patch("rightmemory.cli.default_memory_root", return_value=memory_root),
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.IsolatedWriteSupervisor", FakeSupervisor),
                patch("rightmemory.watch.subprocess.Popen", side_effect=fake_popen),
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "start"])

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            [
                ("cleanup", "reviewer"),
                ("start", "review"),
                ("start", "update-review"),
                ("cleanup", "dreamer"),
                ("start", "dreamer"),
                ("cleanup", "pruner"),
                ("start", "prune"),
                ("cleanup", "insight"),
                ("start", "insight"),
                ("start", "sync"),
                ("start", "cleanup"),
            ],
        )

    def test_sync_is_a_managed_watch_target(self):
        self.assertIn("sync", MANAGED_WATCH_TARGETS)
        self.assertEqual(WATCH_COMMANDS["sync"], ("sync", "watch"))

    def test_agent_cli_cleanup_is_a_managed_watch_target(self):
        self.assertIn("agent-cli-cleanup", MANAGED_WATCH_TARGETS)
        self.assertEqual(WATCH_COMMANDS["agent-cli-cleanup"], ("agent-cli", "cleanup", "--watch"))

    def test_transcript_and_update_review_are_managed_watch_targets(self):
        self.assertIn("review", MANAGED_WATCH_TARGETS)
        self.assertIn("update-review", MANAGED_WATCH_TARGETS)
        self.assertEqual(WATCH_COMMANDS["review"], ("review", "watch"))
        self.assertEqual(WATCH_COMMANDS["update-review"], ("update-review", "watch"))

    def test_pruner_is_a_managed_watch_target(self):
        self.assertIn("pruner", MANAGED_WATCH_TARGETS)
        self.assertEqual(WATCH_COMMANDS["pruner"], ("prune", "watch"))

    def test_insight_is_a_managed_watch_target(self):
        self.assertIn("insight", MANAGED_WATCH_TARGETS)
        self.assertEqual(WATCH_COMMANDS["insight"], ("insight", "watch"))

    def test_watch_status_reports_stopped_without_config(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            with (
                patch("rightmemory.cli.default_memory_root", return_value=Path(tempdir)),
                patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "status"])

        self.assertEqual(result, 0)
        self.assertIn("review: stopped", stdout.getvalue())
        self.assertIn("update-review: stopped", stdout.getvalue())
        self.assertIn("dreamer: stopped", stdout.getvalue())
        self.assertIn("pruner: stopped", stdout.getvalue())
        self.assertIn("insight: stopped", stdout.getvalue())
        self.assertIn("sync: stopped", stdout.getvalue())

    def test_watch_process_command_prefers_proc_cmdline(self):
        with (
            patch("rightmemory.platform.IS_WINDOWS", False),
            patch("rightmemory.watch.Path.read_bytes", return_value=b"python\0-m\0rightmemory.cli\0review\0watch\0"),
        ):
            command = _process_command(123)

        self.assertEqual(command, "python -m rightmemory.cli review watch")

    def test_main_status_prints_operational_dashboard(self):
        stdout = io.StringIO()
        dashboard = "RightMemory\n  root: /memory/root\n  git: clean on main @ abc1234"

        with (
            patch("rightmemory.cli.default_memory_root", return_value=Path("/memory/root")),
            patch("rightmemory.cli.collect_status", return_value=object()) as collect_status,
            patch("rightmemory.cli.format_status_dashboard", return_value=dashboard),
            patch("sys.stdout", stdout),
        ):
            result = main(["status"])

        self.assertEqual(result, 0)
        collect_status.assert_called_once_with(Path("/memory/root"))
        self.assertEqual(stdout.getvalue().strip(), dashboard)

    def test_watch_status_remains_managed_watch_process_view(self):
        stdout = io.StringIO()
        status = type(
            "WatchStatus",
            (),
            {
                "name": "review",
                "state": "running",
                "pid": 123,
                "log_path": Path("/memory/.runtime/watch/review.log"),
            },
        )()

        with (
            patch("rightmemory.cli.default_memory_root", return_value=Path("/memory")),
            patch("rightmemory.cli.managed_watch_status", return_value=status),
            patch("sys.stdout", stdout),
        ):
            result = main(["watch", "status", "review"])

        self.assertEqual(result, 0)
        self.assertIn(f"review: running pid 123, log {Path('/memory/.runtime/watch/review.log')}", stdout.getvalue())
        self.assertNotIn("Async Update", stdout.getvalue())

    def test_sync_watch_help_does_not_load_config(self):
        stdout = io.StringIO()

        with (
            patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
            patch("rightmemory.cli.load_sync_config", side_effect=AssertionError("sync config should not load")),
            patch("sys.stdout", stdout),
        ):
            with self.assertRaises(SystemExit) as caught:
                main(["sync", "watch", "--help"])

        self.assertEqual(caught.exception.code, 0)
        self.assertIn("rightmemory sync watch", stdout.getvalue())

    def test_deferred_sync_runs_exactly_one_shared_cycle(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            sync_config = SimpleNamespace(
                memory_root=Path(tempdir),
                enabled=True,
                stale_pull_after_hours=24,
            )
            result_obj = SimpleNamespace(status="synced", message="local memory is current")
            with (
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli._run_sync_cycle", return_value=result_obj) as cycle,
                patch("sys.stdout", stdout),
            ):
                result = main(["sync", "_deferred"])

        self.assertEqual(result, 0)
        cycle.assert_called_once_with(sync_config)
        self.assertIn("local memory is current", stdout.getvalue())

    def test_deferred_sync_skips_disabled_config(self):
        with tempfile.TemporaryDirectory() as tempdir:
            sync_config = SimpleNamespace(
                memory_root=Path(tempdir),
                enabled=False,
                stale_pull_after_hours=24,
            )
            with (
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli._run_sync_cycle") as cycle,
            ):
                result = main(["sync", "_deferred"])

        self.assertEqual(result, 0)
        cycle.assert_not_called()

    def test_active_sync_reconciler_repairs_locally_before_outer_cycle_pushes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            sync_config = SyncConfig(memory_root=memory_root, enabled=True)
            reconciler_config = RuntimeConfig(
                role="sync-reconciler",
                model_id="openai/test",
                memory_root=memory_root,
                sync=sync_config,
            )
            manager = Mock(memory_root=memory_root)
            manager.repair_message.return_value = "repair dirty memory"
            diagnostic = SimpleNamespace(status="dirty", message="dirty", files=["MEMORY.md"])
            with (
                patch("rightmemory.cli.load_config", return_value=reconciler_config),
                patch("rightmemory.cli.RightMemoryRuntime") as runtime_class,
            ):
                _run_active_sync_reconciler(manager, diagnostic, memory_root)

        local_config = runtime_class.call_args.args[0]
        self.assertFalse(local_config.sync.enabled)
        runtime_class.return_value.run_session_turn.assert_called_once_with(
            "runtime-sync-repair",
            "repair dirty memory",
        )
        runtime_class.return_value.cleanup.assert_called_once_with()

    def test_sync_watch_rejects_non_positive_interval(self):
        with patch("rightmemory.cli.load_sync_config", side_effect=AssertionError("sync config should not load")):
            with self.assertRaises(ValueError):
                main(["sync", "watch", "--interval", "0"])

    def test_sync_watch_sleeps_until_interrupted(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            sync_config = type("SyncConfig", (), {"memory_root": Path(tempdir), "enabled": True, "stale_pull_after_hours": 24})()
            result_obj = type("Result", (), {"status": "synced", "message": "local memory is current", "files": []})()
            with (
                patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.background_sync.return_value = result_obj
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(60)
        self.assertIn("rightmemory sync watch stopped", stderr.getvalue())

    def test_sync_watch_delegates_lock_ownership_to_manager(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        events = []

        with tempfile.TemporaryDirectory() as tempdir:
            sync_config = type("SyncConfig", (), {"memory_root": Path(tempdir), "enabled": True, "stale_pull_after_hours": 24})()
            result_obj = type("Result", (), {"status": "synced", "message": "local memory is current", "files": []})()

            def background_sync(*, repair, active_repair):
                self.assertTrue(callable(repair))
                self.assertTrue(callable(active_repair))
                events.append("background_sync")
                return result_obj

            with (
                patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli.MemoryWriteLock", side_effect=AssertionError("CLI must not own the sync lock")),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.background_sync.side_effect = background_sync
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        self.assertEqual(events, ["background_sync"])

    def test_sync_watch_background_pull_failure_logs_and_sleeps(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            sync_config = type("SyncConfig", (), {"memory_root": Path(tempdir), "enabled": True, "stale_pull_after_hours": 24})()

            with (
                patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.background_sync.side_effect = RuntimeError("boom")
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(60)
        self.assertIn("rightmemory sync check failed: RuntimeError: boom", stderr.getvalue())

    def test_sync_watch_stops_after_consecutive_failures(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            sync_config = type("SyncConfig", (), {"memory_root": Path(tempdir), "enabled": True, "stale_pull_after_hours": 24})()

            with (
                patch("rightmemory.cli.DEFAULT_WATCH_MAX_CONSECUTIVE_FAILURES", 2),
                patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli._sleep_with_refresh_check", return_value=True) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.background_sync.side_effect = RuntimeError("boom")
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 1)
        sleep.assert_called_once()
        self.assertEqual(stdout.getvalue().count("rightmemory sync check"), 2)
        self.assertIn("rightmemory sync watch stopping after 2 consecutive failed cycles", stderr.getvalue())
        self.assertIn("rightmemory sync watch stopped", stderr.getvalue())

    def test_sync_watch_clean_pull_does_not_load_runtime(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            config = type("SyncConfig", (), {"memory_root": Path(tempdir), "enabled": True, "stale_pull_after_hours": 24})()
            result_obj = type("Result", (), {"status": "synced", "message": "local memory is current", "files": []})()

            with (
                patch("rightmemory.cli.load_sync_config", return_value=config),
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.background_sync.return_value = result_obj
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        self.assertIn("rightmemory sync check", stdout.getvalue())
        self.assertIn("local memory is current", stdout.getvalue())

    def test_sync_watch_conflict_invokes_sync_reconciler(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls = []

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            candidate = memory_root / ".runtime" / "worktrees" / "candidate"
            sync_config = type("SyncConfig", (), {"memory_root": memory_root, "enabled": True, "stale_pull_after_hours": 24})()
            diagnostic = type("Result", (), {"status": "conflict", "message": "conflict", "files": ["MEMORY.md"]})()
            result_obj = type(
                "Result",
                (),
                {"status": "synced", "message": "candidate published", "files": ["MEMORY.md"], "operation_id": "sync-op"},
            )()

            def background_sync(*, repair, active_repair):
                repair(candidate, diagnostic, "sync-op")
                return result_obj

            def reconcile(manager, received_candidate, received_result, operation_id, received_root):
                calls.append((manager, received_candidate, received_result, operation_id, received_root))
                return "resolved"

            with (
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli._run_sync_reconciler", side_effect=reconcile),
                patch("rightmemory.cli._finish_sync_repair") as finish,
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.memory_root = memory_root
                manager_class.return_value.background_sync.side_effect = background_sync
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        self.assertEqual(
            calls,
            [(manager_class.return_value, candidate, diagnostic, "sync-op", memory_root)],
        )
        finish.assert_called_once_with(memory_root, result_obj)
        self.assertIn("candidate published", stdout.getvalue())

    def test_sync_watch_reconciler_failure_logs_and_sleeps(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            sync_config = type("SyncConfig", (), {"memory_root": memory_root, "enabled": True, "stale_pull_after_hours": 24})()

            def background_sync(*, repair, active_repair):
                return repair(memory_root / "candidate", object(), "sync-op")

            with (
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli._run_sync_reconciler", side_effect=RuntimeError("boom")),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.memory_root = memory_root
                manager_class.return_value.background_sync.side_effect = background_sync
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(60)
        self.assertIn("rightmemory sync check failed: RuntimeError: boom", stderr.getvalue())

    def test_sync_watch_reconciler_root_mismatch_logs_and_sleeps(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir) / "memory"
            other_root = Path(tempdir) / "other"
            sync_config = type("SyncConfig", (), {"memory_root": memory_root, "enabled": True, "stale_pull_after_hours": 24})()
            reconciler_config = type("Config", (), {"memory_root": other_root})()
            result_obj = type("Result", (), {"status": "conflict", "message": "conflict", "files": ["MEMORY.md"]})()

            def background_sync(*, repair, active_repair):
                return repair(memory_root / "candidate", result_obj, "sync-op")

            with (
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli.load_config", return_value=reconciler_config),
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.memory_root = memory_root
                manager_class.return_value.background_sync.side_effect = background_sync
                manager_class.return_value.repair_message.return_value = "resolve MEMORY.md"
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(60)
        self.assertIn("sync-reconciler memory root mismatch", stderr.getvalue())

    def test_watch_stop_writes_graceful_request_and_removes_pid(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            pid_path = memory_root / ".runtime" / "watch" / "dreamer.pid"
            pid_path.parent.mkdir(parents=True)
            pid_path.write_text("123\n", encoding="utf-8")
            with (
                patch("rightmemory.cli.default_memory_root", return_value=memory_root),
                patch("rightmemory.watch._is_managed_watch_process", side_effect=[True, False]),
                patch("rightmemory.watch._write_pid", wraps=_write_pid) as write_pid,
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "stop", "dreamer"])
            pid_exists = pid_path.exists()

        self.assertEqual(result, 0)
        write_pid.assert_called_once_with(watch_stop_path(memory_root, "dreamer"), 123)
        self.assertFalse(pid_exists)
        self.assertIn("dreamer: stopped pid 123", stdout.getvalue())

    def test_dreamer_watch_once_skips_below_threshold(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls = []

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            DreamerTriggerStore(memory_root).increment(4.0)
            watch_config = _dreamer_watch_config(memory_root=memory_root, trigger_points=5.0)

            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                result = _dreamer_watch_once(
                    watch_config,
                    "dreamer-watch",
                    lambda session_id, _operation_id: calls.append(session_id) or "dream output",
                )
            trigger = DreamerTriggerStore(memory_root).read()

        self.assertEqual(result, "skipped")
        self.assertEqual(calls, [])
        self.assertEqual(trigger.points, 4.0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_dreamer_watch_once_runs_and_consumes_threshold_on_success(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls = []

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            DreamerTriggerStore(memory_root).increment(12.0)
            watch_config = _dreamer_watch_config(memory_root=memory_root, trigger_points=10.0)

            def run_cycle(session_id: str, _operation_id: str) -> str:
                calls.append(session_id)
                return f"session {session_id}: dream"

            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                result = _dreamer_watch_once(watch_config, "dreamer-watch", run_cycle)
            trigger = DreamerTriggerStore(memory_root).read()

        self.assertEqual(result, "succeeded")
        self.assertEqual(calls, ["dreamer-watch"])
        self.assertEqual(trigger.points, 2.0)
        self.assertIsNotNone(trigger.last_successful_dream_at)
        self.assertIn("rightmemory dreamer cycle", stdout.getvalue())
        self.assertIn("session dreamer-watch: dream", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_insight_watch_once_runs_and_consumes_threshold_on_success(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls = []

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            InsightTriggerStore(memory_root).increment(155.0)
            watch_config = _insight_watch_config(memory_root=memory_root, trigger_points=150.0)

            def run_cycle(session_id: str, _operation_id: str) -> str:
                calls.append(session_id)
                return f"session {session_id}: insight"

            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                result = _insight_watch_once(watch_config, "insight-watch", run_cycle)
            trigger = InsightTriggerStore(memory_root).read()

        self.assertEqual(result, "succeeded")
        self.assertEqual(calls, ["insight-watch"])
        self.assertEqual(trigger.points, 5.0)
        self.assertIsNotNone(trigger.last_successful_insight_at)
        self.assertEqual(trigger.last_successful_insight_result, "noop")
        self.assertIn("rightmemory insight cycle", stdout.getvalue())
        self.assertIn("session insight-watch: insight", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_insight_watch_once_records_artifact_result_when_log_changes(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            InsightTriggerStore(memory_root).increment(155.0)
            watch_config = _insight_watch_config(memory_root=memory_root, trigger_points=150.0)

            def run_cycle(session_id: str, _operation_id: str) -> str:
                insight = memory_root / "insight_logs" / "2026-05-30-143012.md"
                insight.parent.mkdir()
                insight.write_text(f"# Insight\n\n{session_id}\n", encoding="utf-8")
                return f"session {session_id}: insight"

            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                result = _insight_watch_once(watch_config, "insight-watch", run_cycle)
            trigger = InsightTriggerStore(memory_root).read()

        self.assertEqual(result, "succeeded")
        self.assertEqual(trigger.points, 5.0)
        self.assertEqual(trigger.last_successful_insight_result, "artifact")
        self.assertEqual(stderr.getvalue(), "")

    def test_insight_watch_once_recovers_artifact_result_from_terminal_receipt(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            trigger_store = InsightTriggerStore(memory_root)
            trigger_store.increment(155.0)
            operation_id = trigger_store.claim_operation(150.0)
            self.assertIsNotNone(operation_id)

            insight_path = memory_root / "insight_logs" / "2026-05-30-143012.md"
            insight_path.parent.mkdir()
            insight_path.write_text("# Recovered insight\n", encoding="utf-8")
            operation_store = SemanticOperationStore(memory_root)
            operation_store.begin(operation_id, {"role": "insight"})
            operation_store.prepare_outcome(
                operation_id,
                output="recovered insight",
                start_commit="base123",
                changed_paths=("insight_logs/2026-05-30-143012.md",),
            )
            operation_store.complete_commit(operation_id, "tip456")
            watch_config = _insight_watch_config(memory_root=memory_root, trigger_points=150.0)

            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                result = _insight_watch_once(
                    watch_config,
                    "insight-watch",
                    lambda _session_id, received_id: f"recovered {received_id}",
                )
            trigger = trigger_store.read()

        self.assertEqual(result, "succeeded")
        self.assertEqual(trigger.last_successful_insight_result, "artifact")
        self.assertEqual(stderr.getvalue(), "")

    def test_dreamer_watch_once_does_not_consume_on_failure(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            DreamerTriggerStore(memory_root).increment(12.0)
            watch_config = _dreamer_watch_config(memory_root=memory_root, trigger_points=10.0)

            def run_cycle(session_id: str, _operation_id: str) -> str:
                raise RuntimeError(f"boom for {session_id}")

            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                result = _dreamer_watch_once(watch_config, "dreamer-watch", run_cycle)
            trigger = DreamerTriggerStore(memory_root).read()

        self.assertEqual(result, "failed")
        self.assertEqual(trigger.points, 12.0)
        self.assertIsNone(trigger.last_successful_dream_at)
        self.assertIn("rightmemory dreamer cycle", stdout.getvalue())
        self.assertIn("rightmemory dreamer cycle failed: RuntimeError: boom for dreamer-watch", stderr.getvalue())

    def test_dreamer_watch_sleeps_with_config_interval_when_skipped(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            runtime_config = type("Config", (), {"memory_root": memory_root})()
            watch_config = _dreamer_watch_config(memory_root=memory_root, trigger_points=5.0, check_interval_seconds=7)
            with (
                patch("rightmemory.cli.load_dreamer_watch_config", return_value=watch_config),
                patch("rightmemory.cli.load_config", return_value=runtime_config),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("dreamer should wait")),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["dreamer", "watch"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(7)
        self.assertNotIn("rightmemory dreamer cycle", stdout.getvalue())
        self.assertIn("rightmemory dreamer watch stopped", stderr.getvalue())

    def test_dreamer_watch_cli_uses_trigger_config_and_runs_when_points_are_available(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls = []

        class RecordingRuntime(FakeRuntime):
            def run_cycle(self, session_id: str, operator_hint=None, *, operation_id=None) -> str:
                calls.append((session_id, operator_hint))
                return f"cycle {session_id}: {operator_hint}"

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            DreamerTriggerStore(memory_root).increment(6.0)
            old_state_path = memory_root / ".runtime" / "dreamer" / "watch-state.json"
            old_state_path.parent.mkdir(parents=True, exist_ok=True)
            old_state = {"last_run_at": "2999-01-01T00:00:00+00:00", "last_status": "succeeded"}
            old_state_path.write_text(json.dumps(old_state), encoding="utf-8")
            runtime_config = type("Config", (), {"memory_root": memory_root})()
            watch_config = _dreamer_watch_config(memory_root=memory_root, trigger_points=5.0, check_interval_seconds=9)
            with (
                patch("rightmemory.cli.load_dreamer_watch_config", return_value=watch_config),
                patch("rightmemory.cli.load_config", return_value=runtime_config),
                patch("rightmemory.cli.RightMemoryRuntime", RecordingRuntime),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["dreamer", "watch"])
            trigger = DreamerTriggerStore(memory_root).read()
            written_old_state = json.loads(old_state_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 130)
        self.assertEqual(calls, [("dreamer-watch", None)])
        self.assertEqual(trigger.points, 1.0)
        self.assertEqual(written_old_state, old_state)
        sleep.assert_called_once_with(9)
        self.assertIn("rightmemory dreamer cycle", stdout.getvalue())
        self.assertIn("cycle dreamer-watch: None", stdout.getvalue())
        self.assertIn("rightmemory dreamer watch stopped", stderr.getvalue())

    def test_insight_watch_cli_uses_cycle_entry_point(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls = []

        class RecordingRuntime(FakeRuntime):
            def run_cycle(self, session_id: str, operator_hint=None, *, operation_id=None) -> str:
                calls.append((session_id, operator_hint))
                return f"cycle {session_id}: {operator_hint}"

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            InsightTriggerStore(memory_root).increment(151.0)
            runtime_config = type("Config", (), {"memory_root": memory_root})()
            watch_config = _insight_watch_config(memory_root=memory_root, trigger_points=150.0, check_interval_seconds=9)
            with (
                patch("rightmemory.cli.load_insight_watch_config", return_value=watch_config),
                patch("rightmemory.cli.load_config", return_value=runtime_config),
                patch("rightmemory.cli.RightMemoryRuntime", RecordingRuntime),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["insight", "watch"])
            trigger = InsightTriggerStore(memory_root).read()

        self.assertEqual(result, 130)
        self.assertEqual(calls, [("insight-watch", None)])
        self.assertEqual(trigger.points, 1.0)
        sleep.assert_called_once_with(9)
        self.assertIn("rightmemory insight cycle", stdout.getvalue())
        self.assertIn("rightmemory insight watch stopped", stderr.getvalue())

    def test_dreamer_watch_failed_cycle_sleeps_without_consuming_points(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        class FailingRuntime(FakeRuntime):
            def run_cycle(self, session_id: str, operator_hint=None, *, operation_id=None) -> str:
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            DreamerTriggerStore(memory_root).increment(6.0)
            runtime_config = type("Config", (), {"memory_root": memory_root})()
            watch_config = _dreamer_watch_config(
                memory_root=memory_root,
                trigger_points=5.0,
                check_interval_seconds=3000,
            )
            with (
                patch("rightmemory.cli.load_dreamer_watch_config", return_value=watch_config),
                patch("rightmemory.cli.load_config", return_value=runtime_config),
                patch("rightmemory.cli.RightMemoryRuntime", FailingRuntime),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["dreamer", "watch"])
            trigger = DreamerTriggerStore(memory_root).read()

        self.assertEqual(result, 130)
        self.assertEqual(trigger.points, 6.0)
        sleep.assert_called_once_with(60)
        self.assertIn("rightmemory dreamer cycle", stdout.getvalue())
        self.assertIn("rightmemory dreamer cycle failed: RuntimeError: boom", stderr.getvalue())
        self.assertIn("rightmemory dreamer watch stopped", stderr.getvalue())

    def test_dreamer_watch_stops_after_consecutive_failures(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        class FailingRuntime(FakeRuntime):
            def run_cycle(self, session_id: str, operator_hint=None, *, operation_id=None) -> str:
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            DreamerTriggerStore(memory_root).increment(6.0)
            runtime_config = type("Config", (), {"memory_root": memory_root})()
            watch_config = _dreamer_watch_config(
                memory_root=memory_root,
                trigger_points=5.0,
                check_interval_seconds=3000,
            )
            with (
                patch("rightmemory.cli.DEFAULT_WATCH_MAX_CONSECUTIVE_FAILURES", 2),
                patch("rightmemory.cli.load_dreamer_watch_config", return_value=watch_config),
                patch("rightmemory.cli.load_config", return_value=runtime_config),
                patch("rightmemory.cli.RightMemoryRuntime", FailingRuntime),
                patch("rightmemory.cli._sleep_with_refresh_check", return_value=True) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["dreamer", "watch"])
            trigger = DreamerTriggerStore(memory_root).read()

        self.assertEqual(result, 1)
        self.assertEqual(trigger.points, 6.0)
        sleep.assert_called_once()
        self.assertEqual(stdout.getvalue().count("rightmemory dreamer cycle"), 2)
        self.assertIn("rightmemory dreamer watch stopping after 2 consecutive failed cycles", stderr.getvalue())
        self.assertIn("rightmemory dreamer watch stopped", stderr.getvalue())

    def test_dreamer_watch_interval_overrides_trigger_check_interval(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            runtime_config = type("Config", (), {"memory_root": memory_root})()
            watch_config = _dreamer_watch_config(memory_root=memory_root, trigger_points=5.0, check_interval_seconds=7)
            with (
                patch("rightmemory.cli.load_dreamer_watch_config", return_value=watch_config),
                patch("rightmemory.cli.load_config", return_value=runtime_config),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("dreamer should wait")),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["dreamer", "watch", "--interval", "11"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(11)
        self.assertNotIn("rightmemory dreamer cycle", stdout.getvalue())
        self.assertIn("rightmemory dreamer watch stopped", stderr.getvalue())

    def test_dreamer_watch_rejects_non_positive_interval(self):
        with patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")):
            with self.assertRaises(ValueError):
                main(["dreamer", "watch", "--interval", "0"])

    def test_role_watch_is_supported_for_dreamer_and_insight_roles(self):
        with patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")):
            with self.assertRaises(ValueError):
                main(["retrieve", "watch"])

    def test_review_normalize_prints_normalized_session_without_loading_config(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "session.jsonl"
            rows = [
                {"type": "session_meta", "timestamp": "t0", "payload": {"id": "s1", "cwd": "/repo"}},
                {"type": "event_msg", "timestamp": "t1", "payload": {"type": "user_message", "message": "hello"}},
                {"type": "event_msg", "timestamp": "t2", "payload": {"type": "agent_message", "message": "hi"}},
                {"type": "event_msg", "timestamp": "t3", "payload": {"type": "task_complete"}},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            with (
                patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
                patch("sys.stdout", stdout),
            ):
                result = main(["review", "normalize", "--source", "codex", "--path", str(path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["source"], "codex")
        self.assertEqual(payload["session_id"], "s1")
        self.assertNotIn("already_reviewed_turns", payload)
        self.assertNotIn("i", payload["turns"][0])
        self.assertEqual(payload["turns"][0]["user"], "hello")

    def test_main_runs_one_shot_session_turn(self):
        roles = []
        stdout = io.StringIO()

        def fake_load_config(role, **kwargs):
            roles.append(role)
            return object()

        with (
            patch("rightmemory.cli.load_config", fake_load_config),
            patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
            patch("sys.stdout", stdout),
        ):
            result = main(["retrieve", "--session", "agent-1", "hello", "there"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["retrieve"])
        self.assertEqual(stdout.getvalue().strip(), "session agent-1: hello there")

    def test_retrieve_include_returned_is_forwarded_for_one_call(self):
        calls = []

        class IncludeReturnedRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str, *, include_returned: bool = False) -> str:
                calls.append((session_id, message, include_returned))
                return "repeated context"

        with (
            patch("rightmemory.cli.load_config", return_value=object()),
            patch("rightmemory.cli.RightMemoryRuntime", IncludeReturnedRuntime),
            patch("sys.stdout", io.StringIO()),
        ):
            result = main(
                [
                    "retrieve",
                    "--include-returned",
                    "--session",
                    "agent-1",
                    "show it again",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(calls, [("agent-1", "show it again", True)])

    def test_main_records_one_pressure_unit_for_memory_changing_update_turn(self):
        stdout = io.StringIO()
        memory_root = Path("/memory")
        config = type("Config", (), {"role": "update", "memory_root": memory_root})()

        class UpdateRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str, **_kwargs) -> str:
                self.last_write_result = IsolatedWriteResult(
                    output="updated",
                    commits_landed=1,
                    changed_paths=("MEMORY.md",),
                )
                return "updated"

        with (
            patch("rightmemory.cli.load_config", return_value=config),
            patch("rightmemory.cli.RightMemoryRuntime", UpdateRuntime),
            patch("rightmemory.cli._record_memory_change_pressure") as pressure,
            patch("sys.stdout", stdout),
        ):
            result = main(["update", "--session", "agent-1", "candidate"])

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().strip(), "updated")
        pressure.assert_called_once_with(memory_root)

    def test_main_cleans_up_after_one_shot_session_turn(self):
        cleaned = []

        class CleanupRuntime(FakeRuntime):
            def cleanup(self):
                cleaned.append(True)

        with (
            patch("rightmemory.cli.load_config", return_value=object()),
            patch("rightmemory.cli.RightMemoryRuntime", CleanupRuntime),
            patch("sys.stdout", io.StringIO()),
        ):
            result = main(["retrieve", "--session", "agent-1", "hello"])

        self.assertEqual(result, 0)
        self.assertEqual(cleaned, [True])

    def test_main_submits_async_update_without_building_runtime(self):
        roles = []
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                roles.append(role)
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", stdout),
            ):
                popen.return_value.pid = 123
                result = main(["update", "submit", "--session", "agent-1", "remember", "this"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["update"])
        self.assertIn("status: running", stdout.getvalue())
        self.assertIn("phase: waiting", stdout.getvalue())
        self.assertIn("session: agent-1", stdout.getvalue())
        self.assertIn("current_batch: 0", stdout.getvalue())
        self.assertIn("pending: 1", stdout.getvalue())
        self.assertIn("pending_ids: 1", stdout.getvalue())

    def test_submit_into_manual_recovery_prints_critical_warning(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            store = AsyncUpdateStore(memory_root, "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="needs_manual_recovery",
                    session_id="agent-1",
                    role="update",
                    attempts=2,
                    error="previous failure",
                    last_error="previous failure",
                    pending=[],
                    next_id=1,
                ),
            )

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", stdout),
            ):
                popen.return_value.pid = 123
                result = main(["update", "submit", "--session", "agent-1", "new", "candidate"])

        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("status: needs_manual_recovery", output)
        self.assertIn("pending: 1", output)
        self.assertIn("CRITICAL: this async RightMemory update session is blocked after 2 failed attempts.", output)
        self.assertIn("Agent: report this issue to the user", output)
        popen.assert_not_called()

    def test_submit_is_only_supported_for_update_role(self):
        with patch("rightmemory.cli.load_config", return_value=object()):
            with self.assertRaises(ValueError):
                main(["retrieve", "submit", "--session", "agent-1", "remember", "this"])

    def test_undo_is_only_supported_for_update_role(self):
        with patch("rightmemory.cli.load_config", return_value=object()):
            with self.assertRaises(ValueError):
                main(["retrieve", "undo", "--session", "agent-1", "1"])

    def test_retry_is_only_supported_for_update_role(self):
        with patch("rightmemory.cli.load_config", return_value=object()):
            with self.assertRaises(ValueError):
                main(["retrieve", "retry"])

    def test_subcommand_help_does_not_load_config(self):
        stdout = io.StringIO()

        with (
            patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
            patch("sys.stdout", stdout),
        ):
            with self.assertRaises(SystemExit) as caught:
                main(["update", "submit", "--help"])

        self.assertEqual(caught.exception.code, 0)
        self.assertIn("rightmemory update submit", stdout.getvalue())

    def test_update_help_reveals_retry_command_without_loading_config(self):
        stdout = io.StringIO()

        with (
            patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
            patch("sys.stdout", stdout),
        ):
            with self.assertRaises(SystemExit) as caught:
                main(["update", "--help"])

        output = stdout.getvalue()
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("rightmemory update", output)
        self.assertIn("retry", output)
        self.assertIn("manual recovery", output)

    def test_update_retry_help_reveals_global_manual_recovery(self):
        stdout = io.StringIO()

        with (
            patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
            patch("sys.stdout", stdout),
        ):
            with self.assertRaises(SystemExit) as caught:
                main(["update", "retry", "--help"])

        output = stdout.getvalue()
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("rightmemory update retry", output)
        self.assertIn("manual recovery", output)
        self.assertIn("No --session is required", output)

    def test_main_accumulates_pending_update_while_worker_is_waiting(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                _fake_async_worker_process(),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", stdout),
            ):
                popen.return_value.pid = 123
                first = main(["update", "submit", "--session", "agent-1", "first"])
                second = main(["update", "submit", "--session", "agent-1", "second"])
                pull = main(["update", "pull", "--session", "agent-1"])

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertEqual(pull, 0)
        self.assertEqual(popen.call_count, 1)
        output = stdout.getvalue()
        self.assertIn("status: running", output)
        self.assertIn("phase: waiting", output)
        self.assertIn("current_batch: 0", output)
        self.assertIn("pending: 2", output)
        self.assertIn("pending_ids: 1, 2", output)

    def test_main_cancels_pending_update_without_building_runtime(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                _fake_async_worker_process(),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", stdout),
            ):
                popen.return_value.pid = 123
                first = main(["update", "submit", "--session", "agent-1", "first"])
                second = main(["update", "submit", "--session", "agent-2", "second"])
                undo = main(["update", "undo", "--session", "agent-1", "1"])
                state = AsyncUpdateStore(memory_root, "update").read("agent-1")

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertEqual(undo, 0)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual([job.id for job in state.pending], [])
        output = stdout.getvalue()
        self.assertIn("canceled pending candidate: 1", output)
        self.assertIn("pending: 0", output)

    def test_main_cancels_attempted_candidate_through_git_fence(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            store = AsyncUpdateStore(memory_root, "update")
            with patch.object(store, "_start_worker_if_needed"):
                state = store.submit("agent-1", "attempted")
            candidate = state.pending[0]
            queue_store = UpdateQueueStore(memory_root)
            queue_store.begin_publication(
                candidate.candidate_uid,
                attempted_at=candidate.submitted_at,
            )
            coordinator = Mock()
            coordinator.cancel_attempted.return_value = "canceled"

            def clear_local(candidate_uids):
                for uid in candidate_uids:
                    queue_store.remove_outbox(uid)
                    queue_store.clear_publication_marker(uid)

            coordinator.clear_local_candidates.side_effect = clear_local

            def fake_load_config(role, **kwargs):
                return SimpleNamespace(memory_root=memory_root)

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch(
                    "rightmemory.cli.load_sync_config",
                    return_value=SyncConfig(memory_root=memory_root, enabled=True),
                ),
                patch(
                    "rightmemory.cli.GitUpdateQueueCoordinator",
                    return_value=coordinator,
                ),
                patch.object(
                    UpdateQueueStore,
                    "publication_state",
                    side_effect=(
                        "attempted",
                        AssertionError("undo must not infer authority from a later marker read"),
                    ),
                ),
                patch("sys.stdout", stdout),
            ):
                result = main(["update", "undo", "--session", "agent-1", "1"])

            final_state = store.read("agent-1")

        self.assertEqual(result, 0)
        self.assertEqual(final_state.pending, [])
        self.assertIsNone(queue_store.read_outbox(candidate.candidate_uid))
        self.assertEqual(queue_store.publication_state(candidate.candidate_uid), "missing")
        coordinator.cancel_attempted.assert_called_once()
        self.assertIn("canceled pending candidate: 1", stdout.getvalue())

    def test_main_reports_non_pending_update_undo(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch(
                    "rightmemory.cli.load_sync_config",
                    side_effect=AssertionError("integer ids must stay local"),
                ),
                patch("sys.stdout", stdout),
            ):
                result = main(["update", "undo", "--session", "agent-1", "1"])

        self.assertEqual(result, 0)
        self.assertIn("candidate is not pending: 1", stdout.getvalue())
        self.assertIn("status: idle", stdout.getvalue())

    def test_pull_marks_dead_worker_failed_and_keeps_pending_updates(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                _fake_async_worker_process(),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", io.StringIO()),
            ):
                popen.return_value.pid = 123
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "first"]), 0)
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "second"]), 0)

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update._process_exists", return_value=False),
                patch("sys.stdout", stdout),
            ):
                pull = main(["update", "pull", "--session", "agent-1"])

        self.assertEqual(pull, 0)
        output = stdout.getvalue()
        self.assertIn("status: failed", output)
        self.assertIn("current_batch: 0", output)
        self.assertIn("pending: 2", output)
        self.assertIn("pending_ids: 1, 2", output)
        self.assertIn("error: worker process exited before writing result", output)

    def test_main_pulls_async_update_state(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with patch("rightmemory.cli.load_config", fake_load_config), patch("sys.stdout", stdout):
                result = main(["update", "pull", "--session", "agent-1"])

        self.assertEqual(result, 0)
        self.assertIn("status: idle", stdout.getvalue())

    def test_update_retry_requeues_manual_recovery_without_session(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            async_root = memory_root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            (async_root / "agent-1.json").write_text(
                json.dumps(
                    {
                        "status": "needs_manual_recovery",
                        "session_id": "agent-1",
                        "role": "update",
                        "phase": None,
                        "started_at": None,
                        "finished_at": None,
                        "pid": None,
                        "result": None,
                        "error": "boom",
                        "attempts": 2,
                        "next_retry_at": None,
                        "last_error": "boom",
                        "next_flush_at": None,
                        "current_batch": [],
                        "pending": [
                            {
                                "id": 1,
                                "candidate_uid": f"{1:032x}",
                                "message": "manual item",
                                "submitted_at": "2026-05-15T00:00:00+00:00",
                            }
                        ],
                        "next_id": 2,
                    }
                ),
                encoding="utf-8",
            )

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                _fake_async_worker_process(),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", stdout),
            ):
                popen.return_value.pid = 123
                result = main(["update", "retry"])

        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("requeued sessions: 1", output)
        self.assertIn("requeued candidates: 1", output)
        self.assertIn("worker: started pid 123", output)

    def test_async_worker_processes_multiple_sessions_as_one_batch(self):
        calls = []

        class RecordingRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str, **_kwargs) -> str:
                calls.append((session_id, message))
                return f"session {session_id}: {message}"

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_async_update_config", return_value=_async_update_config(memory_root, target=2)),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.async_update.UPDATE_DEBOUNCE_SECONDS", 0),
                _fake_async_worker_process(),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", io.StringIO()),
            ):
                popen.return_value.pid = 123
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "first"]), 0)
                self.assertEqual(main(["update", "submit", "--session", "agent-2", "second"]), 0)

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_async_update_config", return_value=_async_update_config(memory_root, target=2)),
                patch("rightmemory.cli.RightMemoryRuntime", RecordingRuntime),
                patch("rightmemory.cli.load_dreamer_watch_config", return_value=_dreamer_watch_config()),
                patch("rightmemory.cli.load_insight_watch_config", return_value=_insight_watch_config()),
            ):
                result = main(["update", "_async-worker"])

            stdout = io.StringIO()
            with patch("rightmemory.cli.load_config", fake_load_config), patch("sys.stdout", stdout):
                pull_result = main(["update", "pull", "--session", "agent-1"])

        self.assertEqual(result, 0)
        self.assertEqual(pull_result, 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].startswith("update-batch-"))
        self.assertIn("Process the following submitted RightMemory candidates as one ordered batch.", calls[0][1])
        self.assertIn("[update session: agent-1 | candidate: 1", calls[0][1])
        self.assertIn("[update session: agent-2 | candidate: 1", calls[0][1])
        self.assertIn("status: succeeded", stdout.getvalue())
        self.assertIn("pending: 0", stdout.getvalue())
        self.assertIn("result: session update-batch-", stdout.getvalue())

    def test_async_worker_reloads_update_config_between_batches(self):
        runtime_models = []

        class RecordingRuntime(FakeRuntime):
            def __init__(self, config=None):
                super().__init__(config)
                runtime_models.append(config.model)

            def run_session_turn(self, session_id: str, message: str, **_kwargs) -> str:
                return f"model {self.config.model}"

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            async_root = memory_root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            base_state = {
                "status": "running",
                "role": "update",
                "phase": "waiting",
                "started_at": "2026-05-29T08:00:00+00:00",
                "finished_at": None,
                "pid": None,
                "result": None,
                "error": None,
                "attempts": 0,
                "next_retry_at": None,
                "last_error": None,
                "next_flush_at": "2026-05-29T08:00:00+00:00",
                "current_batch": [],
                "next_id": 2,
            }
            for session_id in ("agent-1", "agent-2"):
                (async_root / f"{session_id}.json").write_text(
                    json.dumps(
                        {
                            **base_state,
                            "session_id": session_id,
                            "pending": [
                                {
                                    "id": 1,
                                    "candidate_uid": f"{1:032x}",
                                    "message": session_id,
                                    "submitted_at": "2026-05-29T08:00:00+00:00",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            models = iter(("initial", "fresh-a", "fresh-b"))

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root, "model": next(models)})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_async_update_config", return_value=_async_update_config(memory_root, target=1)),
                patch("rightmemory.cli.load_dreamer_watch_config", return_value=_dreamer_watch_config()),
                patch("rightmemory.cli.load_insight_watch_config", return_value=_insight_watch_config()),
                patch("rightmemory.cli.RightMemoryRuntime", RecordingRuntime),
            ):
                result = main(["update", "_async-worker"])

            first = AsyncUpdateStore(memory_root, "update").read("agent-1")
            second = AsyncUpdateStore(memory_root, "update").read("agent-2")

        self.assertEqual(result, 0)
        self.assertEqual(runtime_models, ["fresh-a", "fresh-b"])
        self.assertEqual(first.result, "model fresh-a")
        self.assertEqual(second.result, "model fresh-b")

    def test_async_worker_leaves_pressure_to_runtime_operation_effects(self):
        class RecordingRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str, **_kwargs) -> str:
                self.last_write_result = IsolatedWriteResult(
                    output="updated",
                    commits_landed=1,
                    changed_paths=("MEMORY.md",),
                )
                return "updated"

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_async_update_config", return_value=_async_update_config(memory_root, target=2)),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.async_update.UPDATE_DEBOUNCE_SECONDS", 0),
                _fake_async_worker_process(),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", io.StringIO()),
            ):
                popen.return_value.pid = 123
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "first"]), 0)
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "second"]), 0)

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_async_update_config", return_value=_async_update_config(memory_root, target=2)),
                patch(
                    "rightmemory.cli.load_dreamer_watch_config",
                    return_value=_dreamer_watch_config(update_candidate_points=2.5),
                ),
                patch(
                    "rightmemory.cli.load_insight_watch_config",
                    return_value=_insight_watch_config(memory_root=memory_root, update_candidate_points=2.5),
                ),
                patch("rightmemory.cli.RightMemoryRuntime", RecordingRuntime),
            ):
                result = main(["update", "_async-worker"])

            trigger = DreamerTriggerStore(memory_root).read()
            insight_trigger = InsightTriggerStore(memory_root).read()

        self.assertEqual(result, 0)
        self.assertEqual(trigger.points, 0.0)
        self.assertEqual(insight_trigger.points, 0.0)

    def test_async_worker_does_not_add_pressure_for_pursuit_only_update(self):
        class RecordingRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str, **_kwargs) -> str:
                self.last_write_result = IsolatedWriteResult(
                    output="updated",
                    commits_landed=1,
                    changed_paths=("PURSUITS.md",),
                )
                return "updated"

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_async_update_config", return_value=_async_update_config(memory_root, target=1)),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.async_update.UPDATE_DEBOUNCE_SECONDS", 0),
                _fake_async_worker_process(),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", io.StringIO()),
            ):
                popen.return_value.pid = 123
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "first"]), 0)

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_async_update_config", return_value=_async_update_config(memory_root, target=1)),
                patch("rightmemory.cli.load_dreamer_watch_config", side_effect=AssertionError("not used")),
                patch("rightmemory.cli.load_insight_watch_config", side_effect=AssertionError("not used")),
                patch("rightmemory.cli.RightMemoryRuntime", RecordingRuntime),
            ):
                result = main(["update", "_async-worker"])

            trigger = DreamerTriggerStore(memory_root).read()
            insight_trigger = InsightTriggerStore(memory_root).read()

        self.assertEqual(result, 0)
        self.assertEqual(trigger.points, 0.0)
        self.assertEqual(insight_trigger.points, 0.0)

    def test_async_worker_does_not_reapply_runtime_pressure(self):
        class RecordingRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str, **_kwargs) -> str:
                self.last_write_result = IsolatedWriteResult(
                    output="updated",
                    commits_landed=1,
                    changed_paths=("MEMORY.md",),
                )
                return "updated"

        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_async_update_config", return_value=_async_update_config(memory_root, target=1)),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.async_update.UPDATE_DEBOUNCE_SECONDS", 0),
                _fake_async_worker_process(),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", io.StringIO()),
            ):
                popen.return_value.pid = 123
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "first"]), 0)

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_async_update_config", return_value=_async_update_config(memory_root, target=1)),
                patch(
                    "rightmemory.cli.load_dreamer_watch_config",
                    return_value=_dreamer_watch_config(update_candidate_points=2.5),
                ),
                patch("rightmemory.cli.load_insight_watch_config", return_value=_insight_watch_config(memory_root=memory_root)),
                patch("rightmemory.cli.DreamerTriggerStore") as trigger_store,
                patch("rightmemory.cli.RightMemoryRuntime", RecordingRuntime),
                patch("sys.stderr", stderr),
            ):
                result = main(["update", "_async-worker"])

        self.assertEqual(result, 0)
        trigger_store.assert_not_called()
        self.assertEqual(stderr.getvalue(), "")

    def test_submitted_worker_private_command_is_removed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
            ):
                with self.assertRaises(SystemExit):
                    main(["update", "_submitted-worker", "--session", "agent-1"])


if __name__ == "__main__":
    unittest.main()
