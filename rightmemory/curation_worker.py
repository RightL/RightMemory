"""One configured worker for the shared candidate lifecycle.

This module stays standard-library-only so installation can stop the current
worker before replacing its Python environment. Candidates live in the shared
store, never in this process registry.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .platform import (
    detached_process_kwargs, lock_file, lock_file_nonblocking, process_command,
    process_exists, process_identity, python_module_child_env, unlock_file,
)


MAX_AUTOMATIC_ATTEMPTS = 2


def default_executor(memory_root: Path) -> str:
    stamp = Path(memory_root) / ".runtime" / "install.stamp"
    if stamp.is_symlink():
        raise ValueError("install stamp must be a regular file")
    if stamp.is_file() and "mode=assistant" in stamp.read_text(encoding="utf-8").splitlines():
        return "native"
    return "configured"


def wake_configured_curation(memory_root: Path) -> dict | None:
    """Pick up new or interrupted work after synchronization without running a model here."""
    if default_executor(memory_root) != "configured":
        return None
    from .learning_service import LearningService
    worker = CurationWorker(memory_root)
    if worker.has_work(LearningService(memory_root).notices()):
        return worker.wake()
    return None


class CurationWorker:
    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root).expanduser().resolve()
        self.root = self.memory_root / ".runtime" / "curation-worker"

    def status(self) -> dict:
        state = self._read()
        return {
            "live": self._live(state), "pid": state.get("pid"),
            "error": state.get("error"), "failures": state.get("failures", {}),
            "next_ready_at": state.get("next_ready_at"),
            "log_path": str(self.root / "worker.log"),
        }

    def has_work(self, notices: list[dict]) -> bool:
        failures = self._read().get("failures", {})
        return any((item.get("ready") or item.get("next_ready_at")) and item.get("executor") != "native"
                   and not self._blocked(failures.get(item["domain"]), item.get("task_id"))
                   for item in notices)

    @staticmethod
    def _blocked(failure: dict | None, task_id: str | None) -> bool:
        return bool(failure and failure.get("task_id") == task_id
                    and failure.get("attempts", 0) >= MAX_AUTOMATIC_ATTEMPTS)

    def wake(self, *, python_executable: str | Path | None = None, retry_failed: bool = False) -> dict:
        with self._locked("control.lock"):
            state = self._read()
            if retry_failed:
                state["failures"] = {}
                state["error"] = None
            state["wake"] = state.get("wake", 0) + 1
            self._write(state)
            if self._live(state):
                return {"status": "running", "pid": state["pid"]}
            try:
                with self._locked("install.lock", blocking=False):
                    return self._start(state, python_executable)
            except BlockingIOError:
                return {"status": "deferred", "reason": "runtime installation is in progress"}

    def _start(self, state: dict, python_executable: str | Path | None) -> dict:
        executable = str(python_executable or sys.executable)
        command = [executable, "-m", "rightmemory.curation_worker", "--root", str(self.memory_root)]
        path = self.root / "worker.log"
        self._check(path)
        with path.open("ab") as log:
            child = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=python_module_child_env(), **detached_process_kwargs(),
            )
        state.update(pid=child.pid, identity=process_identity(child.pid))
        state.pop("stop", None)
        self._write(state)
        return {"status": "started", "pid": child.pid}

    def stop(self, *, timeout_seconds: float = 45) -> bool:
        with self._locked("control.lock"):
            state = self._read()
            if not self._live(state):
                return False
            state["stop"] = {"pid": state["pid"], "identity": state.get("identity")}
            self._write(state)
        deadline = time.monotonic() + timeout_seconds
        while self.status()["live"]:
            if time.monotonic() >= deadline:
                raise TimeoutError("curation worker has not reached a safe batch boundary")
            time.sleep(0.05)
        return True

    @contextmanager
    def install_boundary(self, *, timeout_seconds: float = 45):
        # Wake observes this lock without waiting for a long installation.
        # The worker checks stop between domain tasks, preserving in-flight work.
        with self._locked("install.lock"):
            stopped = self.stop(timeout_seconds=timeout_seconds)
            yield stopped

    def run(self, service=None) -> int:
        if service is None:
            from .learning_service import LearningService
            service = LearningService(self.memory_root)
        # A successor can arrive while its predecessor releases the leader lock.
        with self._locked("leader.lock"):
            with self._locked("control.lock"):
                state = self._read()
                if self._stop_requested(state):
                    self._finish(state)
                    return 0
                state.update(pid=os.getpid(), identity=process_identity(os.getpid()))
                self._write(state)
            try:
                while True:
                    with self._locked("control.lock"):
                        state = self._read()
                        observed_wake = state.get("wake", 0)
                        if self._stop_requested(state):
                            return 0
                    progressed = False
                    notices = service.notices()
                    for notice in notices:
                        domain = notice["domain"]
                        if not notice.get("ready") or notice.get("executor") == "native":
                            continue
                        with self._locked("control.lock"):
                            state = self._read()
                            if self._stop_requested(state):
                                return 0
                            failure = state.get("failures", {}).get(domain)
                            if self._blocked(failure, notice.get("task_id")):
                                continue
                        error = None
                        outcome = None
                        try:
                            result = service.run(
                                domain, mode=notice.get("mode", "update"), scope=notice.get("scope", ""),
                            )
                            outcome = result.get("status")
                            if outcome in {"completed", "completed_elsewhere"}:
                                with self._locked("control.lock"):
                                    state = self._read()
                                    state.setdefault("failures", {}).pop(domain, None)
                                    self._write(state)
                                progressed = True
                                continue
                            if outcome in {"active", "not_ready"}:
                                continue
                            error = result.get("reason") or f"curation returned {outcome}: {result.get('sync', '')}"
                        except Exception as exc:
                            error = f"{type(exc).__name__}: {exc}"
                        # Bind the budget to the current task. Automatic replacement
                        # after a stale base carries the same budget forward; an
                        # explicit new task starts with its own budget.
                        latest = next((item for item in service.notices() if item["domain"] == domain), notice)
                        task_id = latest.get("task_id")
                        with self._locked("control.lock"):
                            state = self._read()
                            failures = state.setdefault("failures", {})
                            prior = failures.get(domain, {})
                            continued = prior.get("task_id") == task_id or (
                                outcome == "retry_ready" and prior.get("task_id") == notice.get("task_id")
                            )
                            attempts = prior.get("attempts", 0) if continued else 0
                            failures[domain] = {"task_id": task_id, "attempts": attempts + 1, "error": error}
                            self._write(state)
                            progressed = attempts + 1 < MAX_AUTOMATIC_ATTEMPTS or progressed
                        print(f"{domain}: {error}", flush=True)
                    notices = service.notices()
                    with self._locked("control.lock"):
                        state = self._read()
                        failures = state.get("failures", {})
                        state["error"] = "; ".join(f"{key}: {value['error']}" for key, value in failures.items()) or None
                        self._write(state)
                        if state.get("wake", 0) != observed_wake:
                            progressed = True
                        deadline = None
                        if not progressed:
                            deadlines = [
                                datetime.fromisoformat(item["next_ready_at"])
                                for item in notices
                                if item.get("next_ready_at") and item.get("executor") != "native"
                                and not self._blocked(failures.get(item["domain"]), item.get("task_id"))
                            ]
                            deadline = min(deadlines) if deadlines else None
                            if deadline is None:
                                self._finish(state)
                                return 1 if failures else 0
                        state["next_ready_at"] = None if deadline is None else deadline.isoformat()
                        self._write(state)
                    if deadline is not None:
                        self._wait_for_work(deadline, observed_wake)
                    elif failures:
                        time.sleep(0.1)
            finally:
                with self._locked("control.lock"):
                    state = self._read()
                    if state.get("pid") == os.getpid():
                        self._finish(state)

    def _wait_for_work(self, deadline: datetime, observed_wake: int) -> None:
        """Wait without model work; new submissions and installation can interrupt."""
        while True:
            with self._locked("control.lock"):
                state = self._read()
                if self._stop_requested(state) or state.get("wake", 0) != observed_wake:
                    return
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def _finish(self, state: dict) -> None:
        state.pop("pid", None)
        state.pop("identity", None)
        state.pop("stop", None)
        state.pop("next_ready_at", None)
        self._write(state)

    def _stop_requested(self, state: dict) -> bool:
        stop = state.get("stop")
        return isinstance(stop, dict) and stop.get("pid") == os.getpid() and (
            stop.get("identity") is None or stop["identity"] == process_identity(os.getpid())
        )

    def _live(self, state: dict) -> bool:
        pid = state.get("pid")
        if type(pid) is not int or pid <= 0 or not process_exists(pid):
            return False
        identity = state.get("identity")
        if identity is not None:
            return process_identity(pid) == identity
        command = process_command(pid)
        return command is not None and "rightmemory.curation_worker" in command

    @contextmanager
    def _locked(self, name: str, *, blocking: bool = True):
        path = self.root / name
        self._check(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            (lock_file if blocking else lock_file_nonblocking)(handle)
            try:
                yield
            finally:
                unlock_file(handle)

    def _check(self, path: Path) -> None:
        current = self.memory_root
        for part in path.relative_to(current).parts:
            current /= part
            if current.is_symlink():
                raise ValueError(f"curation worker path must not be a symlink: {current}")

    def _read(self) -> dict:
        path = self.root / "state.json"
        self._check(path)
        if not path.exists():
            return {}
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or type(state.get("wake", 0)) is not int or state.get("wake", 0) < 0:
            raise ValueError("invalid curation worker state")
        failures = state.get("failures", {})
        if not isinstance(failures, dict) or any(
            domain not in {"memory", "guidance", "skills"} or not isinstance(value, dict)
            or type(value.get("attempts")) is not int or value["attempts"] < 0
            or not isinstance(value.get("error"), str)
            or (value.get("task_id") is not None and not isinstance(value["task_id"], str))
            for domain, value in failures.items()
        ):
            raise ValueError("invalid curation worker retry state")
        return state

    def _write(self, state: dict) -> None:
        path = self.root / "state.json"
        self._check(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(state, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    return CurationWorker(args.root).run()


if __name__ == "__main__":
    raise SystemExit(main())
