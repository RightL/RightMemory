"""Durable, authenticated operational history for the Pursuit editor."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from .session import LockedMessageSession, SessionPaths, _ensure_durable_directory


class PursuitJournal:
    def __init__(self, root: Path, root_key: str):
        self.directory = root / ".runtime" / "pursuit-editor"
        self.path = self.directory / "session.json"
        self.root_key = root_key
        self.paths = SessionPaths(root / ".runtime", self.path, self.directory / "session.lock")

    def locked(self) -> LockedMessageSession:
        return LockedMessageSession(self.paths)

    def key(self) -> bytes:
        path = self.directory / "signing-key"
        if not path.exists():
            if self.path.exists():
                raise ValueError("The Pursuit recovery signing key is missing; preserve the recovery directory for review.")
            _ensure_durable_directory(self.directory)
            key_paths = SessionPaths(self.paths.runtime_root, path, self.paths.lock)
            LockedMessageSession(key_paths).save_json(os.urandom(32))
            if os.name != "nt":
                path.chmod(0o600)
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError("The Pursuit recovery signing key is invalid; preserve the recovery directory for review.")
        return key

    def signature(self, record: dict[str, Any]) -> str:
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return hmac.new(self.key(), payload, hashlib.sha256).hexdigest()

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        envelope = json.loads(self.path.read_bytes())
        record = envelope["record"]
        if (not isinstance(record, dict) or record.get("version") != 1
                or record.get("root_key") != self.root_key
                or not hmac.compare_digest(envelope["signature"], self.signature(record))):
            raise ValueError("The Pursuit recovery record is invalid; preserve the recovery directory for review.")
        return record

    def save(self, record: dict[str, Any]) -> None:
        envelope = {"record": record, "signature": self.signature(record)}
        LockedMessageSession(self.paths).save_json(
            json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        )

    def store_files(self, files: dict[str, bytes]) -> dict[str, str]:
        result = {}
        for name, content in files.items():
            digest = hashlib.sha256(content).hexdigest()
            path = self.directory / "blobs" / digest
            if not path.exists():
                # Empty semantic files are valid blobs; the JSON writer rejects
                # empty payloads, so store an exact length-preserving envelope.
                blob_paths = SessionPaths(self.paths.runtime_root, path, self.paths.lock)
                LockedMessageSession(blob_paths).save_json(b"B" + content)
            result[name] = digest
        return result

    def read_files(self, files: dict[str, str]) -> dict[str, bytes]:
        result = {}
        for name, digest in files.items():
            if (len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
                    or Path(name).name != name or name in {".", ".."}):
                raise ValueError("The Pursuit recovery record contains an unsafe file reference.")
            content = (self.directory / "blobs" / digest).read_bytes()
            if not content.startswith(b"B") or hashlib.sha256(content[1:]).hexdigest() != digest:
                raise ValueError("A Pursuit recovery file is damaged; preserve the recovery directory for review.")
            result[name] = content[1:]
        return result
