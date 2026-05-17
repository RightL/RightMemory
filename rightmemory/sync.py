from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import SyncConfig
from .session import _ensure_runtime_gitignore, _fsync_directory


MEMORY_SYNC_PATHS = ("MEMORY.md", "MEMORY_*.md", "dream_logs/*.md")
GIT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class SyncResult:
    status: str
    message: str
    files: list[str] = field(default_factory=list)

    def context_block(self) -> str:
        files = ", ".join(self.files) if self.files else "none"
        return "\n".join(
            (
                "<rightmemory_sync>",
                "authority: This Runtime sync context is authoritative for the current caller message. "
                "Ignore older Runtime sync context blocks from prior message history.",
                f"status: {self.status}",
                f"message: {self.message}",
                f"files: {files}",
                "</rightmemory_sync>",
            )
        )


class SyncManager:
    def __init__(self, config: SyncConfig):
        self.config = config
        self.memory_root = Path(config.memory_root)
        self.state_path = self.memory_root / ".runtime" / "sync" / "state.json"

    def preflight(self) -> SyncResult:
        if not self.config.enabled:
            return SyncResult("disabled", "sync disabled")

        upstream = self._upstream()
        if upstream is None:
            return self._record_failure(SyncResult("unconfigured", "sync unconfigured"))

        conflicted = self._conflicted_memory_files()
        if conflicted:
            return self._record_failure(SyncResult("conflict", "memory sync conflict", conflicted))

        dirty = self._dirty_memory_files()
        if dirty:
            return self._record_failure(SyncResult("dirty", "local memory has uncommitted changes", dirty))

        fetch = self._git("fetch")
        if fetch.returncode != 0:
            return self._record_failure(SyncResult("offline", "sync offline: git fetch failed"))

        ahead_behind = self._ahead_behind(upstream)
        if ahead_behind is None:
            return self._record_failure(SyncResult("error", "sync failed: could not compare upstream"))
        ahead, behind = ahead_behind
        if behind == 0:
            return self._record_success("pull", SyncResult("synced", "local memory is current"))

        if ahead == 0:
            merge = self._git("merge", "--ff-only", upstream)
            if merge.returncode == 0:
                return self._record_success("pull", SyncResult("synced", "local memory fast-forwarded"))
            return self._record_failure(SyncResult("error", "sync failed: fast-forward merge failed"))

        merge = self._git("merge", "--no-edit", upstream)
        if merge.returncode == 0:
            return self._record_success("pull", SyncResult("synced", "local and remote memory merged"))

        conflicted = self._conflicted_memory_files()
        if conflicted:
            return self._record_failure(SyncResult("conflict", "memory sync conflict", conflicted))
        return self._record_failure(SyncResult("error", "sync failed: merge failed"))

    def push(self) -> SyncResult:
        if not self.config.enabled:
            return SyncResult("disabled", "sync disabled")

        upstream = self._upstream()
        if upstream is None:
            return self._record_failure(SyncResult("unconfigured", "sync unconfigured"))
        push_target = self._push_target(upstream)
        if push_target is None:
            return self._record_failure(SyncResult("unconfigured", "sync upstream is not pushable"))

        conflicted = self._conflicted_memory_files()
        if conflicted:
            return self._record_failure(SyncResult("conflict", "memory sync conflict", conflicted))

        dirty = self._dirty_memory_files()
        if dirty:
            return self._record_failure(SyncResult("dirty", "local memory has uncommitted changes", dirty))

        push = self._push(push_target)
        if push.returncode == 0:
            return self._record_success("push", SyncResult("pushed", "local memory pushed"))

        fetch = self._git("fetch")
        if fetch.returncode != 0:
            return self._record_failure(SyncResult("offline", "sync offline: git fetch failed"))

        merge = self._git("merge", "--no-edit", upstream)
        if merge.returncode != 0:
            conflicted = self._conflicted_memory_files()
            if conflicted:
                return self._record_failure(SyncResult("conflict", "memory sync conflict", conflicted))
            return self._record_failure(SyncResult("error", "sync failed: merge failed"))

        retry = self._push(push_target)
        if retry.returncode == 0:
            return self._record_success("push", SyncResult("pushed", "local memory merged and pushed"))
        return self._record_failure(SyncResult("error", "sync failed: git push failed"))

    def background_pull(self) -> SyncResult:
        if self.config.enabled:
            last_pull = self._last_successful_pull_at()
            stale_after = timedelta(hours=self.config.stale_pull_after_hours)
            if last_pull is not None and datetime.now(UTC) - last_pull < stale_after:
                return SyncResult("fresh", "last successful pull is fresh")
        return self.preflight()

    def conflict_message(self, result: SyncResult) -> str:
        if result.status != "conflict":
            return result.message
        files = ", ".join(result.files) if result.files else "memory files"
        return f"{result.message}; resolve conflict markers in {files}"

    def _upstream(self) -> str | None:
        if not self._is_git_repo():
            return None
        result = self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if result.returncode != 0:
            return None
        upstream = result.stdout.strip()
        return upstream or None

    def _is_git_repo(self) -> bool:
        if not self.memory_root.exists():
            return False
        result = self._git("rev-parse", "--is-inside-work-tree")
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _ahead_behind(self, upstream: str) -> tuple[int, int] | None:
        result = self._git("rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if result.returncode != 0:
            return None
        parts = result.stdout.split()
        if len(parts) != 2:
            return None
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None

    def _push_target(self, upstream: str) -> tuple[str, str] | None:
        if "/" not in upstream:
            return None
        remote, branch = upstream.split("/", 1)
        if not remote or not branch:
            return None
        return remote, branch

    def _push(self, target: tuple[str, str]) -> subprocess.CompletedProcess[str]:
        remote, branch = target
        return self._git("push", remote, f"HEAD:{branch}")

    def _dirty_memory_files(self) -> list[str]:
        result = self._git("status", "--porcelain", "--", *MEMORY_SYNC_PATHS)
        if result.returncode != 0:
            return []
        return _porcelain_paths(result.stdout)

    def _conflicted_memory_files(self) -> list[str]:
        result = self._git("diff", "--name-only", "--diff-filter=U", "--", *MEMORY_SYNC_PATHS)
        if result.returncode != 0:
            return []
        return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())

    def _last_successful_pull_at(self) -> datetime | None:
        value = self._read_state().get("last_successful_pull_at")
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _record_success(self, operation: str, result: SyncResult) -> SyncResult:
        now = datetime.now(UTC).isoformat()
        state = self._read_state()
        state[f"last_successful_{operation}_at"] = now
        state["last_status"] = result.status
        state["last_message"] = result.message
        state["last_files"] = result.files
        state.pop("last_failure_at", None)
        state.pop("last_failure_status", None)
        state.pop("last_failure_message", None)
        state.pop("last_failure_files", None)
        self._write_state(state)
        return result

    def _record_failure(self, result: SyncResult) -> SyncResult:
        state = self._read_state()
        state["last_status"] = result.status
        state["last_message"] = result.message
        state["last_files"] = result.files
        state["last_failure_at"] = datetime.now(UTC).isoformat()
        state["last_failure_status"] = result.status
        state["last_failure_message"] = result.message
        state["last_failure_files"] = result.files
        self._write_state(state)
        return result

    def _read_state(self) -> dict[str, object]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _write_state(self, data: dict[str, object]) -> None:
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.state_path)
        _fsync_directory(self.state_path.parent)

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "true"
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.memory_root,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))
        except subprocess.TimeoutExpired as exc:
            stdout = _timeout_output(exc.stdout)
            stderr = _timeout_output(exc.stderr)
            message = f"git command timed out after {GIT_TIMEOUT_SECONDS} seconds"
            if stderr:
                message = f"{stderr}\n{message}"
            return subprocess.CompletedProcess(["git", *args], 124, stdout, message)


def _porcelain_paths(output: str) -> list[str]:
    files: set[str] = set()
    for line in output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        if path:
            files.add(path)
    return sorted(files)


def _timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
