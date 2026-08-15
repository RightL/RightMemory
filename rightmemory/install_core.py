from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from .async_update import AsyncUpdateStore, _state_from_json
from .platform import prepare_command
from .review import ReviewDeliveryStore
from .update_queue import validate_update_queue
from .update_record import validate_update_records


PYTHON_REQUIREMENT = ">=3.11"
# Hash of the superseded managed template after its generated memory-root line is normalized.
LEGACY_MEMORY_ORCHESTRATOR_SHA256 = "b2e0ed77f669b8d1da3f702755b85e7ac9cb8a664ccd02ffe9f7149cea095e00"
LEGACY_MEMORY_ROOT_LINE = (
    "- The memory root is `{{MEMORY_ROOT}}`; the main agent must not read or edit files there by any means "
    "unless the user explicitly permits direct access."
)
LEGACY_MEMORY_ROOT_LINE_PATTERN = re.compile(
    r"(?m)^- The memory root is `[^`\r\n]+`; the main agent must not read or edit files there by any means "
    r"unless the user explicitly permits direct access\.$"
)
MEMORY_GITIGNORE = """\
*
!.gitignore
!MEMORY.md
!MEMORY_*.md
!PURSUITS.md
!PURSUIT_*.md
!AGENT_GUIDANCE_INBOX.md
PURSUIT_RULES.md
!corrections.md
!shared_views.toml
!shares.toml
!shared_views/
!shared_views/*/
!shared_views/*/view.md
!shared_views/*/retriever.md
!shared_views/*/recipe.toml
!shared_views/*/question.toml
!shared_views/*/.gitignore
!insight_logs/
!insight_logs/*.md
!update_queue/
!update_queue/candidates/
!update_queue/candidates/*.json
!update_queue/recovery/
!update_queue/recovery/*.json
!update_queue/lease.json
!update_records/
!update_records/*.json
"""
ROLE_PROMPTS = {
    "{{ROLE_PROMPT_RETRIEVE}}": "retrieve.md",
    "{{ROLE_PROMPT_UPDATE}}": "update.md",
    "{{ROLE_PROMPT_DREAMER}}": "dreamer.md",
    "{{ROLE_PROMPT_REVIEWER}}": "reviewer.md",
}
LEGACY_ROOT_REFERENCE_FILES = (
    "PURSUIT_RULES.md",
    "AGENT_CORRECTION_MEMORY_RULES.md",
)
LEGACY_LOOSE_REFERENCE_SHA256 = {
    "rightmemory-schema.md": "34d7aeb28bd49cb49f3ddc057ed12e964029a59d443704f0699081778b70598d",
    "rightmemory-edit-correction-rules.md": (
        "6c4a3bf3171d1cd4115d9f7ffbe136b1fcf540961e69c9b86fcc4563dd0e3286"
    ),
}


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallTarget:
    kind: Literal["new", "existing"]
    has_head: bool
    missing_required: tuple[str, ...]
    invalid_required: tuple[str, ...]
    legacy_references: tuple[str, ...] = ()
    git_layout_error: str | None = None


class Installer:
    def __init__(self, repo_root: Path, mode: str, memory_root: Path, skills_targets: list[Path]):
        self.repo_root = repo_root
        self.mode = mode
        self.memory_root = memory_root
        self.skills_targets = skills_targets
        self.is_windows = os.name == "nt"

        if self.is_windows:
            local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            self.runtime_home = Path(local_app_data) / "RightMemory"
            self.runtime_bin_dir = self.runtime_home / "bin"
            self.runtime_command = self.runtime_bin_dir / "rightmemory.cmd"
        else:
            self.runtime_home = _posix_data_home() / "rightmemory"
            self.runtime_bin_dir = Path.home() / ".local" / "bin"
            self.runtime_command = self.runtime_bin_dir / "rightmemory"
        self.runtime_venv = self.runtime_home / "venv"
        self.runtime_python: Path | None = None

    def run(self) -> None:
        self._require_valid_update_queue()
        self._require_valid_update_records()
        self._require_no_live_legacy_async_updates()
        self._require_no_legacy_review_deliveries()
        target = self._inspect_target()
        self._print_layout()
        self._require_complete_existing_target(target)

        if target.kind == "new":
            self._bootstrap_state()
        else:
            self._preserve_existing_state()
        (self.memory_root / "insight_logs").mkdir(parents=True, exist_ok=True)
        self._ensure_memory_git()
        self._install_runtime()
        self._run_semantic_upgrades(target)
        self._install_skills()
        self._warn_if_command_not_on_path()
        self._write_install_stamp()
        self._print_next_steps()

    def _require_valid_update_queue(self) -> None:
        diagnostics = validate_update_queue(self.memory_root)
        if diagnostics:
            raise InstallError(
                "RightMemory update queue is invalid:\n- "
                + "\n- ".join(diagnostics)
                + "\ninstallation made no changes; repair or remove the invalid queue state before reinstalling"
            )

    def _require_valid_update_records(self) -> None:
        diagnostics = validate_update_records(self.memory_root)
        if diagnostics:
            raise InstallError(
                "RightMemory update records are invalid:\n- "
                + "\n- ".join(diagnostics)
                + "\ninstallation made no changes; repair the invalid retained evidence before reinstalling"
            )

    def _require_no_live_legacy_async_updates(self) -> None:
        runtime_root = self.memory_root / ".runtime"
        async_parent = runtime_root / "async"
        async_root = self.memory_root / ".runtime" / "async" / "update"
        incompatible: list[str] = []
        for container in (runtime_root, async_parent, async_root):
            if not container.exists() and not container.is_symlink():
                return
            if container.is_symlink() or not container.is_dir():
                incompatible.append(container.relative_to(self.memory_root).as_posix())
                break
        if incompatible:
            raise InstallError(
                "live async updates are incompatible with this coordinated upgrade:\n- "
                + "\n- ".join(incompatible)
                + "\ninstallation made no changes; use the currently installed RightMemory to finish, "
                "retry, or undo every live update until `update pull` shows both `pending: 0` and "
                "`current_batch: 0`; archive any listed drained legacy state, then rerun the installer"
            )

        for path in sorted(async_root.glob("*.json")):
            relative = path.relative_to(self.memory_root).as_posix()
            if path.is_symlink() or not path.is_file():
                incompatible.append(relative)
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                state = _state_from_json(data)
                if state.session_id != path.stem or state.role != "update":
                    raise ValueError("async update state identity does not match its path")
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                incompatible.append(relative)

        reservation_store = AsyncUpdateStore(self.memory_root, "update")
        reservations_root = async_root / "_batches"
        if reservations_root.exists() or reservations_root.is_symlink():
            if reservations_root.is_symlink() or not reservations_root.is_dir():
                incompatible.append(
                    reservations_root.relative_to(self.memory_root).as_posix()
                )
            else:
                for path in sorted(reservations_root.glob("*.json")):
                    relative = path.relative_to(self.memory_root).as_posix()
                    if path.is_symlink() or not path.is_file():
                        incompatible.append(relative)
                        continue
                    try:
                        reservation_store._read_reservation_path(path)
                    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                        incompatible.append(relative)
        if incompatible:
            raise InstallError(
                "live async updates are incompatible with this coordinated upgrade:\n- "
                + "\n- ".join(incompatible)
                + "\ninstallation made no changes; use the currently installed RightMemory to finish, "
                "retry, or undo every live update until `update pull` shows both `pending: 0` and "
                "`current_batch: 0`; archive any listed drained legacy state, then rerun the installer"
            )

    def _require_no_legacy_review_deliveries(self) -> None:
        runtime_root = self.memory_root / ".runtime"
        review_parent = runtime_root / "review"
        deliveries_root = self.memory_root / ".runtime" / "review" / "deliveries"
        incompatible: list[str] = []
        for container in (runtime_root, review_parent, deliveries_root):
            if not container.exists() and not container.is_symlink():
                return
            if container.is_symlink() or not container.is_dir():
                incompatible.append(container.relative_to(self.memory_root).as_posix())
                break
        if incompatible:
            raise InstallError(
                "pending transcript-review deliveries are incompatible with this coordinated upgrade:\n- "
                + "\n- ".join(incompatible)
                + "\ninstallation made no changes; use the currently installed RightMemory review watcher "
                "to finish these deliveries, then rerun the installer"
            )

        delivery_store = ReviewDeliveryStore(self.memory_root)
        for path in sorted(deliveries_root.glob("*.json")):
            relative = path.relative_to(self.memory_root).as_posix()
            if path.is_symlink() or not path.is_file():
                incompatible.append(relative)
                continue
            try:
                delivery_store._load(path)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                incompatible.append(relative)
        if incompatible:
            raise InstallError(
                "pending transcript-review deliveries are incompatible with this coordinated upgrade:\n- "
                + "\n- ".join(incompatible)
                + "\ninstallation made no changes; use the currently installed RightMemory review watcher "
                "to finish these deliveries, then rerun the installer"
            )

    def _print_layout(self) -> None:
        print("Installing RightMemory")
        print(f"  MODE         = {self.mode}")
        print(f"  MEMORY_ROOT  = {self.memory_root}")
        print(f"  SKILLS_ROOTS = {' '.join(str(path) for path in self.skills_targets)}")
        print(f"  RUNTIME_HOME = {self.runtime_home}")
        print(f"  RUNTIME_VENV = {self.runtime_venv}")
        print("  CLI_COMMAND  = rightmemory")
        print()

    def _inspect_target(self) -> InstallTarget:
        # Keep target classification side-effect free so refusal is an exact no-op.
        has_head, git_layout_error = self._inspect_target_git()
        semantic_state_exists = self._semantic_state_exists()
        kind: Literal["new", "existing"] = (
            "existing" if has_head or semantic_state_exists or git_layout_error else "new"
        )
        missing: list[str] = []
        invalid: list[str] = []
        if kind == "existing":
            for name in ("MEMORY.md", "PURSUITS.md"):
                path = self.memory_root / name
                if not os.path.lexists(path):
                    missing.append(name)
                elif path.is_symlink() or not path.is_file():
                    invalid.append(name)
        legacy_references = tuple(
            name for name in LEGACY_ROOT_REFERENCE_FILES if os.path.lexists(self.memory_root / name)
        )
        return InstallTarget(
            kind,
            has_head,
            tuple(sorted(missing)),
            tuple(sorted(invalid)),
            legacy_references,
            git_layout_error,
        )

    def _inspect_target_git(self) -> tuple[bool, str | None]:
        if not self.memory_root.is_dir():
            return False, None
        bare = _run(
            ["git", "rev-parse", "--is-bare-repository"],
            cwd=self.memory_root,
            capture=True,
        )
        if bare.returncode == 0 and bare.stdout.strip() == "true":
            head = _run(
                ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
                cwd=self.memory_root,
                capture=True,
            )
            return head.returncode == 0, "the memory root is a bare Git repository"

        top_level = _run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self.memory_root,
            capture=True,
        )
        if top_level.returncode == 0 and top_level.stdout.strip():
            resolved_top = Path(top_level.stdout.strip()).resolve()
            if resolved_top != self.memory_root.resolve():
                return False, "the memory root is inside another Git working tree"
            head = _run(
                ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
                cwd=self.memory_root,
                capture=True,
            )
            return head.returncode == 0, None
        if os.path.lexists(self.memory_root / ".git"):
            return False, "the memory root has an unusable .git entry"
        return False, None

    def _semantic_state_exists(self) -> bool:
        if not self.memory_root.is_dir():
            return False

        for path in self.memory_root.iterdir():
            name = path.name
            if name in {
                "MEMORY.md",
                "PURSUITS.md",
                "PURSUIT_RULES.md",
                "AGENT_CORRECTION_MEMORY_RULES.md",
                "corrections.md",
                "shared_views.toml",
                "shares.toml",
            }:
                return True
            if (name.startswith("MEMORY_") or name.startswith("PURSUIT_")) and name.endswith(".md"):
                return True

        insight_logs = self.memory_root / "insight_logs"
        if insight_logs.is_dir() and any(path.name.endswith(".md") for path in insight_logs.iterdir()):
            return True

        update_queue = self.memory_root / "update_queue"
        if update_queue.is_dir() and any(update_queue.iterdir()):
            return True
        update_records = self.memory_root / "update_records"
        if update_records.is_dir() and any(update_records.iterdir()):
            return True

        shared_views = self.memory_root / "shared_views"
        if shared_views.is_dir():
            shared_view_files = {"view.md", "retriever.md", "recipe.toml", "question.toml", ".gitignore"}
            for view_dir in shared_views.iterdir():
                if view_dir.is_dir() and any(os.path.lexists(view_dir / name) for name in shared_view_files):
                    return True
        return False

    def _require_complete_existing_target(self, target: InstallTarget) -> None:
        if target.git_layout_error:
            raise InstallError(
                f"unsupported RightMemory target: {target.git_layout_error}\n"
                "installation made no changes; choose a standalone non-bare Git working tree for the memory root"
            )
        if target.kind != "existing":
            return
        if target.legacy_references:
            raise InstallError(
                "existing RightMemory root contains legacy package-reference files: "
                f"{', '.join(target.legacy_references)}\n"
                "installation made no changes; these files are no longer read as root state. "
                "Review any local changes, remove them explicitly, commit that removal, and rerun the installer"
            )
        if target.missing_required or target.invalid_required:
            details: list[str] = []
            if target.missing_required:
                details.append(f"missing required files: {', '.join(target.missing_required)}")
            if target.invalid_required:
                details.append(f"non-regular required files: {', '.join(target.invalid_required)}")
            raise InstallError(
                f"existing RightMemory root is incomplete: {'; '.join(details)}\n"
                "installation made no changes; migrate and review this root explicitly before reinstalling"
            )

    def _bootstrap_state(self) -> None:
        self.memory_root.mkdir(parents=True, exist_ok=True)
        memory_file = self.memory_root / "MEMORY.md"
        pursuits_file = self.memory_root / "PURSUITS.md"
        shutil.copyfile(self.repo_root / "MEMORY.example.md", memory_file)
        shutil.copyfile(self.repo_root / "PURSUITS.example.md", pursuits_file)
        gitignore = self.memory_root / ".gitignore"
        _write_utf8(gitignore, MEMORY_GITIGNORE)
        print(f"  [new]     {memory_file}  (from MEMORY.example.md)")
        print(f"  [new]     {pursuits_file}  (from PURSUITS.example.md)")
        print(f"  [new]     {gitignore}  (memory allowlist)")

    def _preserve_existing_state(self) -> None:
        for name in ("MEMORY.md", "PURSUITS.md"):
            print(f"  [keep]    {self.memory_root / name} already exists")

    def _ensure_memory_git(self) -> None:
        git_marker = self.memory_root / ".git"
        if git_marker.exists():
            print(f"  [keep]    {self.memory_root} is already a git repo")
        else:
            self._git("init", "-q")
            print(f"  [new]     git init in {self.memory_root}")
        self._ensure_git_author()
        self._ensure_initial_commit()

    def _ensure_git_author(self) -> None:
        name = self._git_capture("config", "--local", "--get", "user.name")
        email = self._git_capture("config", "--local", "--get", "user.email")
        if name and email:
            print(f"  [keep]    git author configured for {self.memory_root}")
            return
        if not name:
            name = self._git_capture("config", "--global", "--get", "user.name") or "RightMemory"
            self._git("config", "--local", "user.name", name)
            print(f"  [config] git user.name = {name}  ({self.memory_root})")
        if not email:
            email = self._git_capture("config", "--global", "--get", "user.email") or "rightmemory@localhost"
            self._git("config", "--local", "user.email", email)
            print(f"  [config] git user.email = {email}  ({self.memory_root})")

    def _initial_memory_files(self) -> list[str]:
        files: list[str] = []

        def add(relative: str) -> None:
            if (self.memory_root / relative).is_file():
                files.append(relative.replace("\\", "/"))

        add(".gitignore")
        add("MEMORY.md")
        files.extend(path.name for path in sorted(self.memory_root.glob("MEMORY_*.md")) if path.is_file())
        add("PURSUITS.md")
        files.extend(
            path.name
            for path in sorted(self.memory_root.glob("PURSUIT_*.md"))
            if path.is_file()
        )
        add("AGENT_GUIDANCE_INBOX.md")
        add("corrections.md")
        add("shared_views.toml")
        add("shares.toml")
        shared_views = self.memory_root / "shared_views"
        if shared_views.is_dir():
            for view_dir in sorted(path for path in shared_views.iterdir() if path.is_dir()):
                for name in ("view.md", "retriever.md", "recipe.toml", "question.toml", ".gitignore"):
                    add(f"shared_views/{view_dir.name}/{name}")
        insight_logs = self.memory_root / "insight_logs"
        if insight_logs.is_dir():
            files.extend(
                f"insight_logs/{path.name}" for path in sorted(insight_logs.glob("*.md")) if path.is_file()
            )
        update_queue = self.memory_root / "update_queue"
        candidates = update_queue / "candidates"
        if candidates.is_dir():
            files.extend(
                f"update_queue/candidates/{path.name}"
                for path in sorted(candidates.glob("*.json"))
                if path.is_file() and re.fullmatch(r"[0-9a-f]{32}\.json", path.name)
            )
        recovery = update_queue / "recovery"
        if recovery.is_dir():
            files.extend(
                f"update_queue/recovery/{path.name}"
                for path in sorted(recovery.glob("*.json"))
                if path.is_file() and re.fullmatch(r"update-batch-[0-9a-f]{64}\.json", path.name)
            )
        add("update_queue/lease.json")
        update_records = self.memory_root / "update_records"
        if update_records.is_dir():
            files.extend(
                f"update_records/{path.name}"
                for path in sorted(update_records.glob("*.json"))
                if path.is_file()
                and re.fullmatch(r"update-batch-[0-9a-f]{64}\.json", path.name)
            )
        return files

    def _ensure_initial_commit(self) -> None:
        if self._git_result("rev-parse", "--verify", "--quiet", "HEAD").returncode == 0:
            print("  [keep]    initial memory commit already exists")
            return
        files = self._initial_memory_files()
        regular_files = [path for path in files if path != ".gitignore"]
        if regular_files:
            self._git("add", "--", *regular_files)
        if ".gitignore" in files:
            self._git("add", "-f", "--", ".gitignore")
        staged = self._git_capture("diff", "--cached", "--name-only", "--", *files).splitlines() if files else []
        if not staged:
            print("  [skip]    no memory files to baseline commit")
            return
        self._git("commit", "-q", "-m", "memory: initial baseline", "--", *staged)
        print("  [new]     initial memory baseline commit")

    def _install_runtime(self) -> None:
        self.runtime_home.mkdir(parents=True, exist_ok=True)
        self.runtime_bin_dir.mkdir(parents=True, exist_ok=True)
        if not self.runtime_venv.is_dir():
            result = _run(["uv", "venv", "--no-project", "--python", PYTHON_REQUIREMENT, str(self.runtime_venv)])
            if result.returncode != 0:
                raise InstallError(f"uv could not create the Python {PYTHON_REQUIREMENT} runtime at {self.runtime_venv}")
            print(f"  [new]     {self.runtime_venv}")
        else:
            print(f"  [keep]    {self.runtime_venv} already exists")
        self.runtime_python = self._venv_python()
        result = _run(["uv", "pip", "install", "--python", str(self.runtime_python), str(self.repo_root)])
        if result.returncode != 0:
            raise InstallError(
                f"could not install RightMemory into the uv-managed runtime: {self.runtime_venv}"
            )
        print(f"  [install] rightmemory package into {self.runtime_venv}")
        self._write_runtime_wrapper()

    def _venv_python(self) -> Path:
        candidates = (
            self.runtime_venv / "Scripts" / "python.exe",
            self.runtime_venv / "Scripts" / "python.cmd",
            self.runtime_venv / "Scripts" / "python.bat",
            self.runtime_venv / "bin" / "python",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise InstallError(f"uv created {self.runtime_venv}, but no Python executable was found inside it")

    def _write_runtime_wrapper(self) -> None:
        assert self.runtime_python is not None
        if self.is_windows:
            root = _cmd_literal(str(self.memory_root))
            python = _cmd_literal(str(self.runtime_python))
            call = "call " if self.runtime_python.suffix.lower() in {".cmd", ".bat"} else ""
            text = (
                "@echo off\n"
                "setlocal\n"
                'set "PYTHONUTF8=1"\n'
                f'set "RIGHTMEMORY_ROOT={root}"\n'
                f'{call}"{python}" -m rightmemory.entrypoint %*\n'
                "exit /b %ERRORLEVEL%\n"
            )
        else:
            text = (
                "#!/usr/bin/env sh\n"
                "export PYTHONUTF8=1\n"
                f'export RIGHTMEMORY_ROOT="{_shell_double_quoted(str(self.memory_root))}"\n'
                f'exec "{_shell_double_quoted(str(self.runtime_python))}" -m rightmemory.entrypoint "$@"\n'
            )
        _write_utf8(self.runtime_command, text)
        if not self.is_windows:
            self.runtime_command.chmod(0o755)
        print(f"  [install] {self.runtime_command}")

    def _run_semantic_upgrades(self, target: InstallTarget) -> None:
        assert self.runtime_python is not None
        command = "baseline" if target.kind == "new" else "refresh"
        result = _run(
            [
                str(self.runtime_python),
                "-m",
                "rightmemory.semantic_upgrades",
                command,
                "--memory-root",
                str(self.memory_root),
            ]
        )
        if result.returncode != 0:
            raise InstallError(f"semantic upgrade {command} failed with status {result.returncode}")

    def _install_skills(self) -> None:
        for target in self.skills_targets:
            target.mkdir(parents=True, exist_ok=True)
            self._remove_old_loose_reference(
                target,
                "rightmemory-schema.md",
                self.repo_root / "rightmemory" / "reference" / "rightmemory-schema.md",
            )
            self._remove_old_loose_reference(
                target,
                "rightmemory-edit-correction-rules.md",
                self.repo_root / "rightmemory" / "reference" / "RIGHTMEMORY_EDIT_CORRECTION_RULES.md",
            )
            self._install_skill(
                self.repo_root / "skills" / "memory-retriever-cli" / "SKILL.md",
                "memory-retriever",
                target,
            )
            self._install_skill(
                self.repo_root / "skills" / "rightmemory-orchestrator-cli" / "SKILL.md",
                "rightmemory-orchestrator",
                target,
            )
            self._install_skill(
                self.repo_root / "skills" / "rightmemory-auto-orchestrator-cli" / "SKILL.md",
                "rightmemory-auto-orchestrator",
                target,
            )
            self._install_skill(
                self.repo_root / "skills" / "maintain-rightmemory" / "SKILL.md",
                "maintain-rightmemory",
                target,
            )
            self._install_skill(
                self.repo_root / "skills" / "review-agent-guidance-inbox" / "SKILL.md",
                "review-agent-guidance-inbox",
                target,
            )
            self._remove_old_skill("memory-orchestrator", target)
            self._remove_old_skill("memory-curator", target)
            self._remove_old_skill("memory-dreamer", target)
        print(f"  [skip]    generated role skills; {self.mode} mode uses rightmemory")

    def _remove_old_loose_reference(self, target: Path, name: str, canonical: Path) -> None:
        legacy = target / name
        if not os.path.lexists(legacy):
            return
        if legacy.is_symlink() or not legacy.is_file():
            print(f"  [skip]    {legacy} is not a managed regular reference; left untouched")
            return
        legacy_bytes = legacy.read_bytes()
        known_legacy_hash = LEGACY_LOOSE_REFERENCE_SHA256[name]
        if (
            legacy_bytes != canonical.read_bytes()
            and sha256(legacy_bytes.replace(b"\r\n", b"\n")).hexdigest()
            != known_legacy_hash
        ):
            print(f"  [skip]    {legacy} differs from the canonical reference; left untouched")
            return
        legacy.unlink()
        print(f"  [remove]  {legacy}  (superseded loose reference)")

    def _install_skill(self, source: Path, skill_name: str, target: Path) -> None:
        destination = target / skill_name / "SKILL.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        output: list[str] = []
        for line in _read_utf8_lines(source):
            prompt_name = ROLE_PROMPTS.get(line)
            if prompt_name:
                output.extend(_read_utf8_lines(self.repo_root / "rightmemory" / "prompts" / prompt_name))
            else:
                output.append(
                    line.replace("{{MEMORY_ROOT}}", str(self.memory_root)).replace("{{SKILLS_ROOT}}", str(target))
                )
        _write_utf8_lines(destination, output)
        print(f"  [install] {destination}")

    def _remove_old_skill(self, skill_name: str, target: Path) -> None:
        directory = target / skill_name
        skill_file = directory / "SKILL.md"
        if not directory.exists():
            return
        if not skill_file.is_file():
            print(f"  [skip]    {directory} has no SKILL.md; left untouched")
            return
        text = _read_utf8(skill_file)
        managed_layout = sorted(path.name for path in directory.iterdir()) == ["SKILL.md"]
        if skill_name == "memory-orchestrator":
            normalized = text.replace("\r\n", "\n").replace("\r", "\n")
            normalized = LEGACY_MEMORY_ROOT_LINE_PATTERN.sub(LEGACY_MEMORY_ROOT_LINE, normalized, count=1)
            recognized = managed_layout and (
                sha256(normalized.encode("utf-8")).hexdigest() == LEGACY_MEMORY_ORCHESTRATOR_SHA256
            )
        else:
            recognized = managed_layout and bool(
                re.search(rf"(?m)^name:\s*{re.escape(skill_name)}\s*$", text)
            ) and (
                "subagent execution wrapper for RightMemory" in text
            )
        if not recognized:
            print(f"  [skip]    {directory} is not an old RightMemory role skill; left untouched")
            return
        if directory.is_symlink():
            directory.unlink()
        else:
            shutil.rmtree(directory)
        print(f"  [remove]  {directory}")

    def _warn_if_command_not_on_path(self) -> None:
        resolved = shutil.which("rightmemory")
        if not resolved:
            if self.is_windows:
                print(
                    f"  [notice]  rightmemory is installed at {self.runtime_command}, but "
                    f"{self.runtime_bin_dir} is not on PATH for this shell.\n"
                    "            Add it to your user PATH, then restart the agent or terminal:\n\n"
                    f'              $env:Path = "{self.runtime_bin_dir};$env:Path"'
                )
            else:
                print(
                    f"  [notice]  rightmemory is installed at {self.runtime_command}, but ~/.local/bin "
                    "is not on PATH for this shell.\n"
                    "            Add it to your shell profile, then restart the agent or terminal:\n\n"
                    '              export PATH="$HOME/.local/bin:$PATH"\n\n'
                    "            For zsh, a common place is ~/.zshrc. For bash, use ~/.bashrc or ~/.bash_profile."
                )
            return
        if _same_path(Path(resolved), self.runtime_command):
            return
        command = (
            f'$env:Path = "{self.runtime_bin_dir};$env:Path"'
            if self.is_windows
            else 'export PATH="$HOME/.local/bin:$PATH"'
        )
        print(
            f"  [notice]  rightmemory is installed at {self.runtime_command}, but PATH currently resolves "
            f"rightmemory to:\n\n              {resolved}\n\n"
            f"            Put {self.runtime_bin_dir} earlier on PATH, then restart the agent or terminal:\n\n"
            f"              {command}\n\n"
            "            Otherwise the orchestrator may call stale code or use the wrong RIGHTMEMORY_ROOT."
        )

    def _write_install_stamp(self) -> None:
        runtime_dir = self.memory_root / ".runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        runtime_gitignore = runtime_dir / ".gitignore"
        if not runtime_gitignore.is_file():
            _write_utf8(runtime_gitignore, "*\n")
        stamp = (
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            + f"\nmode={self.mode}\nrepo={self.repo_root}\n"
        )
        install_stamp = runtime_dir / "install.stamp"
        _write_utf8(install_stamp, stamp)
        print(f"  [refresh] {install_stamp}")
        print("             running watch processes refresh after their current cycle or sleep check")
        print("             run rightmemory watch start or restart to start newly added watch targets")

    def _print_next_steps(self) -> None:
        separator = "\\" if self.is_windows else "/"
        print()
        print("Done. Next steps:")
        print(
            f"  1. Open {self.memory_root}{separator}MEMORY.md and "
            f"{self.memory_root}{separator}PURSUITS.md and replace the examples with your own state."
        )
        if self.mode == "cli-agent":
            print(
                "  2. Write [agent_cli], [retrieve.agent_cli], and a default writer [update.agent_cli] "
                f"config to {self.memory_root}{separator}rightmemory.toml."
            )
        else:
            print(
                "  2. Write [retrieve.model] and a default writer [update.model] config to "
                f"{self.memory_root}{separator}rightmemory.toml."
            )
        print(
            "  3. Choose memory-retriever for read-only context, rightmemory-orchestrator "
            "for approval-gated orchestration, rightmemory-auto-orchestrator for automatic "
            "orchestration, maintain-rightmemory when you explicitly want the current agent "
            "to edit RightMemory directly, or review-agent-guidance-inbox to review pending guidance."
        )
        print(
            "  4. Optional background transcript review, dreamer, pruning, insight, and sync: "
            "rightmemory watch start"
        )
        print()
        print("Re-run the installer any time you pull updates from the RightMemory repo;")
        print(
            "your existing MEMORY.md, MEMORY_*.md, PURSUITS.md, PURSUIT_*.md, "
            "AGENT_GUIDANCE_INBOX.md, corrections.md, insight_logs/, and pending update queue are preserved."
        )

    def _git(self, *args: str) -> None:
        result = self._git_result(*args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise InstallError(f"git {' '.join(args)} failed in {self.memory_root}: {detail}")

    def _git_capture(self, *args: str) -> str:
        result = self._git_result(*args)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _git_result(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _run(["git", *args], cwd=self.memory_root, capture=True)


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            prepare_command(command),
            cwd=str(cwd) if cwd is not None else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            check=False,
        )
    except OSError as exc:
        raise InstallError(f"could not run {command[0]}: {exc}") from exc


def _read_utf8(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def _read_utf8_lines(path: Path) -> list[str]:
    return _read_utf8(path).splitlines()


def _write_utf8(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(text.encode("utf-8"))
    os.replace(temporary, path)


def _write_utf8_lines(path: Path, lines: list[str]) -> None:
    _write_utf8(path, ("\n".join(lines) + "\n") if lines else "")


def _same_path(left: Path, right: Path) -> bool:
    left_text = str(left.resolve(strict=False))
    right_text = str(right.resolve(strict=False))
    return left_text.casefold() == right_text.casefold() if os.name == "nt" else left_text == right_text


def _posix_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _shell_double_quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def _cmd_literal(value: str) -> str:
    return value.replace("%", "%%")


def _verify_required_commands() -> None:
    for name in ("git", "uv"):
        try:
            result = _run([name, "--version"], capture=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise InstallError(f"missing or unusable required command: {name}: {exc}") from exc
        if result.returncode != 0:
            raise InstallError(f"missing or unusable required command: {name}")


def _resolved_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="install.ps1" if os.name == "nt" else "install.sh",
        usage="%(prog)s [--mode cli-agent|standalone] [<memory-root> <skills-target>]",
    )
    parser.add_argument("--mode", default="standalone")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    if args.mode == "subagent":
        parser.exit(1, "Unsupported --mode: subagent\nUse --mode cli-agent for command-backed agent skill installs.\n")
    if args.mode not in {"cli-agent", "standalone"}:
        parser.error(f"Invalid --mode: {args.mode}")
    if len(args.paths) not in {0, 2}:
        parser.error("provide either no paths or both <memory-root> and <skills-target>")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        _verify_required_commands()
        home = Path.home()
        if args.paths:
            memory_root = _resolved_path(args.paths[0])
            skills_targets = [_resolved_path(args.paths[1])]
        else:
            memory_root = _resolved_path(home / ".rightmemory")
            skills_targets = [
                _resolved_path(home / ".codex" / "skills"),
                _resolved_path(home / ".claude" / "skills"),
            ]
        repo_root = Path(__file__).resolve().parents[1]
        Installer(repo_root, args.mode, memory_root, skills_targets).run()
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
