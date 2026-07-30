import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from rightmemory.update_queue import (
    UpdateCandidate,
    UpdateQueueLease,
    UpdateQueueRecovery,
    UpdateQueueStore,
    update_candidate_batch_id,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_START = "rightmemory:example:start"
EXAMPLE_END = "rightmemory:example:end"
PURSUIT_EXAMPLE_START = "rightmemory:pursuit-example:start"
PURSUIT_EXAMPLE_END = "rightmemory:pursuit-example:end"
LEGACY_ORCHESTRATOR_TEMPLATE = """---
name: memory-orchestrator
description: "Use when the user's request may depend on long-term context from earlier sessions, or when the current turn may create long-term context worth preserving, such as durable user context, user preferences, project facts, workflow expectations, blockers, or repeated failure patterns."
---

# Memory Orchestrator CLI

## Access Rules

- The memory root is `{{MEMORY_ROOT}}`; the main agent must not read or edit files there by any means unless the user explicitly permits direct access.
- Pick one stable session id for this agent conversation and reuse it for every retrieve/update call.

## Retrieval

- For retrieval, call `rightmemory retrieve --session <stable-session-id> "<memory need>"`.
- Describe the memory needed based on the user's intent instead of blindly forwarding the user's message verbatim.
- For factual, project, or domain context, do not retrieve on every turn. Retrieve when the current conversation lacks the background needed to answer or work well.
- Skip this factual/context retrieval when the message is clearly self-contained and answerable from the conversation alone.
- For preference-, workflow-, and behavior-related memory, retrieve proactively and very frequently when the agent is about to make choices that affect how it collaborates, implements, verifies, communicates, or finishes work.
- Treat phase and topic changes as strong retrieval triggers for preference, workflow, and behavior memory, especially transitions between discussion, implementation, and finishing work.
- When running retrieve, give the actual retrieve command/session up to 3 minutes to return before acting without memory. This means awaiting or polling the tool result; do not run a separate blocking wait such as `sleep 180` after a successful retrieve. During the pending retrieve, do not explore files or advance the task independently.
- The retriever skips items already returned in this session; ask explicitly if you need something again.
- A returned `S#...` heading is a memory skill: reusable instruction backed by a separate skill body, not an ordinary memory fact.
- Broad retrieval usually returns only the skill heading and brief body paragraph.
- Before using a memory skill, retrieve that specific skill again to get its full body.
- Treat retrieved behavior guidance and memory skills seriously: apply them directly when the fit is clear, briefly say how they will guide the work when useful, and ask the user when the fit is unclear.
- If current work shows retrieved memory is stale, wrong, too broad, or misleading, send the correction in the next update brief. This matters because bad memory can keep steering future agents wrong.
- Retrieval may include an `Open context questions` block after ordinary memory matches. Treat those lines as agent-facing questions, not memory facts.
- If the current task or workspace context already answers one, include the question id and answer in the next memory update brief.
- Do not start extra investigation just because a question was surfaced.
- Retrieval may include `Provider question context` lines for relevant `MQ#` headings. Treat these as optional external ask opportunities, not memory facts.
- If provider-question context would materially help the current task, call `rightmemory shared-view ask <mq-id> "<question>"` yourself after retrieve returns.
- Phrase the question from the actual task context; do not forward a question invented by retrieve.
- If the ask reports unavailable, continue with available local context and tell the user the provider question endpoint is currently unavailable.

## Updates

- After completing work, judge whether this turn produced durable context that should change how a future agent acts, decides, retrieves context, or avoids repeating a mistake. If not, skip the update.
- Before submitting an update, check whether the same useful information is already durably captured in a natural artifact that future agents are likely to inspect, such as a git commit message, design doc, code comment, experiment report, run log, or project-local notes.
- If a natural artifact already captures the useful information, skip the memory update unless memory adds retrieval value that the artifact alone does not provide.
- For recurring project artifacts, prefer one compact lookup rule over repeated updates. For example, remember that future agents should inspect the local experiment log/report directory when they need run details, rather than remembering every new experiment report path.
- If a user context, preference, workflow, or behavior update may be durable but is uncertain, submit it as a candidate brief with the uncertainty and surrounding context included. The command-backed update role will triage candidate briefs before editing memory.
- Submit an update when previous work involved a significant amount of effort or reasoning, and reproducing that work later would take substantial effort.
- Memory-worthy context may include durable user context, user preferences, workflow expectations, emergent reusable workflows discovered through iteration, environment/tooling constraints, repeated agent failure patterns and their fixes, project facts, decisions, blockers, or domain working knowledge.
- Domain working knowledge is reusable understanding about a project, company, product, data model, terminology, conventions, or local artifact semantics that helps future agents interpret things correctly without rediscovering them.
- Capture domain working knowledge when remembering it would help future agents avoid rediscovering how to interpret the same kind of thing.
- For updates, call `rightmemory update submit --session <stable-session-id> "<concrete candidate brief>"` and proceed without waiting or pulling for the update result.
- The first update for a stable session id should include fuller surrounding context: meaning, relevance, uncertainty, and relationship to existing memory.
- Later updates with the same session id may be shorter when earlier submitted context or queued candidates are enough. Include fresh context when the meaning changed or depends on details not yet submitted.
- For corrections to retrieved memory, describe the stale or wrong memory well enough for the updater to find it, and say whether it should be revised, narrowed, or deleted.
- When the user asks for a memory-update result or status, call `rightmemory update pull --session <stable-session-id>`.
- To cancel a submitted update candidate that is still pending, call `rightmemory update undo --session <stable-session-id> <candidate-id>`.
"""


@unittest.skipIf(os.name == "nt", "install.sh tests exercise the POSIX installer")
class InstallScriptTests(unittest.TestCase):
    def _env_with_fake_uv(self, root: Path) -> dict[str, str]:
        fake_bin = root / "bin"
        fake_bin.mkdir(exist_ok=True)
        fake_uv = fake_bin / "uv"
        fake_uv.write_text(
            "#!/usr/bin/env sh\n"
            "if [ \"$1\" = \"python\" ] && [ \"$2\" = \"find\" ]; then\n"
            "  echo \"$RIGHTMEMORY_TEST_PYTHON\"\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = \"venv\" ]; then\n"
            "  target=''\n"
            "  for arg in \"$@\"; do target=\"$arg\"; done\n"
            "  mkdir -p \"$target/bin\"\n"
            "  cat > \"$target/bin/python\" <<'PYEOF'\n"
            "#!/usr/bin/env sh\n"
            "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"rightmemory.semantic_upgrades\" ]; then\n"
            "  command=\"$3\"\n"
            "  memory_root=''\n"
            "  previous=''\n"
            "  for arg in \"$@\"; do\n"
            "    if [ \"$previous\" = \"--memory-root\" ]; then memory_root=\"$arg\"; fi\n"
            "    previous=\"$arg\"\n"
            "  done\n"
            "  mkdir -p \"$memory_root/.runtime\"\n"
            "  state=\"$memory_root/.runtime/semantic-upgrades.json\"\n"
            "  if [ \"$command\" = \"baseline\" ]; then\n"
            "    echo '  [keep]    semantic upgrade baseline recorded for 3 current note(s):'\n"
            "    echo '            user-context-agent-behavior-split'\n"
            "    echo '            open-context-questions'\n"
            "    echo '            uncertain-memory-marker'\n"
            "    printf '{\"absorbed\":{\"user-context-agent-behavior-split\":{},\"open-context-questions\":{},\"uncertain-memory-marker\":{}}}\\n' > \"$state\"\n"
            "  elif grep -q 'user-context-agent-behavior-split' \"$state\" 2>/dev/null; then\n"
            "    echo '  [keep]    no semantic upgrade notes pending'\n"
            "  else\n"
            "    echo '  [notice]  3 semantic upgrade note(s) pending for the next dreamer cycle:'\n"
            "    echo '            user-context-agent-behavior-split'\n"
            "    echo '            open-context-questions'\n"
            "    echo '            uncertain-memory-marker'\n"
            "    printf '{\"absorbed\": {}}\\n' > \"$state\"\n"
            "  fi\n"
            "fi\n"
            "exit 0\n"
            "PYEOF\n"
            "  chmod 755 \"$target/bin/python\"\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)
        return {
            **os.environ,
            "HOME": str(root / "home"),
            "XDG_DATA_HOME": str(root / "data"),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "RIGHTMEMORY_TEST_PYTHON": sys.executable,
        }

    def _env_with_fake_git_no_uv(self, root: Path) -> dict[str, str]:
        fake_bin = root / "bin"
        fake_bin.mkdir(exist_ok=True)
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_git.chmod(0o755)
        return {
            **os.environ,
            "HOME": str(root / "home"),
            "XDG_DATA_HOME": str(root / "data"),
            "PATH": f"{fake_bin}:/bin",
        }

    def _env_with_failing_uv_python(self, root: Path) -> dict[str, str]:
        fake_bin = root / "bin"
        fake_bin.mkdir(exist_ok=True)
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_git.chmod(0o755)
        fake_uv = fake_bin / "uv"
        fake_uv.write_text(
            "#!/usr/bin/env sh\n"
            "if [ \"$1\" = \"python\" ] && [ \"$2\" = \"find\" ]; then\n"
            "  echo 'no Python 3.11 available' >&2\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)
        return {
            **os.environ,
            "HOME": str(root / "home"),
            "XDG_DATA_HOME": str(root / "data"),
            "PATH": f"{fake_bin}:/bin",
        }

    def test_initial_install_copies_managed_example(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            self._install(memory_root, skills_target)

            memory = (memory_root / "MEMORY.md").read_text(encoding="utf-8")
            pursuits = (memory_root / "PURSUITS.md").read_text(encoding="utf-8")
            state = (memory_root / ".runtime" / "semantic-upgrades.json").read_text(encoding="utf-8")
            install_stamp_exists = (memory_root / ".runtime" / "install.stamp").exists()
            insight_logs_exists = (memory_root / "insight_logs").is_dir()
            dream_logs_exists = (memory_root / "dream_logs").exists()
            pursuit_rules_exists = (memory_root / "PURSUIT_RULES.md").exists()
            correction_rules_exists = (memory_root / "AGENT_CORRECTION_MEMORY_RULES.md").exists()

        self.assertIn(EXAMPLE_START, memory)
        self.assertIn(EXAMPLE_END, memory)
        self.assertIn("# Open Context Questions {#open-context-questions}", memory)
        self.assertIn("q-rightmemory-project-context", memory)
        self.assertIn(PURSUIT_EXAMPLE_START, pursuits)
        self.assertIn(PURSUIT_EXAMPLE_END, pursuits)
        self.assertFalse(pursuit_rules_exists)
        self.assertFalse(correction_rules_exists)
        self.assertIn("user-context-agent-behavior-split", state)
        self.assertIn("open-context-questions", state)
        self.assertTrue(install_stamp_exists)
        self.assertTrue(insight_logs_exists)
        self.assertFalse(dream_logs_exists)

    def test_initial_install_configures_git_author_and_baseline_commit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = self._install(memory_root, skills_target)
            git_name = self._git(memory_root, "config", "--local", "--get", "user.name")
            git_email = self._git(memory_root, "config", "--local", "--get", "user.email")
            head = self._git(memory_root, "log", "--oneline", "-1")
            status = self._git(memory_root, "status", "--short")

        self.assertEqual(git_name, "RightMemory")
        self.assertEqual(git_email, "rightmemory@localhost")
        self.assertIn("memory: initial baseline", head)
        self.assertEqual(status, "")
        self.assertIn("git user.name = RightMemory", result.stdout)
        self.assertIn("initial memory baseline commit", result.stdout)

    def test_empty_git_repository_without_head_is_bootstrapped_as_new(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=memory_root, check=True)

            result = self._install(memory_root, skills_target)

            self.assertTrue((memory_root / "MEMORY.md").is_file())
            self.assertTrue((memory_root / "PURSUITS.md").is_file())
            self.assertFalse((memory_root / "PURSUIT_RULES.md").exists())
            self.assertFalse((memory_root / "AGENT_CORRECTION_MEMORY_RULES.md").exists())
            self.assertIn("memory: initial baseline", self._git(memory_root, "log", "--oneline", "-1"))
            self.assertEqual(self._git(memory_root, "status", "--short"), "")
            self.assertIn("from MEMORY.example.md", result.stdout)

    def test_complete_non_git_root_is_preserved_and_baseline_committed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            expected = {
                "MEMORY.md": b"# Existing Memory\n\xff\x00",
                "PURSUITS.md": b"# Existing Pursuits\r\n",
            }
            for name, content in expected.items():
                (memory_root / name).write_bytes(content)

            self._install(memory_root, skills_target)

            self.assertEqual({name: (memory_root / name).read_bytes() for name in expected}, expected)
            self.assertIn("memory: initial baseline", self._git(memory_root, "log", "--oneline", "-1"))
            self.assertEqual(self._git(memory_root, "status", "--short"), "")

    def test_initial_install_baselines_existing_shared_view_registry(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            (memory_root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
            (memory_root / "shared_views.toml").write_text(
                '[connections.alice-auth-api]\ntype = "file"\nref = "rightmemory://mf/current"\n',
                encoding="utf-8",
            )

            self._install(memory_root, skills_target)
            status = self._git(memory_root, "status", "--short")
            committed_files = self._git(memory_root, "ls-tree", "--name-only", "-r", "HEAD").splitlines()

        self.assertEqual(status, "")
        self.assertIn("shared_views.toml", committed_files)

    def test_initial_install_baselines_existing_share_registry(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            (memory_root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
            (memory_root / "shares.toml").write_text(
                '[shares.auth-api]\n'
                'version = 1\n'
                'role = "provider"\n'
                'title = "Auth API"\n'
                'state = "draft"\n'
                'parts = ["file"]\n'
                '[shares.auth-api.file]\n'
                'view_id = "auth-api-files"\n'
                'intent = "Expose auth context."\n'
                'approved = false\n',
                encoding="utf-8",
            )

            self._install(memory_root, skills_target)
            status = self._git(memory_root, "status", "--short")
            committed_files = self._git(memory_root, "ls-tree", "--name-only", "-r", "HEAD").splitlines()

        self.assertEqual(status, "")
        self.assertIn("shares.toml", committed_files)

    def test_initial_install_baselines_existing_update_queue_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            (memory_root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
            candidate_uid = "a" * 32
            timestamp = datetime.now(UTC).isoformat()
            queue = UpdateQueueStore(memory_root)
            candidate_record = UpdateCandidate(
                candidate_uid,
                "session",
                1,
                "remember this",
                timestamp,
            )
            batch_id = update_candidate_batch_id((candidate_record,))
            candidate = queue.write_candidate(candidate_record)
            recovery = queue.write_recovery(
                UpdateQueueRecovery(batch_id, (candidate_uid,), 1, "retry", timestamp)
            )
            lease = queue.write_lease(
                UpdateQueueLease("c" * 32, "d" * 32, "e" * 40, batch_id, (candidate_uid,), timestamp)
            )

            self._install(memory_root, skills_target)
            committed_files = self._git(memory_root, "ls-tree", "--name-only", "-r", "HEAD").splitlines()

        self.assertIn(candidate.relative_to(memory_root).as_posix(), committed_files)
        self.assertIn(recovery.relative_to(memory_root).as_posix(), committed_files)
        self.assertIn(lease.relative_to(memory_root).as_posix(), committed_files)

    def test_install_refuses_malformed_existing_update_queue_without_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            (memory_root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
            candidate = memory_root / "update_queue" / "candidates" / f"{'a' * 32}.json"
            candidate.parent.mkdir(parents=True)
            candidate.write_text("{not json\n", encoding="utf-8")

            result = self._run_install(memory_root, skills_target, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RightMemory update queue is invalid", result.stderr)
        self.assertIn(candidate.relative_to(memory_root).as_posix(), result.stderr)
        self.assertFalse((memory_root / ".git").exists())

    def test_queue_only_root_is_refused_without_bootstrap(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            lease = memory_root / "update_queue" / "lease.json"
            lease.parent.mkdir(parents=True)
            lease.write_text("{}\n", encoding="utf-8")

            result = self._run_install(memory_root, skills_target, check=False)
            memory_created = (memory_root / "MEMORY.md").exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RightMemory update queue is invalid", result.stderr)
        self.assertFalse(memory_created)

    def test_install_validates_malformed_queue_before_new_target_bootstrap(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            queue_path = memory_root / "update_queue"
            queue_path.write_text("not a queue directory\n", encoding="utf-8")

            result = self._run_install(memory_root, skills_target, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RightMemory update queue is invalid", result.stderr)
        self.assertIn("update_queue: must be a directory", result.stderr)
        self.assertFalse((memory_root / "MEMORY.md").exists())
        self.assertFalse((memory_root / ".git").exists())

    def test_install_refuses_live_legacy_async_updates_before_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            state_path = memory_root / ".runtime" / "async" / "update" / "legacy-session.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "status": "running",
                        "session_id": "legacy-session",
                        "role": "update",
                        "phase": "waiting",
                        "current_batch": [],
                        "pending": [
                            {
                                "id": 1,
                                "message": "legacy pending evidence",
                                "submitted_at": "2026-07-20T00:00:00+00:00",
                            }
                        ],
                        "next_id": 2,
                    }
                ),
                encoding="utf-8",
            )

            result = self._run_install(memory_root, skills_target, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("live async updates are incompatible", result.stderr)
        self.assertIn(state_path.relative_to(memory_root).as_posix(), result.stderr)
        self.assertIn("finish, retry, or undo every live update", result.stderr)
        self.assertIn("`pending: 0`", result.stderr)
        self.assertIn("`current_batch: 0`", result.stderr)
        self.assertFalse((memory_root / ".git").exists())
        self.assertFalse(skills_target.exists())

    def test_install_allows_drained_legacy_async_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            state_path = memory_root / ".runtime" / "async" / "update" / "drained-session.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "status": "succeeded",
                        "session_id": "drained-session",
                        "role": "update",
                        "current_batch": [],
                        "pending": [],
                        "next_id": 2,
                    }
                ),
                encoding="utf-8",
            )

            result = self._run_install(memory_root, skills_target, check=False)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_refuses_non_directory_async_root_before_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            async_root = memory_root / ".runtime" / "async" / "update"
            async_root.parent.mkdir(parents=True)
            async_root.write_text("not a directory\n", encoding="utf-8")

            result = self._run_install(memory_root, skills_target, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("live async updates are incompatible", result.stderr)
        self.assertIn(async_root.relative_to(memory_root).as_posix(), result.stderr)
        self.assertFalse((memory_root / ".git").exists())
        self.assertFalse(skills_target.exists())

    def test_install_refuses_unsafe_runtime_state_parent_containers(self):
        cases = (
            ("async-file", Path(".runtime/async"), "file", "live async updates"),
            ("async-symlink", Path(".runtime/async"), "symlink", "live async updates"),
            ("review-file", Path(".runtime/review"), "file", "pending transcript-review"),
            ("review-symlink", Path(".runtime/review"), "symlink", "pending transcript-review"),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            for name, relative, kind, diagnostic in cases:
                with self.subTest(name=name):
                    memory_root = root / name / "memory"
                    skills_target = root / name / "skills"
                    memory_root.mkdir(parents=True)
                    for required in ("MEMORY.md", "PURSUITS.md"):
                        (memory_root / required).write_text(f"# {required}\n", encoding="utf-8")
                    container = memory_root / relative
                    container.parent.mkdir(parents=True)
                    if kind == "file":
                        container.write_text("not a directory\n", encoding="utf-8")
                    else:
                        external = root / name / "external"
                        external.mkdir()
                        container.symlink_to(external, target_is_directory=True)

                    result = self._run_install(memory_root, skills_target, check=False)

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(diagnostic, result.stderr)
                    self.assertIn(relative.as_posix(), result.stderr)
                    self.assertFalse(skills_target.exists())

    def test_install_refuses_drained_unsupported_async_state_before_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            state_path = memory_root / ".runtime" / "async" / "update" / "drained-session.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "status": "succeeded",
                        "session_id": "drained-session",
                        "role": "update",
                        "current": None,
                        "queued": [],
                        "next_id": 2,
                    }
                ),
                encoding="utf-8",
            )

            result = self._run_install(memory_root, skills_target, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("live async updates are incompatible", result.stderr)
        self.assertIn(state_path.relative_to(memory_root).as_posix(), result.stderr)
        self.assertIn("archive any listed drained legacy state", result.stderr)
        self.assertFalse((memory_root / ".git").exists())
        self.assertFalse(skills_target.exists())

    def test_install_refuses_legacy_async_batch_reservation_before_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            reservation = (
                memory_root
                / ".runtime"
                / "async"
                / "update"
                / "_batches"
                / ("a" * 64 + ".json")
            )
            reservation.parent.mkdir(parents=True)
            reservation.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "operation_id": "update-batch-" + "a" * 64,
                        "created_at": "2026-07-20T00:00:00+00:00",
                        "participants": [
                            {
                                "session_id": "legacy-session",
                                "ready_at": "2026-07-20T00:00:00+00:00",
                                "jobs": [
                                    {
                                        "id": 1,
                                        "message": "legacy reserved evidence",
                                        "submitted_at": "2026-07-20T00:00:00+00:00",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = self._run_install(memory_root, skills_target, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("live async updates are incompatible", result.stderr)
        self.assertIn(reservation.relative_to(memory_root).as_posix(), result.stderr)
        self.assertFalse((memory_root / ".git").exists())
        self.assertFalse(skills_target.exists())

    def test_install_refuses_legacy_pending_review_delivery_before_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            delivery = memory_root / ".runtime" / "review" / "deliveries" / "legacy.json"
            delivery.parent.mkdir(parents=True)
            delivery.write_text(
                json.dumps({"version": 1, "batch_id": "legacy"}),
                encoding="utf-8",
            )

            result = self._run_install(memory_root, skills_target, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pending transcript-review deliveries are incompatible", result.stderr)
        self.assertIn(delivery.relative_to(memory_root).as_posix(), result.stderr)
        self.assertFalse((memory_root / ".git").exists())
        self.assertFalse(skills_target.exists())

    def test_install_refuses_malformed_v2_review_delivery_before_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            batch_id = "review-batch"
            filename = hashlib.sha256(batch_id.encode("utf-8")).hexdigest() + ".json"
            delivery = memory_root / ".runtime" / "review" / "deliveries" / filename
            delivery.parent.mkdir(parents=True)
            delivery.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "batch_id": batch_id,
                        "candidate_uid": "a" * 32,
                    }
                ),
                encoding="utf-8",
            )

            result = self._run_install(memory_root, skills_target, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pending transcript-review deliveries are incompatible", result.stderr)
        self.assertIn(delivery.relative_to(memory_root).as_posix(), result.stderr)
        self.assertFalse((memory_root / ".git").exists())
        self.assertFalse(skills_target.exists())

    def test_install_accepts_valid_v2_review_delivery(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            batch_id = "review-batch"
            reviewed_at = "2026-07-20T00:00:00+00:00"
            filename = hashlib.sha256(batch_id.encode("utf-8")).hexdigest() + ".json"
            delivery = memory_root / ".runtime" / "review" / "deliveries" / filename
            delivery.parent.mkdir(parents=True)
            delivery.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "batch_id": batch_id,
                        "candidate": "durable review evidence",
                        "candidate_id": 1,
                        "candidate_uid": "a" * 32,
                        "reviewed_at": reviewed_at,
                        "sessions": [
                            {
                                "session_id": "session-1",
                                "source": "codex",
                                "last_reviewed_at": reviewed_at,
                            }
                        ],
                        "reviewed_count": 1,
                        "skipped_duplicate_count": 0,
                    }
                ),
                encoding="utf-8",
            )

            result = self._run_install(memory_root, skills_target, check=False)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_refuses_valid_review_delivery_under_wrong_filename(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            reviewed_at = "2026-07-20T00:00:00+00:00"
            delivery = memory_root / ".runtime" / "review" / "deliveries" / "wrong.json"
            delivery.parent.mkdir(parents=True)
            delivery.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "batch_id": "review-batch",
                        "candidate": "durable review evidence",
                        "candidate_id": 1,
                        "candidate_uid": "a" * 32,
                        "reviewed_at": reviewed_at,
                        "sessions": [
                            {
                                "session_id": "session-1",
                                "source": "codex",
                                "last_reviewed_at": reviewed_at,
                            }
                        ],
                        "reviewed_count": 1,
                        "skipped_duplicate_count": 0,
                    }
                ),
                encoding="utf-8",
            )

            result = self._run_install(memory_root, skills_target, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pending transcript-review deliveries are incompatible", result.stderr)
        self.assertIn(delivery.relative_to(memory_root).as_posix(), result.stderr)
        self.assertFalse(skills_target.exists())

    def test_install_refuses_symlink_review_delivery_root_before_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            external = root / "external-deliveries"
            external.mkdir()
            deliveries_root = memory_root / ".runtime" / "review" / "deliveries"
            deliveries_root.parent.mkdir(parents=True)
            deliveries_root.symlink_to(external, target_is_directory=True)

            result = self._run_install(memory_root, skills_target, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pending transcript-review deliveries are incompatible", result.stderr)
        self.assertIn(deliveries_root.relative_to(memory_root).as_posix(), result.stderr)
        self.assertFalse((memory_root / ".git").exists())
        self.assertFalse(skills_target.exists())

    def test_install_preserves_existing_memory_repo_author(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=memory_root, check=True)
            subprocess.run(["git", "config", "--local", "user.name", "Existing User"], cwd=memory_root, check=True)
            subprocess.run(["git", "config", "--local", "user.email", "existing@example.com"], cwd=memory_root, check=True)

            result = self._install(memory_root, skills_target)
            git_name = self._git(memory_root, "config", "--local", "--get", "user.name")
            git_email = self._git(memory_root, "config", "--local", "--get", "user.email")

        self.assertEqual(git_name, "Existing User")
        self.assertEqual(git_email, "existing@example.com")
        self.assertIn("git author configured", result.stdout)

    def test_install_refuses_incomplete_committed_root_without_mutation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            runtime_home = root / "data" / "rightmemory"
            runtime_command = root / "home" / ".local" / "bin" / "rightmemory"
            memory_root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=memory_root, check=True)
            subprocess.run(["git", "config", "--local", "user.name", "Existing User"], cwd=memory_root, check=True)
            subprocess.run(
                ["git", "config", "--local", "user.email", "existing@example.com"], cwd=memory_root, check=True
            )
            (memory_root / "MEMORY.md").write_text("# Existing Memory\n", encoding="utf-8")
            subprocess.run(["git", "add", "MEMORY.md"], cwd=memory_root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "memory: user baseline"], cwd=memory_root, check=True)
            runtime_dir = memory_root / ".runtime"
            runtime_dir.mkdir()
            (runtime_dir / "install.stamp").write_text("old stamp\n", encoding="utf-8")
            skills_target.mkdir()
            (skills_target / "keep.txt").write_text("skills stay\n", encoding="utf-8")
            runtime_home.mkdir(parents=True)
            (runtime_home / "keep.txt").write_text("runtime stays\n", encoding="utf-8")
            runtime_command.parent.mkdir(parents=True)
            runtime_command.write_text("old wrapper\n", encoding="utf-8")

            before_head = self._git(memory_root, "rev-parse", "HEAD")
            before_status = self._git(memory_root, "status", "--short")
            before_name = self._git(memory_root, "config", "--local", "--get", "user.name")
            before_email = self._git(memory_root, "config", "--local", "--get", "user.email")
            before_memory = self._snapshot(memory_root)
            before_skills = self._snapshot(skills_target)
            before_runtime = self._snapshot(runtime_home)
            before_wrapper = runtime_command.read_bytes()

            result = self._run_install(memory_root, skills_target, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("existing RightMemory root is incomplete", result.stderr)
            self.assertIn(
                "missing required files: PURSUITS.md",
                result.stderr,
            )
            self.assertIn("installation made no changes", result.stderr)
            self.assertIn("migrate and review this root explicitly", result.stderr)
            self.assertEqual(self._git(memory_root, "rev-parse", "HEAD"), before_head)
            self.assertEqual(self._git(memory_root, "status", "--short"), before_status)
            self.assertEqual(self._git(memory_root, "config", "--local", "--get", "user.name"), before_name)
            self.assertEqual(self._git(memory_root, "config", "--local", "--get", "user.email"), before_email)
            self.assertEqual(self._snapshot(memory_root), before_memory)
            self.assertEqual(self._snapshot(skills_target), before_skills)
            self.assertEqual(self._snapshot(runtime_home), before_runtime)
            self.assertEqual(runtime_command.read_bytes(), before_wrapper)

    def test_install_refuses_directory_and_symlink_required_paths_as_non_regular(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            cases = ("directory", "symlink")
            for case in cases:
                with self.subTest(case=case):
                    memory_root = root / case / "memory"
                    skills_target = root / case / "skills"
                    memory_root.mkdir(parents=True)
                    (memory_root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
                    pursuits = memory_root / "PURSUITS.md"
                    if case == "directory":
                        pursuits.mkdir()
                    else:
                        target = memory_root / "pursuits-target.md"
                        target.write_text("# Pursuits\n", encoding="utf-8")
                        pursuits.symlink_to(target.name)
                    before = self._snapshot(memory_root)

                    result = self._run_install(memory_root, skills_target, check=False)

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("non-regular required files: PURSUITS.md", result.stderr)
                    self.assertIn("installation made no changes", result.stderr)
                    self.assertEqual(self._snapshot(memory_root), before)
                    self.assertFalse(skills_target.exists())
                    self.assertFalse((memory_root / ".runtime" / "install.stamp").exists())

    def test_shared_view_gitignore_alone_marks_existing_root_incomplete(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            view_dir = memory_root / "shared_views" / "auth-api"
            view_dir.mkdir(parents=True)
            (view_dir / ".gitignore").write_text("dist/\n", encoding="utf-8")
            before = self._snapshot(memory_root)

            result = self._run_install(memory_root, skills_target, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("existing RightMemory root is incomplete", result.stderr)
            self.assertIn("installation made no changes", result.stderr)
            self.assertEqual(self._snapshot(memory_root), before)
            self.assertFalse(skills_target.exists())

    def test_install_refuses_bare_git_target_without_mutation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory.git"
            skills_target = root / "skills"
            subprocess.run(["git", "init", "--bare", "-q", str(memory_root)], check=True)
            before = self._snapshot(memory_root)

            result = self._run_install(memory_root, skills_target, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bare Git repository", result.stderr)
            self.assertIn("installation made no changes", result.stderr)
            self.assertEqual(self._snapshot(memory_root), before)
            self.assertFalse(skills_target.exists())

    def test_install_refuses_target_nested_in_another_git_worktree(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            outer = root / "outer"
            memory_root = outer / "memory"
            skills_target = root / "skills"
            outer.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
            subprocess.run(["git", "config", "user.name", "Outer User"], cwd=outer, check=True)
            subprocess.run(["git", "config", "user.email", "outer@example.com"], cwd=outer, check=True)
            (outer / "README.md").write_text("outer\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=outer, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "outer baseline"], cwd=outer, check=True)
            memory_root.mkdir()
            self._env_with_fake_uv(outer)
            before = self._snapshot(outer)

            result = self._run_install(memory_root, skills_target, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("inside another Git working tree", result.stderr)
            self.assertIn("installation made no changes", result.stderr)
            self.assertEqual(self._snapshot(outer), before)
            self.assertFalse(skills_target.exists())

    def test_complete_committed_root_preserves_all_semantic_state_bytes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            self._install(memory_root, skills_target)
            semantic_files = {
                "MEMORY.md": b"# Custom Memory\r\n",
                "MEMORY_detail.md": b"detail\n",
                "PURSUITS.md": b"# Custom Pursuits\n",
                "PURSUIT_work.md": b"work\r\n",
                "corrections.md": b"# Corrections\n",
                "shared_views.toml": b"[connections]\n",
                "shares.toml": b"[shares]\n",
                "shared_views/example/view.md": b"view\n",
                "shared_views/example/recipe.toml": b"version = 1\n",
                "insight_logs/one.md": b"insight\n",
            }
            for relative, content in semantic_files.items():
                path = memory_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            subprocess.run(["git", "add", "--", *semantic_files], cwd=memory_root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "memory: custom semantic state"], cwd=memory_root, check=True)
            expected_head = self._git(memory_root, "rev-parse", "HEAD")

            result = self._install(memory_root, skills_target)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._git(memory_root, "rev-parse", "HEAD"), expected_head)
            self.assertEqual(
                {relative: (memory_root / relative).read_bytes() for relative in semantic_files},
                semantic_files,
            )

    def test_reinstall_preserves_complete_root_gitignore_bytes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            self._install(memory_root, skills_target)
            custom_gitignore = (
                b"*\r\n"
                b"!MEMORY.md\r\n"
                b"!PURSUITS.md\r\n"
                b"!local-control-plane-entry\r\n"
            )
            (memory_root / ".gitignore").write_bytes(custom_gitignore)
            subprocess.run(["git", "add", ".gitignore"], cwd=memory_root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "memory: explicit gitignore update"],
                cwd=memory_root,
                check=True,
            )
            expected_head = self._git(memory_root, "rev-parse", "HEAD")

            result = self._install(memory_root, skills_target)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._git(memory_root, "rev-parse", "HEAD"), expected_head)
            self.assertEqual((memory_root / ".gitignore").read_bytes(), custom_gitignore)
            self.assertEqual(self._git(memory_root, "status", "--short"), "")

    def test_new_root_installs_current_memory_gitignore_allowlist(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / ".gitignore").write_text(
                "*\n!MEMORY.md\n!dream_logs/\n!dream_logs/*.md\n",
                encoding="utf-8",
            )

            result = self._install(memory_root, skills_target)
            gitignore = (memory_root / ".gitignore").read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            gitignore,
            "*\n"
            "!.gitignore\n"
            "!MEMORY.md\n"
            "!MEMORY_*.md\n"
            "!PURSUITS.md\n"
            "!PURSUIT_*.md\n"
            "PURSUIT_RULES.md\n"
            "!corrections.md\n"
            "!shared_views.toml\n"
            "!shares.toml\n"
            "!shared_views/\n"
            "!shared_views/*/\n"
            "!shared_views/*/view.md\n"
            "!shared_views/*/retriever.md\n"
            "!shared_views/*/recipe.toml\n"
            "!shared_views/*/question.toml\n"
            "!shared_views/*/.gitignore\n"
            "!insight_logs/\n"
            "!insight_logs/*.md\n"
            "!update_queue/\n"
            "!update_queue/candidates/\n"
            "!update_queue/candidates/*.json\n"
            "!update_queue/recovery/\n"
            "!update_queue/recovery/*.json\n"
            "!update_queue/lease.json\n"
            "!update_records/\n"
            "!update_records/*.json\n",
        )

    def test_cli_agent_installs_two_command_skills_and_direct_maintainer(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = self._install(memory_root, skills_target)
            retriever = (skills_target / "memory-retriever" / "SKILL.md").read_text(encoding="utf-8")
            orchestrator = (skills_target / "rightmemory-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
            maintainer = (skills_target / "maintain-rightmemory" / "SKILL.md").read_text(encoding="utf-8")
            install_stamp = (memory_root / ".runtime" / "install.stamp").read_text(encoding="utf-8")
            wrapper = (root / "home" / ".local" / "bin" / "rightmemory").read_text(encoding="utf-8")
            installed_skill_directories = sorted(path.name for path in skills_target.iterdir() if path.is_dir())

        self.assertIn("MODE         = cli-agent", result.stdout)
        self.assertIn("Write [agent_cli], [retrieve.agent_cli], and a default writer [update.agent_cli] config", result.stdout)
        self.assertNotIn("Write [retrieve.model] and a default writer [update.model] config", result.stdout)
        self.assertIn("mode=cli-agent", install_stamp)
        self.assertIn("user explicitly chooses read-only RightMemory retrieval", retriever)
        self.assertIn("This skill never submits updates", retriever)
        self.assertNotIn("rightmemory update submit", retriever)
        self.assertIn("user explicitly chooses full RightMemory orchestration", orchestrator)
        self.assertIn("rightmemory retrieve --session <stable-session-id>", orchestrator)
        self.assertIn("rightmemory update submit --session <stable-session-id>", orchestrator)
        self.assertIn("including initially small work", orchestrator)
        self.assertIn("open-context questions", orchestrator)
        self.assertIn("--include-returned", orchestrator)
        self.assertIn("Provider question context", orchestrator)
        self.assertIn("rightmemory shared-view ask <mq-id>", orchestrator)
        self.assertIn("export PYTHONUTF8=1", wrapper)
        self.assertIn('export RIGHTMEMORY_ROOT="', wrapper)
        self.assertIn('exec "', wrapper)
        self.assertIn(' -m rightmemory.cli "$@"', wrapper)
        self.assertNotIn("standalone mode", orchestrator)
        self.assertNotIn("standalone runtime", orchestrator)
        self.assertIn("user explicitly asks the current agent", maintainer)
        self.assertNotIn(str(memory_root), maintainer)
        self.assertIn("rightmemory reference schema", maintainer)
        self.assertIn("rightmemory reference edit-correction", maintainer)
        self.assertIn("`rightmemory status`", maintainer)
        self.assertIn("`rightmemory --profile <name> status`", maintainer)
        self.assertIn("use the reported `root:`", maintainer)
        self.assertIn("do not infer or guess it", maintainer)
        self.assertIn("Never call `rightmemory update`, submit candidates", maintainer)
        self.assertIn("`Strongly recommended`", maintainer)
        self.assertIn("wait for explicit approval", maintainer)
        self.assertIn("dedicated temporary Git worktree", maintainer)
        self.assertIn("`maintain: <concise maintenance summary>`", maintainer)
        self.assertIn("`git merge --ff-only`", maintainer)
        self.assertIn("without creating another commit", maintainer)
        self.assertIn("rightmemory validate --root <worktree>", maintainer)
        self.assertIn("rightmemory validate --root <root>", maintainer)
        self.assertIn("push when sync is configured", maintainer)
        self.assertFalse((skills_target / "rightmemory-edit-correction-rules.md").exists())
        self.assertEqual(
            installed_skill_directories,
            ["maintain-rightmemory", "memory-retriever", "rightmemory-orchestrator"],
        )

    def test_rerun_preserves_managed_examples_and_user_state_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            self._install(memory_root, skills_target)

            memory_path = memory_root / "MEMORY.md"
            memory = memory_path.read_text(encoding="utf-8")
            memory_path.write_text(
                "# Real Memory {#real-memory}\n\n- `real-node` keep me.\n\n"
                + memory.replace("Example Application", "Stale Example Application"),
                encoding="utf-8",
            )
            pursuits_path = memory_root / "PURSUITS.md"
            pursuits = pursuits_path.read_text(encoding="utf-8")
            pursuits_path.write_text(
                "# User Pursuits\n\n## Continue Release {#continue-release}\n\nKeep this live intent.\n\n"
                + pursuits.replace("Example Release Readiness", "Stale Release Readiness"),
                encoding="utf-8",
            )
            expected_memory = memory_path.read_bytes()
            expected_pursuits = pursuits_path.read_bytes()

            self._install(memory_root, skills_target)
            actual_memory = memory_path.read_bytes()
            actual_pursuits = pursuits_path.read_bytes()
            refreshed_memory = memory_path.read_text(encoding="utf-8")
            refreshed_pursuits = pursuits_path.read_text(encoding="utf-8")

        self.assertEqual(actual_memory, expected_memory)
        self.assertEqual(actual_pursuits, expected_pursuits)
        self.assertIn("# Real Memory {#real-memory}", refreshed_memory)
        self.assertIn("- `real-node` keep me.", refreshed_memory)
        self.assertIn("Stale Example Application", refreshed_memory)
        self.assertEqual(refreshed_memory.count(EXAMPLE_START), 1)
        self.assertEqual(refreshed_memory.count(EXAMPLE_END), 1)
        self.assertIn("## Continue Release {#continue-release}", refreshed_pursuits)
        self.assertIn("Keep this live intent.", refreshed_pursuits)
        self.assertIn("Stale Release Readiness", refreshed_pursuits)
        self.assertEqual(refreshed_pursuits.count(PURSUIT_EXAMPLE_START), 1)
        self.assertEqual(refreshed_pursuits.count(PURSUIT_EXAMPLE_END), 1)

    def test_rerun_refuses_legacy_root_references_without_mutation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text("# User Memory\n", encoding="utf-8")
            (memory_root / "PURSUITS.md").write_text("# User Pursuits\n\nDo not replace.\n", encoding="utf-8")
            (memory_root / "PURSUIT_RULES.md").write_text("# Custom Rules\n\nDo not replace.\n", encoding="utf-8")
            (memory_root / "AGENT_CORRECTION_MEMORY_RULES.md").write_text(
                "# Custom Agent Correction Rules\n\nDo not replace.\n",
                encoding="utf-8",
            )

            before = self._snapshot(memory_root)
            result = self._run_install(memory_root, skills_target, check=False)

            pursuits = (memory_root / "PURSUITS.md").read_text(encoding="utf-8")
            rules = (memory_root / "PURSUIT_RULES.md").read_text(encoding="utf-8")
            after = self._snapshot(memory_root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy package-reference files", result.stderr)
        self.assertEqual(after, before)
        self.assertEqual(pursuits, "# User Pursuits\n\nDo not replace.\n")
        self.assertEqual(rules, "# Custom Rules\n\nDo not replace.\n")

    def test_rerun_does_not_migrate_known_old_starter_block(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text(
                "# Starter Knowledge Base {#starter-knowledge-base}\n\n"
                "> Old starter text.\n\n"
                "---\n\n"
                "# Real Memory {#real-memory}\n\n"
                "- `real-node` keep me. → []\n",
                encoding="utf-8",
            )
            (memory_root / "PURSUITS.md").write_text("# Existing Pursuits\n", encoding="utf-8")
            expected = (memory_root / "MEMORY.md").read_bytes()

            self._install(memory_root, skills_target)
            actual = (memory_root / "MEMORY.md").read_bytes()
            migrated = (memory_root / "MEMORY.md").read_text(encoding="utf-8")

        self.assertEqual(actual, expected)
        self.assertNotIn(EXAMPLE_START, migrated)
        self.assertNotIn("# Sample Project Graph", migrated)
        self.assertIn("# Starter Knowledge Base", migrated)
        self.assertIn("# Real Memory {#real-memory}", migrated)
        self.assertIn("- `real-node` keep me. → []", migrated)

    def test_default_install_uses_standalone_and_default_skill_targets(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            env = self._env_with_fake_uv(root)

            result = subprocess.run(
                ["bash", "install.sh"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            home = root / "home"
            self.assertTrue((home / ".rightmemory" / "MEMORY.md").exists())
            self.assertTrue((home / ".rightmemory" / "PURSUITS.md").exists())
            self.assertFalse((home / ".rightmemory" / "PURSUIT_RULES.md").exists())
            self.assertFalse((home / ".rightmemory" / "AGENT_CORRECTION_MEMORY_RULES.md").exists())
            self.assertTrue((home / ".rightmemory" / ".runtime" / "install.stamp").exists())
            for target in (home / ".codex" / "skills", home / ".claude" / "skills"):
                self.assertFalse((target / "rightmemory-edit-correction-rules.md").exists())
                self.assertTrue((target / "maintain-rightmemory" / "SKILL.md").exists())
                self.assertTrue((target / "memory-retriever" / "SKILL.md").exists())
                self.assertTrue((target / "rightmemory-orchestrator" / "SKILL.md").exists())
                self.assertFalse((target / "memory-orchestrator").exists())
            self.assertFalse((home / ".codex" / "skills" / "memory-curator").exists())
            self.assertFalse((home / ".claude" / "skills" / "memory-dreamer").exists())
            self.assertIn("MODE         = standalone", result.stdout)
            self.assertIn("Write [retrieve.model] and a default writer [update.model] config", result.stdout)
            self.assertIn("rightmemory is installed", result.stdout)
            self.assertIn("semantic upgrade baseline recorded", result.stdout)
            self.assertNotIn("pending for the next dreamer cycle", result.stdout)

    def test_install_reports_missing_git_before_writes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            env = self._env_with_fake_uv(root)
            fake_git = root / "bin" / "git"
            fake_git.write_text("#!/usr/bin/env sh\nexit 1\n", encoding="utf-8")
            fake_git.chmod(0o755)
            env["PATH"] = f"{root / 'bin'}:/bin"

            result = subprocess.run(
                ["bash", "install.sh", "--mode", "cli-agent", str(memory_root), str(skills_target)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            memory_exists = memory_root.exists()
            skills_exists = skills_target.exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing or unusable required command: git", result.stderr)
        self.assertIn("macOS:", result.stderr)
        self.assertIn("Linux / WSL", result.stderr)
        self.assertIn("Official git install guide", result.stderr)
        self.assertFalse(memory_exists)
        self.assertFalse(skills_exists)

    def test_install_reports_missing_uv_before_writes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = subprocess.run(
                ["bash", "install.sh", "--mode", "cli-agent", str(memory_root), str(skills_target)],
                cwd=REPO_ROOT,
                env=self._env_with_fake_git_no_uv(root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            memory_exists = memory_root.exists()
            skills_exists = skills_target.exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing or unusable required command: uv", result.stderr)
        self.assertIn("macOS:", result.stderr)
        self.assertIn("Linux / WSL", result.stderr)
        self.assertIn("Official uv install guide", result.stderr)
        self.assertFalse(memory_exists)
        self.assertFalse(skills_exists)

    def test_install_reports_uv_python_failure_before_writes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = subprocess.run(
                ["bash", "install.sh", "--mode", "cli-agent", str(memory_root), str(skills_target)],
                cwd=REPO_ROOT,
                env=self._env_with_failing_uv_python(root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            memory_exists = memory_root.exists()
            skills_exists = skills_target.exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not find or provision Python >=3.11 with uv", result.stderr)
        self.assertIn("uv Python guide", result.stderr)
        self.assertFalse(memory_exists)
        self.assertFalse(skills_exists)

    def test_install_reports_pending_semantic_upgrade_notes_for_existing_memory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text(
                "# Existing Memory {#existing-memory}\n\n- `existing-node` keep me. → []\n",
                encoding="utf-8",
            )
            (memory_root / "PURSUITS.md").write_text("# Existing Pursuits\n", encoding="utf-8")

            result = self._install(memory_root, skills_target)
            state_exists = (memory_root / ".runtime" / "semantic-upgrades.json").exists()

        self.assertIn("semantic upgrade note(s) pending", result.stdout)
        self.assertIn("user-context-agent-behavior-split", result.stdout)
        self.assertIn("open-context-questions", result.stdout)
        self.assertIn("uncertain-memory-marker", result.stdout)
        self.assertTrue(state_exists)

    def test_install_warns_when_stale_rightmemory_precedes_installed_wrapper(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            env = self._env_with_fake_uv(root)
            stale_rightmemory = root / "bin" / "rightmemory"
            stale_rightmemory.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
            stale_rightmemory.chmod(0o755)

            result = subprocess.run(
                ["bash", "install.sh", "--mode", "cli-agent", str(memory_root), str(skills_target)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            installed_wrapper = root / "home" / ".local" / "bin" / "rightmemory"

        self.assertIn(f"rightmemory is installed at {installed_wrapper}", result.stdout)
        self.assertIn(f"PATH currently resolves rightmemory to:\n\n              {stale_rightmemory}", result.stdout)
        self.assertIn("stale code or use the wrong RIGHTMEMORY_ROOT", result.stdout)

    def test_subagent_mode_is_rejected_with_cli_agent_guidance(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = subprocess.run(
                ["bash", "install.sh", "--mode", "subagent", str(memory_root), str(skills_target)],
                cwd=REPO_ROOT,
                env=self._env_with_fake_uv(root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported --mode: subagent", result.stderr)
        self.assertIn("--mode cli-agent", result.stderr)

    def test_install_removes_old_rightmemory_role_skills_and_preserves_user_dirs(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            old_curator = skills_target / "memory-curator"
            old_dreamer = skills_target / "memory-dreamer"
            old_orchestrator = skills_target / "memory-orchestrator"
            old_curator.mkdir(parents=True)
            old_dreamer.mkdir(parents=True)
            old_orchestrator.mkdir(parents=True)
            old_curator.joinpath("SKILL.md").write_text(
                "---\nname: memory-curator\n---\n"
                "You are the subagent execution wrapper for RightMemory retrieval and update work.\n",
                encoding="utf-8",
            )
            old_dreamer.joinpath("SKILL.md").write_text(
                "---\nname: memory-dreamer\n---\n"
                "You are the subagent execution wrapper for RightMemory dream cycles.\n",
                encoding="utf-8",
            )
            old_orchestrator.joinpath("SKILL.md").write_text(
                LEGACY_ORCHESTRATOR_TEMPLATE.replace("{{MEMORY_ROOT}}", str(memory_root)),
                encoding="utf-8",
            )

            self._install(memory_root, skills_target)
            old_curator_exists = old_curator.exists()
            old_dreamer_exists = old_dreamer.exists()
            old_orchestrator_exists = old_orchestrator.exists()

            user_memory_root = root / "user-memory"
            user_skills_target = root / "user-skills"
            user_curator = user_skills_target / "memory-curator"
            user_dreamer = user_skills_target / "memory-dreamer"
            user_orchestrator = user_skills_target / "memory-orchestrator"
            user_curator.mkdir(parents=True)
            user_dreamer.mkdir(parents=True)
            user_orchestrator.mkdir(parents=True)
            user_curator.joinpath("SKILL.md").write_text(
                "---\nname: memory-curator\n---\nUser-owned memory-curator helper.\n",
                encoding="utf-8",
            )
            user_dreamer.joinpath("SKILL.md").write_text(
                "---\nname: memory-dreamer\n---\nUser-owned memory-dreamer helper.\n",
                encoding="utf-8",
            )
            customized_orchestrator = (
                LEGACY_ORCHESTRATOR_TEMPLATE.replace("{{MEMORY_ROOT}}", str(user_memory_root))
                + "\n# User customization\n"
            )
            user_orchestrator.joinpath("SKILL.md").write_text(customized_orchestrator, encoding="utf-8")

            self._install(user_memory_root, user_skills_target)
            user_curator_exists = user_curator.exists()
            user_dreamer_exists = user_dreamer.exists()
            user_orchestrator_exists = user_orchestrator.exists()
            user_curator_text = user_curator.joinpath("SKILL.md").read_text(encoding="utf-8")
            user_dreamer_text = user_dreamer.joinpath("SKILL.md").read_text(encoding="utf-8")
            user_orchestrator_text = user_orchestrator.joinpath("SKILL.md").read_text(encoding="utf-8")

        self.assertFalse(old_curator_exists)
        self.assertFalse(old_dreamer_exists)
        self.assertFalse(old_orchestrator_exists)
        self.assertTrue(user_curator_exists)
        self.assertTrue(user_dreamer_exists)
        self.assertTrue(user_orchestrator_exists)
        self.assertIn("User-owned", user_curator_text)
        self.assertIn("User-owned", user_dreamer_text)
        self.assertEqual(user_orchestrator_text, customized_orchestrator)

    def _install(self, memory_root: Path, skills_target: Path) -> subprocess.CompletedProcess[str]:
        return self._run_install(memory_root, skills_target, check=True)

    def _run_install(
        self,
        memory_root: Path,
        skills_target: Path,
        *,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        root = memory_root.parent
        return subprocess.run(
            ["bash", "install.sh", "--mode", "cli-agent", str(memory_root), str(skills_target)],
            cwd=REPO_ROOT,
            env=self._env_with_fake_uv(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def _snapshot(self, root: Path) -> tuple[tuple[str, str, bytes | str], ...] | None:
        if not os.path.lexists(root):
            return None
        entries: list[tuple[str, str, bytes | str]] = []
        for path in sorted((root, *root.rglob("*")), key=lambda item: str(item.relative_to(root))):
            relative = "." if path == root else path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append((relative, "symlink", os.readlink(path)))
            elif path.is_dir():
                entries.append((relative, "directory", b""))
            else:
                entries.append((relative, "file", path.read_bytes()))
        return tuple(entries)

    def _git(self, memory_root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=memory_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
