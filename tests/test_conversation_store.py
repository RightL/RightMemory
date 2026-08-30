import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path

from rightmemory.conversations import ConversationError
from rightmemory.conversations.store import (
    DATABASE_RELATIVE_PATH,
    MAX_EVENT_PAYLOAD_BYTES,
    ConversationStore,
)


class ConversationStoreTests(unittest.TestCase):
    def test_empty_initialization_creates_root_local_v5_and_defaults(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ConversationStore(root)

            initialized = store.initialize()

            self.assertEqual(store.db_path, root.resolve() / DATABASE_RELATIVE_PATH)
            self.assertTrue(store.db_path.is_file())
            self.assertEqual((root / ".runtime" / ".gitignore").read_text(encoding="utf-8"), "*\n")
            self.assertEqual(initialized["schema_version"], 5)
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

            self.assertEqual(version, 5)
            self.assertTrue(
                {
                    "conversation_hosts",
                    "conversation_projects",
                    "pursuit_conversations",
                    "pursuit_conversation_preferences",
                    "conversation_events",
                    "conversation_attachments",
                    "pending_server_requests",
                }.issubset(tables)
            )
            self.assertNotIn("pursuit_id", {row[3] for row in conversation_fks})
            self.assertTrue(
                {
                    "model",
                    "reasoning_effort",
                    "kind",
                    "parent_conversation_id",
                    "last_final_event_id",
                    "last_read_event_id",
                    "owner_session_id",
                }.issubset(columns["pursuit_conversations"])
            )
            self.assertIn("marks_final", columns["conversation_events"])
            self.assertTrue(all("root_id" not in names for names in columns.values()))
            forbidden_credentials = {"password", "passphrase", "private_key", "api_key", "token"}
            self.assertTrue(all(forbidden_credentials.isdisjoint(names) for names in columns.values()))

    def test_version_one_database_upgrades_in_place_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ConversationStore(root)
            store.initialize()
            legacy = store.create_conversation(
                pursuit_id="P1",
                host_id="local",
                project_id="local-root",
                thread_id="legacy-thread",
            )
            event = store.append_event(
                kind="user.message",
                payload={"text": "preserve me"},
                conversation_id=legacy["conversation_id"],
            )
            with closing(sqlite3.connect(store.db_path)) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    """
                    CREATE TABLE pursuit_conversations_v1(
                        conversation_id TEXT PRIMARY KEY,
                        pursuit_id TEXT NOT NULL,
                        pursuit_title_snapshot TEXT,
                        host_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        provider TEXT NOT NULL DEFAULT 'codex' CHECK(provider = 'codex'),
                        thread_id TEXT NOT NULL,
                        thread_title TEXT,
                        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active', 'archived')),
                        status TEXT NOT NULL CHECK(status IN (
                            'idle', 'starting', 'running', 'waiting_approval',
                            'waiting_input', 'completed', 'failed', 'interrupted',
                            'unknown'
                        )),
                        active_turn_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_activity_at TEXT NOT NULL,
                        UNIQUE(host_id, thread_id),
                        FOREIGN KEY(host_id)
                            REFERENCES conversation_hosts(host_id) ON DELETE RESTRICT,
                        FOREIGN KEY(project_id, host_id)
                            REFERENCES conversation_projects(project_id, host_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO pursuit_conversations_v1(
                        conversation_id, pursuit_id, pursuit_title_snapshot,
                        host_id, project_id, provider, thread_id, thread_title,
                        lifecycle, status, active_turn_id, created_at, updated_at,
                        last_activity_at
                    )
                    SELECT
                        conversation_id, pursuit_id, pursuit_title_snapshot,
                        host_id, project_id, provider, thread_id, thread_title,
                        lifecycle, status, active_turn_id, created_at, updated_at,
                        last_activity_at
                    FROM pursuit_conversations
                    """
                )
                connection.execute("DROP TABLE conversation_attachments")
                connection.execute(
                    "ALTER TABLE conversation_events DROP COLUMN marks_final"
                )
                connection.execute("DROP TABLE pursuit_conversations")
                connection.execute(
                    "ALTER TABLE pursuit_conversations_v1 RENAME TO pursuit_conversations"
                )
                connection.execute(
                    """
                    CREATE INDEX pursuit_conversations_by_pursuit
                    ON pursuit_conversations(pursuit_id, last_activity_at DESC)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX pursuit_conversations_by_host
                    ON pursuit_conversations(host_id, last_activity_at DESC)
                    """
                )
                connection.execute("PRAGMA user_version = 1")
                connection.commit()

            with ThreadPoolExecutor(max_workers=2) as executor:
                initializations = list(
                    executor.map(
                        lambda _: ConversationStore(root).initialize(), range(2)
                    )
                )
            initialized = initializations[0]
            upgraded = store.get_conversation(legacy["conversation_id"])
            with closing(sqlite3.connect(store.db_path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(pursuit_conversations)"
                    ).fetchall()
                }

            self.assertEqual(initialized["schema_version"], 5)
            self.assertEqual(version, 5)
            self.assertTrue(
                {
                    "model",
                    "reasoning_effort",
                    "kind",
                    "parent_conversation_id",
                    "last_final_event_id",
                    "last_read_event_id",
                    "owner_session_id",
                }.issubset(columns)
            )
            self.assertEqual(upgraded["thread_id"], "legacy-thread")
            self.assertEqual(upgraded["kind"], "pursuit")
            self.assertIsNone(upgraded["parent_conversation_id"])
            self.assertIsNone(upgraded["model"])
            self.assertIsNone(upgraded["reasoning_effort"])
            self.assertIsNone(upgraded["last_final_event_id"])
            self.assertIsNone(upgraded["last_read_event_id"])
            self.assertEqual(
                store.list_events(conversation_id=legacy["conversation_id"]), [event]
            )
            configured = store.update_conversation(
                legacy["conversation_id"],
                model="gpt-after-upgrade",
                reasoning_effort="medium",
            )
            self.assertEqual(configured["model"], "gpt-after-upgrade")
            self.assertEqual(configured["reasoning_effort"], "medium")

    def test_conversation_model_settings_persist_and_remain_nullable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            created = store.create_conversation(
                pursuit_id="P1",
                host_id="local",
                project_id="local-root",
                thread_id="thread-settings",
                model="gpt-example",
                reasoning_effort="high",
            )

            self.assertEqual(created["model"], "gpt-example")
            self.assertEqual(created["reasoning_effort"], "high")
            self.assertEqual(
                store.get_conversation(created["conversation_id"]), created
            )
            self.assertEqual(store.list_conversations(), [created])

            cleared = store.update_conversation(
                created["conversation_id"], model=None, reasoning_effort=None
            )
            self.assertIsNone(cleared["model"])
            self.assertIsNone(cleared["reasoning_effort"])

    def test_version_three_database_adds_side_chat_session_ownership(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ConversationStore(root)
            store.initialize()
            with closing(sqlite3.connect(store.db_path)) as connection:
                connection.execute(
                    "ALTER TABLE conversation_events DROP COLUMN marks_final"
                )
                connection.execute(
                    "ALTER TABLE pursuit_conversations DROP COLUMN owner_session_id"
                )
                connection.execute("PRAGMA user_version = 3")
                connection.commit()

            initialized = ConversationStore(root).initialize()
            with closing(sqlite3.connect(store.db_path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(pursuit_conversations)"
                    ).fetchall()
                }

            self.assertEqual(initialized["schema_version"], 5)
            self.assertEqual(version, 5)
            self.assertIn("owner_session_id", columns)

            parent = self._create_local_conversation(store)
            side_chat = store.create_conversation(
                pursuit_id=parent["pursuit_id"],
                host_id=parent["host_id"],
                project_id=parent["project_id"],
                thread_id="side-after-v3",
                kind="side_chat",
                parent_conversation_id=parent["conversation_id"],
                owner_session_id="session-after-v3",
            )
            self.assertTrue(
                store.side_chat_belongs_to_session(
                    side_chat["conversation_id"], "session-after-v3"
                )
            )

    def test_version_four_database_backfills_known_latest_final_marker(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ConversationStore(root)
            store.initialize()
            conversation = self._create_local_conversation(store)
            older = store.append_event(
                conversation_id=conversation["conversation_id"],
                kind="item.completed",
                payload={"item": {"type": "agentMessage", "phase": "final_answer"}},
                mark_final=True,
            )
            latest = store.append_event(
                conversation_id=conversation["conversation_id"],
                kind="item.completed",
                payload={"truncated": True},
                mark_final=True,
            )
            with closing(sqlite3.connect(store.db_path)) as connection:
                connection.execute(
                    "ALTER TABLE conversation_events DROP COLUMN marks_final"
                )
                connection.execute("PRAGMA user_version = 4")
                connection.commit()

            initialized = ConversationStore(root).initialize()
            events = store.read_events(
                conversation_id=conversation["conversation_id"]
            )

            self.assertEqual(initialized["schema_version"], 5)
            self.assertFalse(events[0]["marks_final"])
            self.assertEqual(events[0]["event_id"], older["event_id"])
            self.assertTrue(events[1]["marks_final"])
            self.assertEqual(events[1]["event_id"], latest["event_id"])

    def test_side_chats_are_filterable_and_cleanup_purges_events(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            parent = self._create_local_conversation(store)
            side_chat = store.create_conversation(
                pursuit_id=parent["pursuit_id"],
                pursuit_title_snapshot=parent["pursuit_title_snapshot"],
                host_id=parent["host_id"],
                project_id=parent["project_id"],
                thread_id="side-thread-1",
                kind="side_chat",
                parent_conversation_id=parent["conversation_id"],
                owner_session_id="session-a",
            )
            event = store.append_event(
                conversation_id=side_chat["conversation_id"],
                kind="assistant.message",
                payload={"text": "temporary"},
            )
            attachment = store.create_attachment(
                conversation_id=side_chat["conversation_id"],
                kind="pasted_text",
                display_name="Pasted text.txt",
                media_type="text/plain",
                byte_size=9,
                sha256="1" * 64,
                relative_path="side-chat/pasted-text.txt",
            )
            side_chat = store.get_conversation(side_chat["conversation_id"])
            assert side_chat is not None

            self.assertEqual(store.list_conversations(kind="pursuit"), [parent])
            self.assertEqual(
                store.list_conversations(kind="side_chat"), [side_chat]
            )
            self.assertEqual(store.list_side_chats(), [side_chat])
            self.assertEqual(
                store.list_side_chats(
                    parent_conversation_id=parent["conversation_id"]
                ),
                [side_chat],
            )

            self.assertTrue(store.delete_conversation(parent["conversation_id"]))
            orphan = store.get_conversation(side_chat["conversation_id"])
            assert orphan is not None
            self.assertIsNone(orphan["parent_conversation_id"])
            self.assertEqual(store.cleanup_side_chats(), 1)
            self.assertEqual(store.cleanup_side_chats(), 0)
            self.assertIsNone(store.get_attachment(attachment["attachment_id"]))
            self.assertNotIn(
                event["event_id"],
                {stored["event_id"] for stored in store.read_events()},
            )

    def test_event_reads_hide_side_chats_owned_by_other_sessions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            parent = self._create_local_conversation(store)
            side_a = store.create_conversation(
                pursuit_id=parent["pursuit_id"],
                host_id=parent["host_id"],
                project_id=parent["project_id"],
                thread_id="side-session-a",
                kind="side_chat",
                parent_conversation_id=parent["conversation_id"],
                owner_session_id="session-a",
            )
            side_b = store.create_conversation(
                pursuit_id=parent["pursuit_id"],
                host_id=parent["host_id"],
                project_id=parent["project_id"],
                thread_id="side-session-b",
                kind="side_chat",
                parent_conversation_id=parent["conversation_id"],
                owner_session_id="session-b",
            )
            root_event = store.append_event(kind="connection.disconnected", payload={})
            pursuit_event = store.append_event(
                conversation_id=parent["conversation_id"],
                kind="assistant.message",
                payload={"text": "pursuit"},
            )
            side_a_event = store.append_event(
                conversation_id=side_a["conversation_id"],
                kind="assistant.message",
                payload={"text": "private a"},
            )
            side_b_event = store.append_event(
                conversation_id=side_b["conversation_id"],
                kind="assistant.message",
                payload={"text": "private b"},
            )
            scoped_root_event = store.append_event(
                kind="side_chat.closed",
                payload={"conversation_id": side_a["conversation_id"]},
                owner_session_id="session-a",
            )

            visible_a = {
                event["event_id"]
                for event in store.read_events_for_session("session-a")
            }
            self.assertEqual(
                visible_a,
                {
                    root_event["event_id"],
                    pursuit_event["event_id"],
                    side_a_event["event_id"],
                    scoped_root_event["event_id"],
                },
            )
            self.assertNotIn(side_b_event["event_id"], visible_a)
            visible_b = store.read_events_for_session("session-b")
            self.assertNotIn(
                scoped_root_event["event_id"],
                {event["event_id"] for event in visible_b},
            )
            scoped_visible = next(
                event for event in store.read_events_for_session("session-a")
                if event["event_id"] == scoped_root_event["event_id"]
            )
            self.assertEqual(
                scoped_visible["payload"],
                {"conversation_id": side_a["conversation_id"]},
            )
            self.assertTrue(
                store.side_chat_belongs_to_session(
                    side_a["conversation_id"], "session-a"
                )
            )
            self.assertFalse(
                store.side_chat_belongs_to_session(
                    side_a["conversation_id"], "session-b"
                )
            )

    def test_final_event_and_unread_cursor_commit_atomically(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            conversation = self._create_local_conversation(store)
            cursor_before = store.latest_event_id()
            final_update_staged = threading.Event()
            allow_commit = threading.Event()
            original_connect = store._connect

            class PausingConnection:
                def __init__(self, connection):
                    self.connection = connection

                def execute(self, sql, parameters=()):
                    cursor = self.connection.execute(sql, parameters)
                    normalized = " ".join(sql.split())
                    if (
                        normalized.startswith("UPDATE pursuit_conversations")
                        and "last_final_event_id" in normalized
                    ):
                        final_update_staged.set()
                        if not allow_commit.wait(2):
                            raise AssertionError("timed out waiting to commit final event")
                    return cursor

                def __getattr__(self, name):
                    return getattr(self.connection, name)

            @contextmanager
            def pausing_connect():
                with original_connect() as connection:
                    yield PausingConnection(connection)

            store._connect = pausing_connect
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    store.append_event,
                    kind="item.completed",
                    payload={"item": {"type": "agentMessage", "phase": "final_answer"}},
                    conversation_id=conversation["conversation_id"],
                    mark_final=True,
                )
                self.assertTrue(final_update_staged.wait(2))
                try:
                    with closing(sqlite3.connect(store.db_path)) as reader:
                        visible_cursor = int(
                            reader.execute(
                                "SELECT COALESCE(MAX(event_id), 0) FROM conversation_events"
                            ).fetchone()[0]
                        )
                        visible_final = reader.execute(
                            "SELECT last_final_event_id FROM pursuit_conversations "
                            "WHERE conversation_id = ?",
                            (conversation["conversation_id"],),
                        ).fetchone()[0]
                    self.assertEqual(visible_cursor, cursor_before)
                    self.assertIsNone(visible_final)
                finally:
                    allow_commit.set()
                event = future.result(timeout=2)

            with closing(sqlite3.connect(store.db_path)) as reader:
                committed = reader.execute(
                    "SELECT last_final_event_id FROM pursuit_conversations "
                    "WHERE conversation_id = ?",
                    (conversation["conversation_id"],),
                ).fetchone()[0]
            self.assertEqual(committed, event["event_id"])
            self.assertTrue(event["marks_final"])

    def test_each_final_event_marker_survives_newer_finals_and_restart(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ConversationStore(root)
            conversation = self._create_local_conversation(store)
            first = store.append_event(
                conversation_id=conversation["conversation_id"],
                turn_id="turn-1",
                kind="item.completed",
                payload={"truncated": True, "summary": "first bounded final"},
                mark_final=True,
            )
            ordinary = store.append_event(
                conversation_id=conversation["conversation_id"],
                turn_id="turn-1",
                kind="turn.completed",
                payload={"turn": {"id": "turn-1", "status": "completed"}},
            )
            second = store.append_event(
                conversation_id=conversation["conversation_id"],
                turn_id="turn-2",
                kind="item.completed",
                payload={"truncated": True, "summary": "second bounded final"},
                mark_final=True,
            )
            expected = [first, ordinary, second]

            self.assertTrue(first["marks_final"])
            self.assertFalse(ordinary["marks_final"])
            self.assertTrue(second["marks_final"])
            self.assertEqual(
                store.get_conversation(conversation["conversation_id"])[
                    "last_final_event_id"
                ],
                second["event_id"],
            )
            self.assertEqual(
                store.read_events(conversation_id=conversation["conversation_id"]),
                expected,
            )
            self.assertEqual(
                store.latest_events(conversation["conversation_id"]), expected
            )
            self.assertEqual(
                store.read_events_for_session("browser-session"), expected
            )

            reopened = ConversationStore(root)
            reopened.initialize()
            self.assertEqual(
                reopened.read_events(
                    conversation_id=conversation["conversation_id"]
                ),
                expected,
            )
            self.assertEqual(
                reopened.latest_events(conversation["conversation_id"]), expected
            )

    def test_final_and_read_event_cursors_are_monotonic(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            conversation = self._create_local_conversation(store)
            first = store.append_event(
                conversation_id=conversation["conversation_id"],
                kind="assistant.message",
                payload={"text": "first"},
            )
            second = store.append_event(
                conversation_id=conversation["conversation_id"],
                kind="assistant.message",
                payload={"text": "second"},
            )

            marked = store.mark_final_event(
                conversation["conversation_id"], second["event_id"]
            )
            self.assertEqual(marked["last_final_event_id"], second["event_id"])
            self.assertIsNone(marked["last_read_event_id"])
            self.assertEqual(
                store.mark_final_event(
                    conversation["conversation_id"], first["event_id"]
                )["last_final_event_id"],
                second["event_id"],
            )
            marked_events = store.read_events(
                conversation_id=conversation["conversation_id"]
            )
            self.assertTrue(marked_events[0]["marks_final"])
            self.assertTrue(marked_events[1]["marks_final"])

            read = store.acknowledge_read(conversation["conversation_id"])
            self.assertEqual(read["last_read_event_id"], second["event_id"])
            self.assertEqual(
                store.acknowledge_read(
                    conversation["conversation_id"], first["event_id"]
                )["last_read_event_id"],
                second["event_id"],
            )
            later_non_final = store.append_event(
                conversation_id=conversation["conversation_id"],
                kind="agent.delta",
                payload={"delta": "later operational detail"},
            )
            with self.assertRaises(ConversationError) as not_final:
                store.acknowledge_read(
                    conversation["conversation_id"], later_non_final["event_id"]
                )
            self.assertEqual(not_final.exception.code, "event_not_final")
            self.assertEqual(
                store.get_conversation(conversation["conversation_id"])[
                    "last_read_event_id"
                ],
                second["event_id"],
            )

            other = store.create_conversation(
                pursuit_id="P2",
                host_id="local",
                project_id="local-root",
                thread_id="thread-2",
            )
            other_event = store.append_event(
                conversation_id=other["conversation_id"],
                kind="assistant.message",
                payload={"text": "other"},
            )
            with self.assertRaises(ConversationError) as mismatch:
                store.mark_final_event(
                    conversation["conversation_id"], other_event["event_id"]
                )
            self.assertEqual(
                mismatch.exception.code, "event_conversation_mismatch"
            )

    def test_event_projection_preserves_each_final_marker(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            conversation = self._create_local_conversation(store)
            ordinary = store.append_event(
                conversation_id=conversation["conversation_id"],
                kind="agent.delta",
                payload={"delta": "working"},
            )
            first_final = store.append_event(
                conversation_id=conversation["conversation_id"],
                kind="item.completed",
                payload={"truncated": True, "summary": "bounded final"},
                mark_final=True,
            )
            second_final = store.append_event(
                conversation_id=conversation["conversation_id"],
                kind="item.completed",
                payload={"truncated": True, "summary": "newer bounded final"},
                mark_final=True,
            )

            self.assertFalse(ordinary["marks_final"])
            self.assertTrue(first_final["marks_final"])
            self.assertTrue(second_final["marks_final"])
            replayed = {
                event["event_id"]: event
                for event in store.read_events(
                    conversation_id=conversation["conversation_id"]
                )
            }
            self.assertTrue(replayed[first_final["event_id"]]["marks_final"])
            self.assertTrue(replayed[second_final["event_id"]]["marks_final"])

            latest = {
                event["event_id"]: event
                for event in store.latest_events(conversation["conversation_id"])
            }
            self.assertTrue(latest[first_final["event_id"]]["marks_final"])
            self.assertTrue(latest[second_final["event_id"]]["marks_final"])

            session_replay = {
                event["event_id"]: event
                for event in store.read_events_for_session("browser-session")
            }
            self.assertFalse(session_replay[ordinary["event_id"]]["marks_final"])
            self.assertTrue(session_replay[first_final["event_id"]]["marks_final"])
            self.assertTrue(session_replay[second_final["event_id"]]["marks_final"])

    def test_state_mutations_publish_complete_conversation_summaries(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ConversationStore(Path(tempdir))
            conversation = self._create_local_conversation(store)
            final = store.append_event(
                conversation_id=conversation["conversation_id"],
                kind="item.completed",
                payload={"item": {"type": "agentMessage", "phase": "final_answer"}},
                mark_final=True,
            )

            read = store.acknowledge_read(
                conversation["conversation_id"],
                final["event_id"],
                emit_state_event=True,
            )
            configured = store.update_conversation(
                conversation["conversation_id"],
                model="gpt-example",
                reasoning_effort="high",
                emit_state_event=True,
            )

            states = [
                event
                for event in store.read_events(
                    conversation_id=conversation["conversation_id"]
                )
                if event["kind"] == "conversation.state"
            ]
            self.assertEqual(len(states), 2)
            self.assertEqual(
                states[0]["payload"]["conversation"]["last_read_event_id"],
                final["event_id"],
            )
            self.assertEqual(
                states[1]["payload"]["conversation"]["model"], "gpt-example"
            )
            self.assertEqual(
                states[1]["payload"]["conversation"]["reasoning_effort"],
                "high",
            )
            self.assertEqual(read["last_read_event_id"], final["event_id"])
            self.assertEqual(configured["model"], "gpt-example")

    def test_attachment_metadata_persists_updates_and_filters(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ConversationStore(root)
            conversation = self._create_local_conversation(store)
            image = store.create_attachment(
                attachment_id="attachment-image",
                conversation_id=conversation["conversation_id"],
                kind="image",
                display_name="diagram.png",
                media_type="image/png",
                byte_size=1234,
                sha256="a" * 64,
                relative_path="images\\diagram.png",
            )
            pasted = store.create_attachment(
                attachment_id="attachment-text",
                conversation_id=conversation["conversation_id"],
                kind="pasted_text",
                display_name="Pasted text.txt",
                media_type="text/plain; charset=utf-8",
                byte_size=4321,
                sha256="b" * 64,
                relative_path="pastes/pasted-text.txt",
            )

            self.assertEqual(image["relative_path"], "images/diagram.png")
            self.assertEqual(
                ConversationStore(root).get_attachment(image["attachment_id"]),
                image,
            )
            self.assertEqual(
                store.list_attachments(
                    conversation["conversation_id"], kind="pasted_text"
                ),
                [pasted],
            )
            sent = store.update_attachment(
                image["attachment_id"],
                remote_path="/remote/runtime/diagram.png",
                state="sent",
            )
            self.assertEqual(sent["state"], "sent")
            self.assertEqual(sent["remote_path"], "/remote/runtime/diagram.png")
            self.assertEqual(
                store.list_attachments(
                    conversation["conversation_id"], state="sent"
                ),
                [sent],
            )
            with self.assertRaises(ConversationError):
                store.create_attachment(
                    conversation_id=conversation["conversation_id"],
                    kind="image",
                    display_name="escape.png",
                    media_type="image/png",
                    byte_size=1,
                    sha256="c" * 64,
                    relative_path="../escape.png",
                )
            self.assertTrue(store.delete_attachment(pasted["attachment_id"]))
            self.assertFalse(store.delete_attachment(pasted["attachment_id"]))

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
            self.assertEqual(
                [event["event_id"] for event in store.read_events_before(
                    conversation["conversation_id"],
                    before_event_id=fourth["event_id"],
                    limit=2,
                )],
                [1, 2],
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
