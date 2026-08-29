import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from rightmemory.conversations import ConversationError
from rightmemory.conversations.store import (
    DATABASE_RELATIVE_PATH,
    MAX_EVENT_PAYLOAD_BYTES,
    ConversationStore,
)


class ConversationStoreTests(unittest.TestCase):
    def test_empty_initialization_creates_root_local_v1_and_defaults(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ConversationStore(root)

            initialized = store.initialize()

            self.assertEqual(store.db_path, root.resolve() / DATABASE_RELATIVE_PATH)
            self.assertTrue(store.db_path.is_file())
            self.assertEqual((root / ".runtime" / ".gitignore").read_text(encoding="utf-8"), "*\n")
            self.assertEqual(initialized["schema_version"], 1)
            self.assertEqual(initialized["local_host"]["host_id"], "local")
            self.assertEqual(initialized["local_host"]["kind"], "local")
            self.assertEqual(initialized["default_local_project"]["project_id"], "local-root")
            self.assertEqual(initialized["default_local_project"]["cwd"], str(root.resolve()))
            json.dumps(initialized)

            with closing(sqlite3.connect(store.db_path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                conversation_fks = connection.execute(
                    "PRAGMA foreign_key_list(pursuit_conversations)"
                ).fetchall()
                columns = {
                    table: {
                        row[1]
                        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                    }
                    for table in tables
                }

            self.assertEqual(version, 1)
            self.assertTrue(
                {
                    "conversation_hosts",
                    "conversation_projects",
                    "pursuit_conversations",
                    "pursuit_conversation_preferences",
                    "conversation_events",
                    "pending_server_requests",
                }.issubset(tables)
            )
            self.assertNotIn("pursuit_id", {row[3] for row in conversation_fks})
            self.assertTrue(all("root_id" not in names for names in columns.values()))
            forbidden_credentials = {"password", "passphrase", "private_key", "api_key", "token"}
            self.assertTrue(all(forbidden_credentials.isdisjoint(names) for names in columns.values()))

    def test_each_memory_root_has_an_isolated_database(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base = Path(tempdir)
            first_root = base / "first"
            second_root = base / "second"
            first_root.mkdir()
            second_root.mkdir()
            first = ConversationStore(first_root)
            second = ConversationStore(second_root)
            first.initialize()
            second.initialize()

            first.create_conversation(
                pursuit_id="P1",
                host_id="local",
                project_id="local-root",
                thread_id="thread-first",
            )

            self.assertEqual(len(first.list_conversations()), 1)
            self.assertEqual(second.list_conversations(), [])
            self.assertNotEqual(first.db_path, second.db_path)
            self.assertEqual(second.list_hosts(), [second.get_host("local")])

    def test_project_and_provider_thread_uniqueness_are_scoped_correctly(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            store.initialize()

            with self.assertRaises(ConversationError) as duplicate_project:
                store.create_project(host_id="local", cwd=str(Path(tempdir).resolve()), label="Duplicate")
            self.assertEqual(duplicate_project.exception.code, "project_conflict")
            self.assertEqual(duplicate_project.exception.status, 409)

            first = store.create_conversation(
                pursuit_id="P1",
                host_id="local",
                project_id="local-root",
                thread_id="thread-shared",
            )
            with self.assertRaises(ConversationError) as duplicate_thread:
                store.create_conversation(
                    pursuit_id="P2",
                    host_id="local",
                    project_id="local-root",
                    thread_id="thread-shared",
                )
            self.assertEqual(duplicate_thread.exception.code, "thread_already_attached")

            remote = store.upsert_host(
                kind="ssh",
                display_name="GPU server",
                host_id="gpu",
                ssh_alias="gpu",
            )
            project = store.create_project(
                host_id=remote["host_id"],
                cwd="/srv/project",
                label="Remote project",
            )
            second = store.create_conversation(
                pursuit_id="P2",
                host_id=remote["host_id"],
                project_id=project["project_id"],
                thread_id="thread-shared",
            )

            self.assertNotEqual(first["conversation_id"], second["conversation_id"])
            self.assertEqual(store.find_conversation("gpu", "thread-shared"), second)

    def test_unstarted_thread_rebind_is_stable_and_rejects_turn_history(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            conversation = self._create_local_conversation(store)

            rebound = store.rebind_unstarted_thread(
                conversation["conversation_id"],
                expected_thread_id=conversation["thread_id"],
                replacement_thread_id="replacement-thread",
                thread_title="Replacement",
            )

            self.assertEqual(rebound["conversation_id"], conversation["conversation_id"])
            self.assertEqual(rebound["thread_id"], "replacement-thread")
            self.assertEqual(rebound["thread_title"], "Replacement")
            self.assertIsNone(
                store.find_conversation("local", conversation["thread_id"])
            )
            self.assertEqual(
                store.find_conversation("local", "replacement-thread"), rebound
            )
            store.append_event(
                conversation_id=conversation["conversation_id"],
                turn_id="turn-1",
                kind="turn.started",
                payload={"turn": {"id": "turn-1"}},
            )
            self.assertTrue(store.has_turn_evidence(conversation["conversation_id"]))

            with self.assertRaises(ConversationError) as caught:
                store.rebind_unstarted_thread(
                    conversation["conversation_id"],
                    expected_thread_id="replacement-thread",
                    replacement_thread_id="unsafe-thread",
                )

            self.assertEqual(
                caught.exception.code, "conversation_has_turn_history"
            )
            persisted = store.get_conversation(conversation["conversation_id"])
            assert persisted is not None
            self.assertEqual(persisted["thread_id"], "replacement-thread")
            self.assertIsNone(store.find_conversation("local", "unsafe-thread"))

    def test_concurrent_stores_compare_and_swap_unstarted_thread_once(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = ConversationStore(root)
            conversation = self._create_local_conversation(first)
            second = ConversationStore(root)
            second.initialize()
            barrier = threading.Barrier(2)

            def replace(store: ConversationStore, thread_id: str) -> str:
                barrier.wait(timeout=3)
                try:
                    store.rebind_unstarted_thread(
                        conversation["conversation_id"],
                        expected_thread_id=conversation["thread_id"],
                        replacement_thread_id=thread_id,
                    )
                except ConversationError as exc:
                    return exc.code
                return "rebound"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = [
                    future.result(timeout=5)
                    for future in (
                        executor.submit(replace, first, "replacement-one"),
                        executor.submit(replace, second, "replacement-two"),
                    )
                ]

            self.assertCountEqual(outcomes, ["rebound", "thread_changed"])
            persisted = first.get_conversation(conversation["conversation_id"])
            assert persisted is not None
            self.assertIn(
                persisted["thread_id"], {"replacement-one", "replacement-two"}
            )

    def test_host_runtime_details_are_json_safe_and_multiline_errors_survive(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))

            updated = store.update_host_runtime(
                "local",
                capabilities={"platformFamily": "windows", "nested": {"available": True}},
                last_error="first line\nsecond line",
            )

            self.assertEqual(updated["last_error"], "first line\nsecond line")
            self.assertEqual(updated["capabilities"]["nested"], {"available": True})
            json.dumps(updated)

    def test_events_use_a_monotonic_cursor_and_reject_oversized_payloads(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            conversation = self._create_local_conversation(store)

            first = store.append_event(
                conversation_id=conversation["conversation_id"],
                turn_id="turn-1",
                kind="message.delta",
                payload={"text": "one"},
            )
            second = store.append_event(
                conversation_id=conversation["conversation_id"],
                turn_id="turn-1",
                kind="message.delta",
                payload={"text": "two"},
            )
            third = store.append_event(kind="host.connected", payload={"host_id": "local"})
            fourth = store.append_event(
                conversation_id=conversation["conversation_id"],
                kind="turn.completed",
                payload={"status": "completed"},
            )

            self.assertEqual(
                [first["event_id"], second["event_id"], third["event_id"], fourth["event_id"]],
                [1, 2, 3, 4],
            )
            self.assertEqual(
                [event["event_id"] for event in store.read_events(after_event_id=first["event_id"])],
                [2, 3, 4],
            )
            self.assertEqual(
                [event["event_id"] for event in store.read_events(
                    after_event_id=0,
                    conversation_id=conversation["conversation_id"],
                )],
                [1, 2, 4],
            )
            self.assertEqual(
                [event["event_id"] for event in store.latest_events(
                    conversation["conversation_id"], limit=2
                )],
                [2, 4],
            )
            self.assertEqual(store.latest_event_id(), 4)
            json.dumps(store.read_events())

            with self.assertRaises(ConversationError) as oversized:
                store.append_event(
                    kind="command.output",
                    payload={"text": "x" * MAX_EVENT_PAYLOAD_BYTES},
                )
            self.assertEqual(oversized.exception.code, "payload_too_large")
            self.assertEqual(oversized.exception.status, 413)

    def test_short_lived_connections_are_safe_for_parallel_event_writes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            conversation = self._create_local_conversation(store)

            def write_event(index: int) -> int:
                event = store.append_event(
                    conversation_id=conversation["conversation_id"],
                    kind="test.event",
                    payload={"index": index},
                )
                return event["event_id"]

            with ThreadPoolExecutor(max_workers=8) as executor:
                returned = list(executor.map(write_event, range(40)))

            events = store.read_events(limit=100)
            self.assertEqual(len(events), 40)
            self.assertEqual(len(set(returned)), 40)
            self.assertEqual([event["event_id"] for event in events], list(range(1, 41)))

    def test_missing_pursuit_does_not_remove_or_mutate_attachment(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ConversationStore(root)
            created = store.create_conversation(
                pursuit_id="P404",
                pursuit_title_snapshot="A direction that is temporarily unavailable",
                host_id="local",
                project_id="local-root",
                thread_id="thread-orphaned",
            )

            self.assertFalse((root / "PURSUITS.md").exists())
            reopened = ConversationStore(root).get_conversation(created["conversation_id"])

            self.assertEqual(reopened["pursuit_id"], "P404")
            self.assertEqual(
                reopened["pursuit_title_snapshot"],
                "A direction that is temporarily unavailable",
            )
            self.assertEqual(reopened["lifecycle"], "active")

    def test_conversation_requires_a_provider_confirmed_thread_id(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))

            with self.assertRaises(ConversationError) as missing_thread:
                store.create_conversation(
                    pursuit_id="P1",
                    host_id="local",
                    project_id="local-root",
                    thread_id="",
                )

            self.assertEqual(missing_thread.exception.code, "invalid_input")
            self.assertEqual(store.list_conversations(), [])

    def test_conversation_creation_and_explicit_choice_update_pursuit_default(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            self.assertIsNone(store.get_pursuit_default("P1"))

            store.create_conversation(
                pursuit_id="P1",
                host_id="local",
                project_id="local-root",
                thread_id="thread-1",
            )
            local_default = store.get_pursuit_default("P1")
            self.assertEqual(local_default["host_id"], "local")
            self.assertEqual(local_default["project_id"], "local-root")

            store.upsert_host(kind="ssh", display_name="Remote", host_id="remote", ssh_alias="remote")
            remote_project = store.create_project(host_id="remote", cwd="/work/repo", label="Repo")
            selected = store.set_pursuit_default("P1", "remote", remote_project["project_id"])

            self.assertEqual(store.get_pursuit_default("P1"), selected)
            self.assertEqual(selected["host_id"], "remote")
            touched = store.get_project(remote_project["project_id"])
            self.assertEqual(touched["last_used_at"], selected["last_used_at"])

    def test_pending_requests_are_bound_to_connection_epoch_and_single_resolution(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            conversation = self._create_local_conversation(store)
            arguments = {
                "host_id": "local",
                "connection_epoch": 1,
                "rpc_id": 7,
                "conversation_id": conversation["conversation_id"],
                "method": "item/commandExecution/requestApproval",
                "payload": {"command": "python -m tests"},
            }

            pending = store.create_pending_request(**arguments)
            self.assertEqual(pending["connection_epoch"], "1")
            self.assertEqual(pending["rpc_id"], 7)
            self.assertEqual(pending["thread_id"], conversation["thread_id"])
            self.assertEqual(pending["state"], "pending")
            with self.assertRaises(ConversationError) as duplicate:
                store.create_pending_request(**arguments)
            self.assertEqual(duplicate.exception.code, "duplicate_request")

            resolved = store.resolve_pending_request("local", 1, 7)
            self.assertEqual(resolved["state"], "resolved")
            self.assertEqual(resolved["payload"], {})
            self.assertIsNotNone(resolved["resolved_at"])
            with self.assertRaises(ConversationError) as duplicate_response:
                store.resolve_pending_request("local", 1, 7)
            self.assertEqual(duplicate_response.exception.code, "duplicate_response")

            stale_arguments = {**arguments, "rpc_id": "approval-8"}
            store.create_pending_request(**stale_arguments)
            self.assertEqual(store.mark_pending_requests_stale("local", 1), 1)
            with self.assertRaises(ConversationError) as stale:
                store.resolve_pending_request("local", 1, "approval-8")
            self.assertEqual(stale.exception.code, "stale_request")

            new_epoch = store.create_pending_request(
                **{**stale_arguments, "connection_epoch": "epoch-2"}
            )
            self.assertEqual(new_epoch["state"], "pending")
            self.assertEqual(store.get_pending_request_by_key(new_epoch["request_key"]), new_epoch)
            with self.assertRaises(ConversationError) as wrong_epoch:
                store.resolve_pending_request("local", "epoch-3", "approval-8")
            self.assertEqual(wrong_epoch.exception.code, "stale_request")
            with self.assertRaises(ConversationError) as wrong_key_epoch:
                store.resolve_pending_request_by_key(
                    new_epoch["request_key"],
                    host_id="local",
                    connection_epoch="epoch-3",
                )
            self.assertEqual(wrong_key_epoch.exception.code, "stale_request")
            resolved_by_key = store.resolve_pending_request_by_key(
                new_epoch["request_key"],
                host_id="local",
                connection_epoch="epoch-2",
            )
            self.assertEqual(resolved_by_key["state"], "resolved")
            self.assertEqual(
                [request["request_key"] for request in store.list_pending_requests()],
                [],
            )
            json.dumps(store.list_pending_requests(state=None))

    def test_restart_stales_unanswered_requests_and_drops_their_payload(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ConversationStore(root)
            conversation = self._create_local_conversation(store)
            pending = store.create_pending_request(
                host_id="local",
                connection_epoch="old-process",
                rpc_id=11,
                method="item/commandExecution/requestApproval",
                payload={"command": "sensitive command context"},
                conversation_id=conversation["conversation_id"],
            )

            recovered = ConversationStore(root).initialize()
            stale = store.get_pending_request_by_key(pending["request_key"])

            self.assertEqual(recovered["recovered_stale_request_count"], 1)
            self.assertEqual(store.list_pending_requests(), [])
            self.assertEqual(stale["state"], "stale")
            self.assertEqual(stale["payload"], {})

    def test_conversation_request_staling_can_target_one_turn_or_all_turns(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            conversation = self._create_local_conversation(store)
            first = store.create_pending_request(
                host_id="local",
                connection_epoch="epoch-1",
                rpc_id=21,
                method="item/commandExecution/requestApproval",
                payload={"turnId": "turn-1", "command": "first"},
                conversation_id=conversation["conversation_id"],
            )
            second = store.create_pending_request(
                host_id="local",
                connection_epoch="epoch-1",
                rpc_id=22,
                method="item/fileChange/requestApproval",
                payload={"turnId": "turn-2", "path": "second"},
                conversation_id=conversation["conversation_id"],
            )

            stale_first = store.mark_conversation_requests_stale(
                conversation["conversation_id"], turn_id="turn-1"
            )
            self.assertEqual([request["request_key"] for request in stale_first], [first["request_key"]])
            self.assertEqual(store.get_pending_request_by_key(first["request_key"])["state"], "stale")
            self.assertEqual(store.get_pending_request_by_key(second["request_key"])["state"], "pending")

            stale_rest = store.mark_conversation_requests_stale(conversation["conversation_id"])
            self.assertEqual([request["request_key"] for request in stale_rest], [second["request_key"]])
            self.assertEqual(store.get_pending_request_by_key(second["request_key"])["state"], "stale")

    def test_pending_request_persists_explicit_normalized_turn_identity(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            conversation = self._create_local_conversation(store)
            pending = store.create_pending_request(
                host_id="local",
                connection_epoch="epoch-nested",
                rpc_id=23,
                method="item/commandExecution/requestApproval",
                payload={"turn": {"id": "turn-nested"}, "command": "nested"},
                conversation_id=conversation["conversation_id"],
                turn_id="turn-nested",
            )
            self.assertEqual(pending["payload"]["turnId"], "turn-nested")
            self.assertEqual(pending["payload"]["turn"]["id"], "turn-nested")

            with self.assertRaises(ConversationError) as mismatch:
                store.create_pending_request(
                    host_id="local",
                    connection_epoch="epoch-nested",
                    rpc_id=24,
                    method="item/fileChange/requestApproval",
                    payload={"turnId": "turn-other"},
                    conversation_id=conversation["conversation_id"],
                    turn_id="turn-nested",
                )
            self.assertEqual(mismatch.exception.code, "invalid_request_binding")

    def test_ssh_alias_admission_matches_transport_safe_shape(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))

            accepted = store.upsert_host(
                kind="ssh",
                display_name="Build server",
                ssh_alias="build-server_2.example",
            )
            self.assertEqual(accepted["ssh_alias"], "build-server_2.example")
            for alias in ("-option", "user@host", "host:22", "a" * 129):
                with self.subTest(alias=alias), self.assertRaises(ConversationError):
                    store.upsert_host(kind="ssh", display_name="Rejected", ssh_alias=alias)

    @staticmethod
    def _create_local_conversation(store: ConversationStore) -> dict:
        return store.create_conversation(
            pursuit_id="P1",
            pursuit_title_snapshot="Direction",
            host_id="local",
            project_id="local-root",
            thread_id="thread-1",
        )


if __name__ == "__main__":
    unittest.main()
