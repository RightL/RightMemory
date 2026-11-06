"""Measure the local retained text of one curation domain."""

import base64
import hashlib
import os
import stat
from collections.abc import Iterator
from functools import cache
from importlib import metadata, resources
from pathlib import Path

from .learning import DOMAIN_ROLES, LEARNING_DIRECTORY
from .tools import MemoryTools

_ENCODING = "o200k_base"
_TIKTOKEN_VERSION = "0.13.0"
_RANKS_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
# The pinned tiktoken 0.13.0 public definition. See data/NOTICE-o200k_base.txt.
_PATTERN = "|".join((
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
    r"\p{N}{1,3}",
    r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
    r"\s*[\r\n]+",
    r"\s+(?!\S)",
    r"\s+",
))
_SPECIAL_TOKENS = {"<|endoftext|>": 199999, "<|endofprompt|>": 200018}
_BINARY_SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".pdf": (b"%PDF-",),
    ".zip": (b"PK\x03\x04", b"PK\x05\x06"),
    ".gz": (b"\x1f\x8b\x08",),
    ".ico": (b"\x00\x00\x01\x00",),
    ".ogg": (b"OggS\x00",),
    ".flac": (b"fLaC",),
    ".wasm": (b"\x00asm",),
}
_RIFF_TYPES = {".wav": b"WAVE", ".webp": b"WEBP", ".avi": b"AVI "}


@cache
def _encoding():
    from tiktoken import Encoding

    if metadata.version("tiktoken") != _TIKTOKEN_VERSION:
        raise ValueError(f"Retained-text counting requires tiktoken {_TIKTOKEN_VERSION}.")
    rank_data = resources.files("rightmemory").joinpath("data", "o200k_base.tiktoken").read_bytes()
    if hashlib.sha256(rank_data).hexdigest() != _RANKS_SHA256:
        raise ValueError("Bundled o200k_base ranks do not match the expected SHA-256.")
    ranks = {}
    for line in rank_data.splitlines():
        token, rank = line.split()
        ranks[base64.b64decode(token)] = int(rank)
    return Encoding(
        name=_ENCODING,
        pat_str=_PATTERN,
        mergeable_ranks=ranks,
        special_tokens=_SPECIAL_TOKENS,
    )


def _could_contain_owned_files(path: str, selector: MemoryTools) -> bool:
    # Check descendants through the canonical file predicate. A directory can
    # hold Markdown or a skill package even when its own name is not writable.
    probes = ("probe.md", "probe/SKILL.md")
    if path == LEARNING_DIRECTORY:
        probes = ("experiences/probe.md", "guidance/probe.md", "skills/probe/SKILL.md")
    return any(selector._is_allowed_write_path(f"{path}/{probe}") for probe in probes)


def _is_link(status: os.stat_result) -> bool:
    return stat.S_ISLNK(status.st_mode) or bool(
        getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _read_owned_file(path: Path, relative: str, status: os.stat_result) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(status, opened):
                raise ValueError(f"Owned file changed while opening: {relative}")
            return stream.read()
    except OSError as exc:
        raise ValueError(f"Could not read owned file {relative}: {exc.strerror or exc}") from exc


def _owned_files(root: Path, selector: MemoryTools) -> Iterator[tuple[str, bytes]]:
    def visit(directory: Path) -> Iterator[tuple[str, bytes]]:
        try:
            children = sorted(directory.iterdir())
        except OSError as exc:
            raise ValueError(f"Could not list retained text directory {directory}: {exc.strerror or exc}") from exc
        for path in children:
            relative = path.relative_to(root).as_posix()
            owned = selector._is_allowed_write_path(relative)
            may_contain_owned = _could_contain_owned_files(relative, selector)
            if not owned and not may_contain_owned:
                continue
            try:
                status = path.lstat()
            except OSError as exc:
                raise ValueError(f"Could not inspect owned path {relative}: {exc.strerror or exc}") from exc
            if _is_link(status):
                raise ValueError(f"Owned path must not be a link or reparse point: {relative}")
            if stat.S_ISDIR(status.st_mode) and may_contain_owned:
                yield from visit(path)
            elif owned:
                if not stat.S_ISREG(status.st_mode):
                    raise ValueError(f"Owned path must be a regular file: {relative}")
                yield relative, _read_owned_file(path, relative, status)

    yield from visit(root)


def _decode_text(path: str, data: bytes) -> str | None:
    """Return UTF-8 text, or None for a binary asset."""
    suffix = Path(path).suffix.lower()
    is_bin = suffix == ".bin"
    if (
        data.startswith(_BINARY_SIGNATURES.get(suffix, ()))
        or (data.startswith(b"RIFF") and _RIFF_TYPES.get(suffix) == data[8:12])
        or (is_bin and b"\x00" in data)
    ):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if is_bin:
            return None
        raise ValueError(f"Invalid UTF-8 in owned file {path} at byte {exc.start}.") from exc


def count_retained_text(memory_root: Path, domain: str) -> dict:
    """Count each owned UTF-8 file separately with the bundled o200k_base encoding.

    Recognized binary formats and opaque .bin assets are listed by path and byte size.
    This measure counts local file text, including markup, for a curation budget.
    """
    if domain not in DOMAIN_ROLES:
        raise ValueError(f"Unknown curation domain: {domain!r}")
    root = Path(memory_root).absolute()
    try:
        status = root.lstat()
    except OSError as exc:
        raise ValueError(f"Could not inspect Memory root {root}: {exc.strerror or exc}") from exc
    if _is_link(status) or not stat.S_ISDIR(status.st_mode):
        raise ValueError(f"Memory root must be a directory without a link or reparse point: {root}")
    selector = MemoryTools(root, role=DOMAIN_ROLES[domain])
    tokens_by_file = {}
    binary_assets = []
    for relative, data in _owned_files(root, selector):
        text = _decode_text(relative, data)
        if text is None:
            binary_assets.append({"path": relative, "bytes": len(data)})
            continue
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        tokens_by_file[relative] = len(_encoding().encode_ordinary(normalized))
    return {
        "domain": domain,
        "encoding": _ENCODING,
        "retained_text_tokens": sum(tokens_by_file.values()),
        "text_file_count": len(tokens_by_file),
        "tokens_by_text_file": tokens_by_file,
        "binary_assets": binary_assets,
    }
