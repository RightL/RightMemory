import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.semantic_operation import (
    OperationConflictError,
    OperationEffect,
    SemanticOperationStore,
)


class SemanticOperationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.store = SemanticOperationStore(self.root)

    def test_begin_hashes_full_input_but_retains_only_replay_metadata(self):
        input_data = {"sessions": [{"id": "agent-1", "message": "remember this"}], "role": "update"}

        with patch("rightmemory.semantic_operation.process_identity", return_value="proc:self"):
            record = self.store.begin("operation-1", input_data)

        canonical = json.dumps(
            input_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digest = hashlib.sha256(b"operation-1").hexdigest()
        self.assertEqual(record.input_data, {"role": "update"})
        self.assertEqual(record.input_sha256, hashlib.sha256(canonical).hexdigest())
        self.assertEqual(record.phase, "running")
        self.assertEqual(record.owner_identity, "proc:self")
        self.assertEqual(self.store.state_root("operation-1"), self.root / ".runtime" / "operations" / "state" / digest)
        self.assertTrue((self.root / ".runtime" / "operations" / "records" / f"{digest}.json").is_file())
        self.assertTrue((self.root / ".runtime" / ".gitignore").is_file())
        self.assertNotIn("remember this", self.store.record_path("operation-1").read_text(encoding="utf-8"))
        self.assertEqual(self.store.read("operation-1"), record)

    def test_begin_is_idempotent_for_same_input_and_rejects_same_id_with_different_input(self):
        first = self.store.begin("operation-1", {"candidate": "one"})
        second = self.store.begin("operation-1", {"candidate": "one"})

        self.assertEqual(second, first)
        with self.assertRaisesRegex(OperationConflictError, "different input"):
            self.store.begin("operation-1", {"candidate": "two"})

    def test_record_path_digest_collision_fails_closed(self):
        with patch("rightmemory.semantic_operation._operation_digest", return_value="a" * 64):
            self.store.begin("operation-1", {"candidate": "one"})
            with self.assertRaisesRegex(OperationConflictError, "digest collision"):
                self.store.begin("operation-2", {"candidate": "two"})

    def test_malformed_or_tampered_record_fails_closed(self):
        self.store.begin("operation-1", {"candidate": "one"})
        path = self.store.record_path("operation-1")
        path.write_text("{not-json\n", encoding="utf-8")

        with self.assertRaises(json.JSONDecodeError):
            self.store.read("operation-1")

        self.store = SemanticOperationStore(self.root)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": "operation-1",
                    "input_sha256": "invalid",
                    "input_data": {"candidate": "one"},
                    "owner_pid": 1,
                    "owner_identity": None,
                    "phase": "running",
                    "outcome": None,
                    "effects": [],
                    "failure": None,
                    "created_at": "2026-07-20T00:00:00+00:00",
                    "updated_at": "2026-07-20T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "input_sha256"):
            self.store.read("operation-1")

    def test_prepared_commit_output_survives_landing_and_finalization_is_idempotent(self):
        self.store.begin("operation-1", {"candidate": "one"})
        effect = OperationEffect("update-review", metadata={"review_id": "review-1"})

        prepared = self.store.prepare_outcome(
            "operation-1",
            output="updated memory",
            start_commit="base123",
            changed_paths=("MEMORY.md",),
            effects=(effect,),
        )
        completed = self.store.complete_commit("operation-1", "tip456")
        repeated = self.store.complete_commit("operation-1", "tip456")

        self.assertEqual(prepared.phase, "prepared")
        self.assertEqual(prepared.outcome.output, "updated memory")
        self.assertEqual(completed.phase, "committed")
        self.assertEqual(completed.outcome.landed_commit, "tip456")
        self.assertEqual(repeated, completed)
        with self.assertRaisesRegex(OperationConflictError, "different landed commit"):
            self.store.complete_commit("operation-1", "other789")

    def test_completed_no_change_is_recovered_without_reexecuting(self):
        input_data = {"candidate": "already known"}
        self.store.begin("operation-1", input_data)
        self.store.prepare_outcome(
            "operation-1",
            output="no durable change",
            start_commit="base123",
            changed_paths=(),
            effects=(),
        )
        completed = self.store.complete_no_change("operation-1")

        with (
            patch("rightmemory.semantic_operation.os.getpid", return_value=9999),
            patch("rightmemory.semantic_operation.process_exists", return_value=True),
            patch("rightmemory.semantic_operation.process_identity", return_value="proc:other"),
        ):
            recovered = SemanticOperationStore(self.root).begin("operation-1", input_data)

        self.assertEqual(completed.phase, "no_change")
        self.assertEqual(completed.outcome.output, "no durable change")
        self.assertEqual(completed.outcome.landed_commit, "base123")
        self.assertEqual(recovered, completed)

    def test_stale_owner_recovers_prepared_output_and_completes_landed_commit(self):
        input_data = {"candidate": "one"}
        with (
            patch("rightmemory.semantic_operation.os.getpid", return_value=101),
            patch("rightmemory.semantic_operation.process_identity", return_value="proc:first"),
        ):
            self.store.begin("operation-1", input_data)
            self.store.prepare_outcome(
                "operation-1",
                output="prepared before landing",
                start_commit="base123",
                changed_paths=("MEMORY.md",),
            )

        with (
            patch("rightmemory.semantic_operation.os.getpid", return_value=202),
            patch("rightmemory.semantic_operation.process_exists", return_value=False),
            patch("rightmemory.semantic_operation.process_identity", return_value="proc:second"),
        ):
            claimed = self.store.begin("operation-1", input_data)
            completed = self.store.complete_commit("operation-1", "tip456")

        self.assertEqual(claimed.phase, "prepared")
        self.assertEqual(claimed.outcome.output, "prepared before landing")
        self.assertEqual(claimed.owner_pid, 202)
        self.assertEqual(completed.phase, "committed")
        self.assertEqual(completed.outcome.landed_commit, "tip456")

    def test_effect_failure_remains_replayable_and_done_metadata_is_durable(self):
        self.store.begin("operation-1", {"candidate": "one"})
        self.store.prepare_outcome(
            "operation-1",
            output="updated",
            start_commit="base123",
            changed_paths=("MEMORY.md",),
            effects=(OperationEffect("publish", metadata={"operation_key": "operation-1"}),),
        )
        self.store.complete_commit("operation-1", "tip456")

        self.assertEqual([effect.name for effect in self.store.list_pending_effects("operation-1")], ["publish"])
        with self.assertRaisesRegex(OperationConflictError, "cannot overwrite durable metadata"):
            self.store.mark_effect(
                "operation-1",
                "publish",
                "done",
                metadata={"operation_key": "different-operation"},
            )
        failed = self.store.mark_effect(
            "operation-1",
            "publish",
            "failed",
            error="hub unavailable",
            metadata={"attempts": 1},
        )
        self.assertEqual(failed.effects[0].status, "failed")
        self.assertEqual(failed.effects[0].metadata, {"attempts": 1, "operation_key": "operation-1"})
        self.assertEqual([effect.name for effect in self.store.list_pending_effects("operation-1")], ["publish"])
        with self.assertRaisesRegex(OperationConflictError, "cannot overwrite durable metadata"):
            self.store.mark_effect(
                "operation-1",
                "publish",
                "failed",
                error="hub still unavailable",
                metadata={"attempts": 2},
            )

        done = self.store.mark_effect(
            "operation-1",
            "publish",
            "done",
            metadata={"version_id": "version-1"},
        )
        repeated = self.store.mark_effect(
            "operation-1",
            "publish",
            "done",
            metadata={"version_id": "version-1"},
        )
        self.assertEqual(repeated, done)
        self.assertEqual(done.effects[0].status, "done")
        self.assertIsNone(done.effects[0].error)
        self.assertEqual(done.effects[0].metadata["version_id"], "version-1")
        self.assertEqual(self.store.list_pending_effects("operation-1"), ())

    def test_new_locked_attempt_can_reclaim_receipt_from_live_process(self):
        with (
            patch("rightmemory.semantic_operation.os.getpid", return_value=101),
            patch("rightmemory.semantic_operation.process_identity", return_value="proc:first"),
        ):
            self.store.begin("operation-1", {"candidate": "one"})

        with (
            patch("rightmemory.semantic_operation.os.getpid", return_value=202),
            patch("rightmemory.semantic_operation.process_exists", return_value=True),
            patch("rightmemory.semantic_operation.process_identity", return_value="proc:second"),
        ):
            with self.store.execution_locked():
                claimed = self.store.begin("operation-1", {"candidate": "one"})

        self.assertEqual(claimed.owner_pid, 202)
        self.assertEqual(claimed.owner_identity, "proc:second")

    def test_duplicate_execution_does_not_steal_final_receipt_from_effect_worker(self):
        with (
            patch("rightmemory.semantic_operation.os.getpid", return_value=101),
            patch("rightmemory.semantic_operation.process_identity", return_value="proc:effects"),
        ):
            self.store.begin("operation-1", {"candidate": "one"})
            self.store.prepare_outcome(
                "operation-1",
                output="saved",
                start_commit="base123",
                changed_paths=(),
                effects=(OperationEffect("publish"),),
            )
            completed = self.store.complete_no_change("operation-1")

        with (
            patch("rightmemory.semantic_operation.os.getpid", return_value=202),
            patch("rightmemory.semantic_operation.process_identity", return_value="proc:duplicate"),
        ):
            duplicate = self.store.begin("operation-1", {"candidate": "one"})
            settled = self.store.mark_effect("operation-1", "publish", "done")

        self.assertEqual(duplicate.owner_pid, completed.owner_pid)
        self.assertEqual(duplicate.owner_identity, completed.owner_identity)
        self.assertTrue(settled.complete)

    def test_outstanding_index_keeps_only_unsettled_operations(self):
        self.store.begin("operation-running", {"candidate": "one"})
        self.store.begin("operation-pending", {"candidate": "two"})
        self.store.prepare_outcome(
            "operation-pending",
            output="saved",
            start_commit="base123",
            changed_paths=(),
            effects=(OperationEffect("publish"),),
        )
        self.store.complete_no_change("operation-pending")
        self.store.begin("operation-settled", {"candidate": "three"})
        self.store.prepare_outcome(
            "operation-settled",
            output="saved",
            start_commit="base123",
            changed_paths=(),
        )
        self.store.complete_no_change("operation-settled")

        self.assertEqual(
            [record.operation_id for record in self.store.list_outstanding_records()],
            ["operation-pending", "operation-running"],
        )
        self.store.mark_effect("operation-pending", "publish", "done")
        self.assertEqual(
            [record.operation_id for record in self.store.list_outstanding_records()],
            ["operation-running"],
        )
        self.assertEqual(len(self.store.list_records()), 3)

    def test_marker_without_receipt_is_discarded_as_an_abandoned_start(self):
        marker = self.store._outstanding_path("abandoned-operation")
        marker.parent.mkdir(parents=True)
        marker.write_text(
            json.dumps({"operation_id": "abandoned-operation"}) + "\n",
            encoding="utf-8",
        )

        self.assertEqual(self.store.list_outstanding_records(), ())
        self.assertFalse(marker.exists())

    def test_preparation_sequence_is_monotonic_when_the_clock_moves_backward(self):
        self.store.begin("operation-first", {"candidate": "first"})
        self.store.begin("operation-second", {"candidate": "second"})

        with patch(
            "rightmemory.semantic_operation._now",
            return_value="2026-07-20T00:00:02+00:00",
        ):
            first = self.store.prepare_outcome(
                "operation-first",
                output="first",
                start_commit="base123",
                changed_paths=(),
            )
        with patch(
            "rightmemory.semantic_operation._now",
            return_value="2026-07-20T00:00:01+00:00",
        ):
            second = self.store.prepare_outcome(
                "operation-second",
                output="second",
                start_commit="base123",
                changed_paths=(),
            )

        self.assertEqual(first.outcome.sequence, 1)
        self.assertEqual(second.outcome.sequence, 2)

    def test_effect_retry_cursor_moves_past_a_poison_operation(self):
        records = []
        for operation_id in ("operation-poison", "operation-ready"):
            self.store.begin(operation_id, {"candidate": operation_id})
            self.store.prepare_outcome(
                operation_id,
                output="saved",
                start_commit="base123",
                changed_paths=(),
                effects=(OperationEffect("publish"),),
            )
            records.append(self.store.complete_no_change(operation_id))

        first = self.store.choose_effect_retry("role:update", records)
        second = SemanticOperationStore(self.root).choose_effect_retry("role:update", records)
        wrapped = self.store.choose_effect_retry("role:update", records)

        self.assertEqual(first.operation_id, "operation-poison")
        self.assertEqual(second.operation_id, "operation-ready")
        self.assertEqual(wrapped.operation_id, "operation-poison")

    def test_stale_or_reused_owner_is_replaced_and_failure_is_retained(self):
        with (
            patch("rightmemory.semantic_operation.os.getpid", return_value=101),
            patch("rightmemory.semantic_operation.process_identity", return_value="proc:first"),
        ):
            self.store.begin("operation-1", {"candidate": "one"})
            self.store.record_failure("operation-1", "worker exited")

        def identity(pid):
            return {101: "proc:reused", 202: "proc:second"}.get(pid)

        with (
            patch("rightmemory.semantic_operation.os.getpid", return_value=202),
            patch("rightmemory.semantic_operation.process_exists", return_value=True),
            patch("rightmemory.semantic_operation.process_identity", side_effect=identity),
        ):
            claimed = self.store.begin("operation-1", {"candidate": "one"})

        self.assertEqual(claimed.owner_pid, 202)
        self.assertEqual(claimed.owner_identity, "proc:second")
        self.assertEqual(claimed.failure, "worker exited")

    def test_pending_effects_are_hidden_until_outcome_is_final(self):
        self.store.begin("operation-1", {"candidate": "one"}, effects=(OperationEffect("publish"),))
        self.store.prepare_outcome(
            "operation-1",
            output="updated",
            start_commit="base123",
            changed_paths=("MEMORY.md",),
        )

        self.assertEqual(self.store.list_pending_effects("operation-1"), ())
        with self.assertRaisesRegex(OperationConflictError, "before a final outcome"):
            self.store.mark_effect("operation-1", "publish", "done")

    def test_effect_plan_is_frozen_after_prepare_and_completion(self):
        input_data = {"candidate": "one"}
        review = OperationEffect("review", metadata={"review_id": "review-1"})
        self.store.begin("operation-1", input_data)
        self.store.prepare_outcome(
            "operation-1",
            output="no change",
            start_commit="base123",
            changed_paths=(),
            effects=(review,),
        )

        with self.assertRaisesRegex(OperationConflictError, "plan is already frozen"):
            self.store.begin("operation-1", input_data, effects=(OperationEffect("publish"),))

        self.store.complete_no_change("operation-1")
        self.store.mark_effect("operation-1", "review", "done")
        with self.assertRaisesRegex(OperationConflictError, "plan is already frozen"):
            self.store.begin("operation-1", input_data, effects=(OperationEffect("publish"),))

    def test_schema_version_rejects_boolean_and_unknown_fields(self):
        self.store.begin("operation-1", {"candidate": "one"})
        path = self.store.record_path("operation-1")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["schema_version"] = True
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported semantic operation schema version"):
            self.store.read("operation-1")

        data["schema_version"] = 1
        data["unexpected"] = "field"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unexpected unexpected"):
            self.store.read("operation-1")

    def test_list_records_is_sorted_and_fails_closed_on_any_malformed_record(self):
        self.store.begin("operation-z", {"candidate": "last"})
        self.store.begin("operation-a", {"candidate": "first"})

        self.assertEqual(
            [record.operation_id for record in self.store.list_records()],
            ["operation-a", "operation-z"],
        )

        self.store.record_path("operation-z").write_text("{broken\n", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.store.list_records()

    def test_record_locks_serialize_same_id_without_blocking_different_ids(self):
        with self.store._locked("operation-1"):
            same_id, same_started, same_done = self._start_child_begin("operation-1")
            self._wait_for_path(same_started, same_id)
            time.sleep(0.1)
            self.assertFalse(same_done.exists())
        self._finish_child(same_id)
        self.assertTrue(same_done.is_file())

        with self.store._locked("operation-1"):
            other_id, _other_started, other_done = self._start_child_begin("operation-2")
            self._wait_for_path(other_done, other_id)
        self._finish_child(other_id)

    def _start_child_begin(self, operation_id: str):
        started = self.root / f"{operation_id}-started"
        done = self.root / f"{operation_id}-done"
        code = "\n".join(
            (
                "import sys",
                "from pathlib import Path",
                "from rightmemory.semantic_operation import SemanticOperationStore",
                "root = Path(sys.argv[1])",
                "operation_id = sys.argv[2]",
                "(root / f'{operation_id}-started').write_text('started', encoding='utf-8')",
                "SemanticOperationStore(root).begin(operation_id, {'candidate': operation_id})",
                "(root / f'{operation_id}-done').write_text('done', encoding='utf-8')",
            )
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(self.root), operation_id],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return process, started, done

    def _wait_for_path(self, path: Path, process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                return
            if process.poll() is not None:
                self._finish_child(process)
            time.sleep(0.02)
        process.kill()
        stdout, stderr = process.communicate()
        self.fail(f"child did not create {path.name}; stdout={stdout!r}; stderr={stderr!r}")

    def _finish_child(self, process: subprocess.Popen[str]) -> None:
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, f"child failed; stdout={stdout!r}; stderr={stderr!r}")


if __name__ == "__main__":
    unittest.main()
