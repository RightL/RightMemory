from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .learning import learning_files, learning_validation_errors
from .platform import prepare_command


PYTHON_REQUIREMENT = ">=3.11"
MEMORY_GITIGNORE = """\
*
!.gitignore
!MEMORY.md
!MEMORY_*.md
!PURSUITS.md
!PURSUIT_*.md
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
!learning/
!learning/**
"""


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallTarget:
    kind: Literal["new", "existing"]
    has_head: bool
    missing_required: tuple[str, ...]
    invalid_required: tuple[str, ...]
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
        self._require_valid_learning()
        target = self._inspect_target()
        self._print_layout()
        self._require_complete_existing_target(target)
        self._require_installable_tasks(allow_running_configured=True)

        with self._curation_install_boundary() as worker_was_running:
            self._require_installable_tasks()
            if target.kind == "new":
                self._bootstrap_state()
            else:
                self._preserve_existing_state()
            (self.memory_root / "insight_logs").mkdir(parents=True, exist_ok=True)
            self._ensure_memory_git()
            self._install_runtime()
            self._install_skills()
            self._warn_if_command_not_on_path()
            self._write_install_stamp()
        self._restart_curation_worker(worker_was_running)
        self._print_next_steps()

    def _require_valid_learning(self) -> None:
        diagnostics = learning_validation_errors(self.memory_root)
        if diagnostics:
            raise InstallError(
                "RightMemory learning state is invalid:\n- "
                + "\n- ".join(diagnostics)
                + "\ninstallation made no changes; repair the invalid learning state before reinstalling"
            )

    def _require_installable_tasks(self, *, allow_running_configured: bool = False) -> None:
        if not self.memory_root.is_dir():
            return
        from .learning_service import LearningService

        notices = LearningService(self.memory_root).notices()
        unfinished = [
            notice for notice in notices
            if notice.get("task_id") and notice.get("status") in {"active", "resume", "effects_pending"}
            and not (
                allow_running_configured
                and notice.get("executor") == "configured"
                and notice.get("status") in {"active", "effects_pending"}
            )
        ]
        if unfinished:
            tasks = ", ".join(str(notice["task_id"]) for notice in unfinished)
            raise InstallError(
                f"unfinished curation tasks prevent installation: {tasks}; "
                "installation made no runtime changes. Resume or explicitly resolve these tasks "
                "with their current executor before reinstalling or switching modes."
            )

    @contextmanager
    def _curation_install_boundary(self):
        from .curation_worker import CurationWorker

        try:
            with CurationWorker(self.memory_root).install_boundary() as stopped:
                yield stopped
        except TimeoutError as exc:
            raise InstallError(
                "the live RightMemory curation worker did not reach a safe stop boundary; "
                "installation made no runtime changes. Wait for its current task to finish, "
                "then rerun the installer"
            ) from exc

    def _restart_curation_worker(self, worker_was_running: bool) -> None:
        if self.mode == "assistant":
            return
        from .curation_worker import CurationWorker
        from .learning_service import LearningService

        worker = CurationWorker(self.memory_root)
        if not worker_was_running and not worker.has_work(LearningService(self.memory_root).notices()):
            return
        if self.runtime_python is None:
            raise InstallError("installed RightMemory runtime is unavailable for the curation worker")
        worker.wake(python_executable=self.runtime_python)
        print("  [start]   curation worker on the installed runtime")

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
        return InstallTarget(
            kind,
            has_head,
            tuple(sorted(missing)),
            tuple(sorted(invalid)),
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
                "learning",
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
        if target.missing_required or target.invalid_required:
            details: list[str] = []
            if target.missing_required:
                details.append(f"missing required files: {', '.join(target.missing_required)}")
            if target.invalid_required:
                details.append(f"non-regular required files: {', '.join(target.invalid_required)}")
            raise InstallError(
                f"existing RightMemory root is incomplete: {'; '.join(details)}\n"
                "installation made no changes; inspect and repair this root using the current schema before reinstalling"
            )
        from .validation import validate_memory_root

        validation = validate_memory_root(self.memory_root)
        if not validation.ok:
            raise InstallError(
                f"existing RightMemory root is invalid:\n{validation.report}\n"
                "installation made no changes; inspect and repair this root using the current schema before reinstalling"
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
        files.extend(path.relative_to(self.memory_root).as_posix() for path in learning_files(self.memory_root))
        return files

    def _ensure_initial_commit(self) -> None:
        if self._git_result("rev-parse", "--verify", "--quiet", "HEAD").returncode == 0:
            print("  [keep]    initial memory commit already exists")
            return
        files = self._initial_memory_files()
        learning_paths = [path for path in files if path.startswith("learning/")]
        regular_files = [path for path in files if path != ".gitignore" and not path.startswith("learning/")]
        if regular_files:
            self._git("add", "--", *regular_files)
        if learning_paths:
            self._git("add", "-f", "--", *learning_paths)
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
        package = str(self.repo_root)
        if self.mode == "cli-agent":
            package += "[codex-sdk]"
        result = _run(["uv", "pip", "install", "--python", str(self.runtime_python), package])
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

    def _install_skills(self) -> None:
        for target in self.skills_targets:
            target.mkdir(parents=True, exist_ok=True)
            for skill_name in (
                "maintain-rightmemory",
                "maintain-agent-guidance",
                "maintain-work-skills",
            ):
                self._install_skill(
                    self.repo_root / "skills" / skill_name / "SKILL.md",
                    skill_name,
                    target,
                )
            if self.mode == "assistant":
                self._remove_managed_skill(
                    self.repo_root / "skills" / "rightmemory-auto-orchestrator-cli" / "SKILL.md",
                    "rightmemory-auto-orchestrator",
                    target,
                )
                self._install_skill(
                    self.repo_root / "skills" / "rightmemory-assistant" / "SKILL.md",
                    "rightmemory-assistant",
                    target,
                )
                continue
            self._remove_managed_skill(
                self.repo_root / "skills" / "rightmemory-assistant" / "SKILL.md",
                "rightmemory-assistant",
                target,
            )
            for source_directory, skill_name in (
                ("rightmemory-auto-orchestrator-cli", "rightmemory-auto-orchestrator"),
                ("maintain-pursuit-map", "maintain-pursuit-map"),
                ("rightmemory-manager", "rightmemory-manager"),
            ):
                self._install_skill(
                    self.repo_root / "skills" / source_directory / "SKILL.md",
                    skill_name,
                    target,
                )
        print(f"  [skip]    generated role skills; {self.mode} mode uses rightmemory")

    def _install_skill(self, source: Path, skill_name: str, target: Path) -> None:
        destination = target / skill_name / "SKILL.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_utf8_lines(destination, self._render_skill(source))
        print(f"  [install] {destination}")

    def _render_skill(self, source: Path) -> list[str]:
        output: list[str] = []
        for line in _read_utf8_lines(source):
            output.append(line.replace("{{MEMORY_ROOT}}", str(self.memory_root)))
        return output

    def _remove_managed_skill(self, source: Path, skill_name: str, target: Path) -> None:
        directory = target / skill_name
        skill_file = directory / "SKILL.md"
        if not os.path.lexists(directory):
            return
        if directory.is_symlink() or not directory.is_dir():
            print(f"  [skip]    {directory} is not a managed skill directory; left untouched")
            return
        managed_layout = sorted(path.name for path in directory.iterdir()) == ["SKILL.md"]
        expected_lines = self._render_skill(source)
        expected = (("\n".join(expected_lines) + "\n") if expected_lines else "").encode("utf-8")
        if skill_file.is_symlink() or not skill_file.is_file() or not managed_layout:
            print(f"  [skip]    {directory} is not an installer-managed RightMemory skill; left untouched")
            return
        if skill_file.read_bytes() != expected:
            print(f"  [skip]    {directory} differs from the installer-managed skill; left untouched")
            return
        shutil.rmtree(directory)
        print(f"  [remove]  {directory}  (no longer installed by default)")

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
        if self.mode != "assistant":
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
        if self.mode == "assistant":
            print("  2. Enable the installed rightmemory-assistant skill in your assistant.")
            print("     Memory, guidance, and work-skills maintenance skills are installed alongside it.")
            print("     The main agent and native subagents need access to this root and its worktrees.")
            print("     No RightMemory model API or CLI-agent backend configuration is needed.")
            print(f"  3. Check the root: rightmemory validate --root {self.memory_root}")
            print("     Use assistant context, submit --executor native, and curate begin/complete for native work.")
            print("Re-run this installer with --mode assistant and the same paths to update the runtime and skill.")
            return
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
            "  3. The default rightmemory-auto-orchestrator skill retrieves and submits "
            "qualifying evidence automatically according to its instructions."
        )
        print(
            "  4. Open the Pursuit map with rightmemory pursuit and copy node context "
            "into Codex App. Use rightmemory-manager there for requested RightMemory "
            "management or task coordination. Memory, guidance, and work-skills "
            "maintenance are installed as separate explicit skills."
        )
        print(
            "  5. Optional background pruning, insight, and sync: "
            "rightmemory watch start"
        )
        print()
        print("Re-run the installer any time you pull updates from the RightMemory repo;")
        print(
            "your existing MEMORY.md, MEMORY_*.md, PURSUITS.md, PURSUIT_*.md, "
            "learning/, corrections.md, and insight_logs/ are preserved."
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
        usage="%(prog)s [--mode assistant|cli-agent|standalone] [<memory-root> <skills-target>]",
    )
    parser.add_argument("--mode", choices=("assistant", "cli-agent", "standalone"), default="standalone")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    if len(args.paths) not in {0, 2}:
        parser.error("provide either no paths or both <memory-root> and <skills-target>")
    if args.mode == "assistant" and not args.paths:
        parser.error("assistant mode requires explicit <memory-root> and <skills-target> paths")
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
