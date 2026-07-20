from __future__ import annotations

import argparse
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

from .platform import prepare_command


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
!MEMORY.md
!MEMORY_*.md
!PURSUITS.md
!PURSUIT_*.md
!PURSUIT_RULES.md
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
"""
ROLE_PROMPTS = {
    "{{ROLE_PROMPT_RETRIEVE}}": "retrieve.md",
    "{{ROLE_PROMPT_UPDATE}}": "update.md",
    "{{ROLE_PROMPT_DREAMER}}": "dreamer.md",
    "{{ROLE_PROMPT_REVIEWER}}": "reviewer.md",
}


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallTarget:
    kind: Literal["new", "existing"]
    has_head: bool
    missing_required: tuple[str, ...]
    invalid_required: tuple[str, ...]


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
        has_head = self._target_has_head()
        semantic_state_exists = self._semantic_state_exists()
        kind: Literal["new", "existing"] = "existing" if has_head or semantic_state_exists else "new"
        missing: list[str] = []
        invalid: list[str] = []
        if kind == "existing":
            for name in ("MEMORY.md", "PURSUITS.md", "PURSUIT_RULES.md"):
                path = self.memory_root / name
                if not os.path.lexists(path):
                    missing.append(name)
                elif path.is_symlink() or not path.is_file():
                    invalid.append(name)
        return InstallTarget(kind, has_head, tuple(sorted(missing)), tuple(sorted(invalid)))

    def _target_has_head(self) -> bool:
        if not self.memory_root.is_dir() or not os.path.lexists(self.memory_root / ".git"):
            return False
        result = _run(
            ["git", "-C", str(self.memory_root), "rev-parse", "--verify", "--quiet", "HEAD"],
            capture=True,
        )
        return result.returncode == 0

    def _semantic_state_exists(self) -> bool:
        if not self.memory_root.is_dir():
            return False

        for path in self.memory_root.iterdir():
            name = path.name
            if name in {
                "MEMORY.md",
                "PURSUITS.md",
                "PURSUIT_RULES.md",
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
            shared_view_files = {"view.md", "retriever.md", "recipe.toml", "question.toml"}
            for view_dir in shared_views.iterdir():
                if view_dir.is_dir() and any(os.path.lexists(view_dir / name) for name in shared_view_files):
                    return True
        return False

    def _require_complete_existing_target(self, target: InstallTarget) -> None:
        if target.kind != "existing" or (not target.missing_required and not target.invalid_required):
            return
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
        rules_file = self.memory_root / "PURSUIT_RULES.md"
        shutil.copyfile(self.repo_root / "MEMORY.example.md", memory_file)
        shutil.copyfile(self.repo_root / "PURSUITS.example.md", pursuits_file)
        shutil.copyfile(self.repo_root / "PURSUIT_RULES.md", rules_file)
        print(f"  [new]     {memory_file}  (from MEMORY.example.md)")
        print(f"  [new]     {pursuits_file}  (from PURSUITS.example.md)")
        print(f"  [new]     {rules_file}")

    def _preserve_existing_state(self) -> None:
        for name in ("MEMORY.md", "PURSUITS.md", "PURSUIT_RULES.md"):
            print(f"  [keep]    {self.memory_root / name} already exists")

    def _ensure_memory_git(self) -> None:
        git_marker = self.memory_root / ".git"
        if git_marker.exists():
            print(f"  [keep]    {self.memory_root} is already a git repo")
        else:
            self._git("init", "-q")
            print(f"  [new]     git init in {self.memory_root}")
        self._ensure_git_author()
        _write_utf8(self.memory_root / ".gitignore", MEMORY_GITIGNORE)
        print(f"  [refresh] {self.memory_root / '.gitignore'}  (memory allowlist)")
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

        add("MEMORY.md")
        files.extend(path.name for path in sorted(self.memory_root.glob("MEMORY_*.md")) if path.is_file())
        add("PURSUITS.md")
        files.extend(
            path.name
            for path in sorted(self.memory_root.glob("PURSUIT_*.md"))
            if path.is_file() and path.name != "PURSUIT_RULES.md"
        )
        add("PURSUIT_RULES.md")
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
        return files

    def _ensure_initial_commit(self) -> None:
        if self._git_result("rev-parse", "--verify", "--quiet", "HEAD").returncode == 0:
            print("  [keep]    initial memory commit already exists")
            return
        files = self._initial_memory_files()
        if files:
            self._git("add", "--", *files)
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
                f'{call}"{python}" -m rightmemory.cli %*\n'
                "exit /b %ERRORLEVEL%\n"
            )
        else:
            text = (
                "#!/usr/bin/env sh\n"
                "export PYTHONUTF8=1\n"
                f'export RIGHTMEMORY_ROOT="{_shell_double_quoted(str(self.memory_root))}"\n'
                f'exec "{_shell_double_quoted(str(self.runtime_python))}" -m rightmemory.cli "$@"\n'
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
            schema = target / "rightmemory-schema.md"
            shutil.copyfile(self.repo_root / "skills" / "rightmemory-schema.md", schema)
            print(f"  [install] {schema}")
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
            self._remove_old_skill("memory-orchestrator", target)
            self._remove_old_skill("memory-curator", target)
            self._remove_old_skill("memory-dreamer", target)
        print(f"  [skip]    generated role skills; {self.mode} mode uses rightmemory")

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
            "  3. Choose memory-retriever for read-only context or rightmemory-orchestrator "
            "for conditional retrieval and unified updates."
        )
        print(
            "  4. Optional background transcript review, update review, dreamer, pruning, insight, and sync: "
            "rightmemory watch start"
        )
        print()
        print("Re-run the installer any time you pull updates from the RightMemory repo;")
        print(
            "your existing MEMORY.md, MEMORY_*.md, PURSUITS.md, PURSUIT_*.md, "
            "PURSUIT_RULES.md, corrections.md, and insight_logs/ are preserved."
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
