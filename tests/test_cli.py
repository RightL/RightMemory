import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.async_update import AsyncUpdateState, AsyncUpdateStore
from rightmemory.cli import _daemon_stdio_json, _dreamer_watch_once, _handle_json_request, _insight_watch_once, cli_main, main
from rightmemory.config import DreamerWatchConfig, InsightWatchConfig
from rightmemory.dreamer_trigger import DreamerTriggerStore
from rightmemory.doctor import DoctorCheck
from rightmemory.hub.store import HubStore
from rightmemory.insight_trigger import InsightTriggerStore
from rightmemory.shared_view_files import FileViewPullResult
from rightmemory.shared_view_models import SharedViewConnection, SharedViewTarget, load_shared_view_credential, save_connections
from rightmemory.watch import MANAGED_WATCH_TARGETS, WATCH_COMMANDS, _process_command


class FakeRuntime:
    def __init__(self, config=None):
        self.config = config
        self.session_turns = []

    def run_turn(self, message: str) -> str:
        return f"handled: {message}"

    def run_session_turn(self, session_id: str, message: str) -> str:
        self.session_turns.append((session_id, message))
        return f"session {session_id}: {message}"

    def run_cycle(self, session_id: str, operator_hint=None) -> str:
        self.session_turns.append((session_id, operator_hint))
        return f"cycle {session_id}: {operator_hint}"

    def run_prune_turn(self, session_id: str, pruner_config) -> str:
        self.session_turns.append((session_id, f"prune:{pruner_config.memory_root}"))
        return f"prune session {session_id}: {pruner_config.memory_root}"

    def cleanup(self):
        pass


class CliEntrypointTests(unittest.TestCase):
    def test_cli_main_reports_expected_errors_without_traceback(self):
        stderr = io.StringIO()

        with patch("rightmemory.cli.main", side_effect=ValueError("hub request failed: HTTP 404")), patch("sys.stderr", stderr):
            result = cli_main(["shared-view", "accept-invite", "https://hub.example.test/i/revoked"])

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), "error: hub request failed: HTTP 404\n")
        self.assertNotIn("Traceback", stderr.getvalue())


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
        publish.assert_called_once_with(root, "auth-api", label="frontend", expires_at=None)
        self.assertIn("https://hub/i/share/token", stdout.getvalue())

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
    review_session_points: float = 1.5,
    check_interval_seconds: int = 3000,
):
    return DreamerWatchConfig(
        memory_root=Path("/unused") if memory_root is None else memory_root,
        trigger_points=trigger_points,
        update_candidate_points=update_candidate_points,
        review_session_points=review_session_points,
        check_interval_seconds=check_interval_seconds,
    )


def _insight_watch_config(
    memory_root: Path | None = None,
    trigger_points: float = 150.0,
    update_candidate_points: float = 1.0,
    review_session_points: float = 1.5,
    check_interval_seconds: int = 3000,
):
    return InsightWatchConfig(
        memory_root=Path("/unused") if memory_root is None else memory_root,
        trigger_points=trigger_points,
        update_candidate_points=update_candidate_points,
        review_session_points=review_session_points,
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
                f'[profiles.alpha]\nroot = "{profile_root}"\n',
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
                f'[profiles.alpha]\nroot = "{profile_root}"\n',
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
            imported = root / ".runtime" / "shared_views" / "imports" / "auth-api-files" / "dist"
            imported.mkdir(parents=True)
            (imported / "MEMORY.md").write_text("published context\n", encoding="utf-8")

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
                f'[profiles.alpha]\nroot = "{profile_root}"\n',
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
        self.assertIn("alpha\t/profiles/alpha", stdout.getvalue())

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
        self.assertEqual(stdout.getvalue().strip(), "prune session pruner: /memory")

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
        self.assertIn("prune session prune-1: /memory", stdout.getvalue())

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
            def run_prune_turn(self, session_id: str, pruner_config) -> str:
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

        class FailingRuntime(FakeRuntime):
            def run_prune_turn(self, session_id: str, pruner_config) -> str:
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
            def __init__(self, config, run_reviewer, *, on_review_success=None):
                self.config = config
                self.run_reviewer = run_reviewer
                self.on_review_success = on_review_success

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

    def test_review_scan_increments_dreamer_and_insight_triggers(self):
        stdout = io.StringIO()

        class FakeScanner:
            def __init__(self, config, run_reviewer, *, on_review_success=None):
                self.on_review_success = on_review_success

            def scan_once(self, *, require_full_batch=False):
                self.on_review_success(2)
                return FakeReviewResult("reviewed: 2", reviewed=2)

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            config = type("Config", (), {"memory_root": memory_root})()
            with (
                patch("rightmemory.cli.load_config", return_value=config),
                patch("rightmemory.cli.load_review_config", return_value=object()),
                patch(
                    "rightmemory.cli.load_dreamer_watch_config",
                    return_value=_dreamer_watch_config(memory_root=memory_root, review_session_points=1.5),
                ),
                patch(
                    "rightmemory.cli.load_insight_watch_config",
                    return_value=_insight_watch_config(memory_root=memory_root, review_session_points=1.5),
                ),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("rightmemory.cli.ReviewScanner", FakeScanner),
                patch("sys.stdout", stdout),
            ):
                result = main(["review", "scan", "--once"])

            trigger = DreamerTriggerStore(memory_root).read()
            insight_trigger = InsightTriggerStore(memory_root).read()

        self.assertEqual(result, 0)
        self.assertEqual(trigger.points, 3.0)
        self.assertEqual(insight_trigger.points, 3.0)
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
            def __init__(self, config, run_reviewer, *, on_review_success=None):
                self.config = config
                self.run_reviewer = run_reviewer
                self.on_review_success = on_review_success

            def scan_once(self, *, require_full_batch=False):
                scan_flags.append(require_full_batch)
                result = results.pop(0)
                if result.reviewed:
                    self.on_review_success(result.reviewed)
                return result

        def fake_load_config(role, **kwargs):
            roles.append(role)
            return type("Config", (), {"memory_root": memory_root})()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_review_config", return_value=object()),
                patch(
                    "rightmemory.cli.load_dreamer_watch_config",
                    return_value=_dreamer_watch_config(review_session_points=4.0),
                ),
                patch(
                    "rightmemory.cli.load_insight_watch_config",
                    return_value=_insight_watch_config(memory_root=memory_root, review_session_points=4.0),
                ),
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
        self.assertEqual(trigger.points, 4.0)
        self.assertIn("rightmemory review scan", stdout.getvalue())
        self.assertIn("reviewed: 1", stdout.getvalue())
        self.assertIn("reviewed: 0", stdout.getvalue())
        self.assertIn("rightmemory review watch stopped", stderr.getvalue())

    def test_review_watch_failed_scan_uses_retry_sleep(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        class FakeScanner:
            def __init__(self, config, run_reviewer, *, on_review_success=None):
                self.config = config
                self.run_reviewer = run_reviewer
                self.on_review_success = on_review_success

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
            def __init__(self, config, run_reviewer, *, on_review_success=None):
                self.config = config
                self.run_reviewer = run_reviewer
                self.on_review_success = on_review_success

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
            def __init__(self, config, run_reviewer, *, on_review_success=None):
                self.config = config
                self.run_reviewer = run_reviewer
                self.on_review_success = on_review_success

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

    def test_watch_start_starts_review_dreamer_pruner_and_insight_managed_processes(self):
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
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.IsolatedWriteSupervisor.cleanup_stale", return_value=None),
                patch(
                    "rightmemory.watch.subprocess.Popen",
                    side_effect=[FakeProcess(101), FakeProcess(102), FakeProcess(103), FakeProcess(104)],
                ) as popen,
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "start"])

            review_pid = (memory_root / ".runtime" / "watch" / "review.pid").read_text(encoding="utf-8")
            dreamer_pid = (memory_root / ".runtime" / "watch" / "dreamer.pid").read_text(encoding="utf-8")
            pruner_pid = (memory_root / ".runtime" / "watch" / "pruner.pid").read_text(encoding="utf-8")
            insight_pid = (memory_root / ".runtime" / "watch" / "insight.pid").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["reviewer", "dreamer", "pruner", "insight"])
        self.assertEqual(popen.call_count, 4)
        self.assertEqual(review_pid, "101\n")
        self.assertEqual(dreamer_pid, "102\n")
        self.assertEqual(pruner_pid, "103\n")
        self.assertEqual(insight_pid, "104\n")
        self.assertIn("review: running pid 101", stdout.getvalue())
        self.assertIn("dreamer: running pid 102", stdout.getvalue())
        self.assertIn("pruner: running pid 103", stdout.getvalue())
        self.assertIn("insight: running pid 104", stdout.getvalue())
        self.assertIn("sync: disabled", stdout.getvalue())

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
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.IsolatedWriteSupervisor.cleanup_stale", return_value=None),
                patch(
                    "rightmemory.watch.subprocess.Popen",
                    side_effect=[FakeProcess(101), FakeProcess(102), FakeProcess(103), FakeProcess(104), FakeProcess(105)],
                ) as popen,
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "start"])

            sync_pid = (memory_root / ".runtime" / "watch" / "sync.pid").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(popen.call_count, 5)
        self.assertEqual(sync_pid, "105\n")
        self.assertIn("sync: running pid 105", stdout.getvalue())

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
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.IsolatedWriteSupervisor.cleanup_stale", return_value=None),
                patch(
                    "rightmemory.watch.subprocess.Popen",
                    side_effect=[FakeProcess(101), FakeProcess(102), FakeProcess(103), FakeProcess(104)],
                ) as popen,
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "start"])

        self.assertEqual(result, 0)
        self.assertEqual(popen.call_count, 4)
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
                f'[profiles.alpha]\nroot = "{profile_root}"\n',
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
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.IsolatedWriteSupervisor.cleanup_stale", return_value=None),
                patch(
                    "rightmemory.watch.subprocess.Popen",
                    side_effect=[FakeProcess(201), FakeProcess(202), FakeProcess(203), FakeProcess(204)],
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
        self.assertEqual(roles, ["reviewer", "dreamer", "pruner", "insight"])
        self.assertEqual(popen.call_count, 4)
        self.assertEqual(dreamer_pid, "201\n")
        self.assertEqual(pruner_pid, "202\n")
        self.assertEqual(insight_pid, "203\n")
        self.assertEqual(sync_pid, "204\n")
        self.assertIn("review: error: RuntimeError: review unavailable", stderr.getvalue())
        self.assertIn("dreamer: running pid 201", stdout.getvalue())
        self.assertIn("pruner: running pid 202", stdout.getvalue())
        self.assertIn("insight: running pid 203", stdout.getvalue())
        self.assertIn("sync: running pid 204", stdout.getvalue())

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
                ("cleanup", "dreamer"),
                ("start", "dreamer"),
                ("cleanup", "pruner"),
                ("start", "prune"),
                ("cleanup", "insight"),
                ("start", "insight"),
                ("start", "sync"),
            ],
        )

    def test_sync_is_a_managed_watch_target(self):
        self.assertIn("sync", MANAGED_WATCH_TARGETS)
        self.assertEqual(WATCH_COMMANDS["sync"], ("sync", "watch"))

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
        self.assertIn("dreamer: stopped", stdout.getvalue())
        self.assertIn("pruner: stopped", stdout.getvalue())
        self.assertIn("insight: stopped", stdout.getvalue())
        self.assertIn("sync: stopped", stdout.getvalue())

    def test_watch_process_command_prefers_proc_cmdline(self):
        with patch("rightmemory.watch.Path.read_bytes", return_value=b"python\0-m\0rightmemory.cli\0review\0watch\0"):
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
        self.assertIn("review: running pid 123, log /memory/.runtime/watch/review.log", stdout.getvalue())
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
                manager_class.return_value.background_pull.return_value = result_obj
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(60)
        self.assertIn("rightmemory sync watch stopped", stderr.getvalue())

    def test_sync_watch_background_pull_runs_while_write_lock_is_held(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        events = []

        class FakeMemoryWriteLock:
            def __init__(self, memory_root):
                self.memory_root = memory_root

            def __enter__(self):
                events.append("lock_enter")
                return self

            def __exit__(self, exc_type, exc, traceback):
                events.append("lock_exit")

        with tempfile.TemporaryDirectory() as tempdir:
            sync_config = type("SyncConfig", (), {"memory_root": Path(tempdir), "enabled": True, "stale_pull_after_hours": 24})()
            result_obj = type("Result", (), {"status": "synced", "message": "local memory is current", "files": []})()

            def background_pull():
                events.append("background_pull")
                return result_obj

            with (
                patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli.MemoryWriteLock", FakeMemoryWriteLock),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.background_pull.side_effect = background_pull
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        self.assertEqual(events, ["lock_enter", "background_pull", "lock_exit"])

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
                manager_class.return_value.background_pull.side_effect = RuntimeError("boom")
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
                manager_class.return_value.background_pull.side_effect = RuntimeError("boom")
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
                manager_class.return_value.background_pull.return_value = result_obj
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        self.assertIn("rightmemory sync check", stdout.getvalue())
        self.assertIn("local memory is current", stdout.getvalue())

    def test_sync_watch_conflict_invokes_sync_reconciler(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls = []
        cleanup_calls = []
        events = []

        class FakeMemoryWriteLock:
            def __init__(self, memory_root):
                self.memory_root = memory_root

            def __enter__(self):
                events.append("lock_enter")
                return self

            def __exit__(self, exc_type, exc, traceback):
                events.append("lock_exit")

        class RecordingRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str) -> str:
                events.append("reconciler")
                calls.append((session_id, message))
                return "resolved"

            def cleanup(self):
                cleanup_calls.append("cleanup")

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            sync_config = type("SyncConfig", (), {"memory_root": memory_root, "enabled": True, "stale_pull_after_hours": 24})()
            reconciler_config = type("Config", (), {"memory_root": memory_root})()
            result_obj = type("Result", (), {"status": "conflict", "message": "conflict", "files": ["MEMORY.md"]})()

            def background_pull():
                events.append("background_pull")
                return result_obj

            with (
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli.load_config", return_value=reconciler_config) as load_config,
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli.MemoryWriteLock", FakeMemoryWriteLock),
                patch("rightmemory.cli.RightMemoryRuntime", RecordingRuntime),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.memory_root = memory_root
                manager_class.return_value.background_pull.side_effect = background_pull
                manager_class.return_value.repair_message.return_value = "resolve MEMORY.md"
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        load_config.assert_called_with("sync-reconciler", memory_root=memory_root)
        self.assertEqual(calls, [("sync-watch", "resolve MEMORY.md")])
        self.assertEqual(cleanup_calls, ["cleanup"])
        self.assertEqual(events, ["lock_enter", "background_pull", "lock_exit", "reconciler"])
        self.assertIn("resolved", stdout.getvalue())

    def test_sync_watch_reconciler_failure_logs_and_sleeps(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        cleanup_calls = []

        class FailingRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str) -> str:
                raise RuntimeError("boom")

            def cleanup(self):
                cleanup_calls.append("cleanup")

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            sync_config = type("SyncConfig", (), {"memory_root": memory_root, "enabled": True, "stale_pull_after_hours": 24})()
            reconciler_config = type("Config", (), {"memory_root": memory_root})()
            result_obj = type("Result", (), {"status": "conflict", "message": "conflict", "files": ["MEMORY.md"]})()

            with (
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli.load_config", return_value=reconciler_config),
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli.RightMemoryRuntime", FailingRuntime),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.memory_root = memory_root
                manager_class.return_value.background_pull.return_value = result_obj
                manager_class.return_value.repair_message.return_value = "resolve MEMORY.md"
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(60)
        self.assertEqual(cleanup_calls, ["cleanup"])
        self.assertIn("rightmemory sync reconciler failed: RuntimeError: boom", stderr.getvalue())

    def test_sync_watch_reconciler_root_mismatch_logs_and_sleeps(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir) / "memory"
            other_root = Path(tempdir) / "other"
            sync_config = type("SyncConfig", (), {"memory_root": memory_root, "enabled": True, "stale_pull_after_hours": 24})()
            reconciler_config = type("Config", (), {"memory_root": other_root})()
            result_obj = type("Result", (), {"status": "conflict", "message": "conflict", "files": ["MEMORY.md"]})()

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
                manager_class.return_value.background_pull.return_value = result_obj
                manager_class.return_value.repair_message.return_value = "resolve MEMORY.md"
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(60)
        self.assertIn("sync-reconciler memory root mismatch", stderr.getvalue())

    def test_watch_stop_sends_graceful_term_and_removes_pid(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            pid_path = memory_root / ".runtime" / "watch" / "dreamer.pid"
            pid_path.parent.mkdir(parents=True)
            pid_path.write_text("123\n", encoding="utf-8")
            with (
                patch("rightmemory.cli.default_memory_root", return_value=memory_root),
                patch("rightmemory.watch._is_managed_watch_process", side_effect=[True, False]),
                patch("rightmemory.watch.os.kill") as kill,
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "stop", "dreamer"])
            pid_exists = pid_path.exists()

        self.assertEqual(result, 0)
        kill.assert_called_once()
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
                    lambda session_id: calls.append(session_id) or "dream output",
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

            def run_cycle(session_id: str) -> str:
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

            def run_cycle(session_id: str) -> str:
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

            def run_cycle(session_id: str) -> str:
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

    def test_dreamer_watch_once_does_not_consume_on_failure(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            DreamerTriggerStore(memory_root).increment(12.0)
            watch_config = _dreamer_watch_config(memory_root=memory_root, trigger_points=10.0)

            def run_cycle(session_id: str) -> str:
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
            def run_cycle(self, session_id: str, operator_hint=None) -> str:
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
            def run_cycle(self, session_id: str, operator_hint=None) -> str:
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
            def run_cycle(self, session_id: str, operator_hint=None) -> str:
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
            def run_cycle(self, session_id: str, operator_hint=None) -> str:
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
        self.assertIn("CRITICAL: this async memory update session is blocked after 2 failed attempts.", output)
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
                patch("rightmemory.async_update._process_exists", return_value=True),
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
                patch("rightmemory.async_update._process_exists", return_value=True),
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

    def test_main_reports_non_pending_update_undo(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role, **kwargs):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
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
                patch("rightmemory.async_update._process_exists", return_value=True),
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
                patch("rightmemory.async_update._process_exists", return_value=True),
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
            def run_session_turn(self, session_id: str, message: str) -> str:
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
                patch("rightmemory.async_update._process_exists", return_value=True),
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
        self.assertIn("Process the following submitted memory update candidates as one batch.", calls[0][1])
        self.assertIn("[update session: agent-1 | candidate: 1", calls[0][1])
        self.assertIn("[update session: agent-2 | candidate: 1", calls[0][1])
        self.assertIn("status: succeeded", stdout.getvalue())
        self.assertIn("pending: 0", stdout.getvalue())
        self.assertIn("result: session update-batch-", stdout.getvalue())

    def test_async_worker_increments_dreamer_and_insight_trigger_points(self):
        class RecordingRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str) -> str:
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
                patch("rightmemory.async_update._process_exists", return_value=True),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", io.StringIO()),
            ):
                popen.return_value.pid = 123
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "first"]), 0)
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "second"]), 0)

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_async_update_config", return_value=_async_update_config(memory_root, target=1)),
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
        self.assertEqual(trigger.points, 5.0)
        self.assertEqual(insight_trigger.points, 5.0)

    def test_async_worker_warns_when_trigger_increment_fails_without_failing(self):
        class RecordingRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str) -> str:
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
                patch("rightmemory.async_update._process_exists", return_value=True),
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
                trigger_store.return_value.increment.side_effect = OSError("disk full")
                result = main(["update", "_async-worker"])

        self.assertEqual(result, 0)
        self.assertIn("Warning: could not update dreamer trigger state", stderr.getvalue())
        self.assertIn("disk full", stderr.getvalue())

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
