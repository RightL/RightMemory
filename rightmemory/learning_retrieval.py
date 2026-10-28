"""Task-facing sources from the separate learning collection."""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

from .learning import learning_curated_files, list_learning_candidates


LEARNING_SOURCE_RE = re.compile(
    r"^L#(?:[A-Za-z0-9_.-]|%[0-9A-Fa-f]{2})+(?:/(?:[A-Za-z0-9_.-]|%[0-9A-Fa-f]{2})+)*$"
)


@dataclass(frozen=True)
class LearningSource:
    source_id: str
    path: Path
    kind: str
    text: str
    summary: str
    whole_only: bool = False
    submitted_at: str = ""


@dataclass(frozen=True)
class LearningSearchPage:
    sources: tuple[LearningSource, ...]
    total: int
    next_offset: int | None


@dataclass(frozen=True)
class LearningSourcePage:
    source: LearningSource
    text: str
    offset: int
    start_line: int
    total_chars: int
    next_offset: int | None


def is_learning_source_id(source_id: str) -> bool:
    return bool(LEARNING_SOURCE_RE.fullmatch(source_id)) and all(
        part not in {".", ".."} for part in unquote(source_id[2:]).split("/")
    )


def learning_sources(memory_root: Path) -> dict[str, LearningSource]:
    """Return real, regular sources; processed receipts are never retrieval content."""
    root = Path(memory_root).resolve()
    sources: dict[str, LearningSource] = {}
    for candidate in list_learning_candidates(root):
        relative = f"candidates/{candidate.candidate_id}.json"
        source_id = "L#" + quote(relative, safe="/-._")
        sources[source_id] = LearningSource(
            source_id,
            root / "learning" / relative,
            "interaction evidence",
            _evidence_text(candidate.session_id, candidate.submitted_at, candidate.evidence),
            _summary(candidate.evidence),
            whole_only=True,
            submitted_at=candidate.submitted_at,
        )
    for path in learning_curated_files(root):
        path = Path(path)
        if not path.is_absolute():
            path = root / path
        if path.suffix.lower() != ".md":
            continue
        relative = path.relative_to(root / "learning").as_posix()
        source_id = "L#" + quote(relative, safe="/-._")
        if not is_learning_source_id(source_id):
            raise ValueError(f"unsupported learning source path: {relative}")
        text = path.read_text(encoding="utf-8")
        kind = (
            "work skill" if relative.startswith("skills/") and path.name == "SKILL.md"
            else "work experience" if relative.startswith("experiences/")
            else "guidance or case" if relative.startswith("guidance/")
            else "skill resource"
        )
        sources[source_id] = LearningSource(
            source_id, path, kind, text, _summary(text), whole_only=kind == "work skill"
        )
    return dict(sorted(sources.items()))


def get_learning_source(memory_root: Path, source_id: str) -> LearningSource:
    if not is_learning_source_id(source_id):
        raise ValueError("invalid learning source id")
    source = learning_sources(memory_root).get(source_id)
    if source is None:
        raise ValueError(f"unknown learning source: {source_id}")
    return source


def read_learning_page(
    memory_root: Path, source_id: str, *, offset: int = 0, limit: int = 12_000,
) -> LearningSourcePage:
    """Bound source inspection even when evidence contains one very long line."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12_000:
        raise ValueError("learning reads require a nonnegative offset and a limit from 1 to 12000 characters")
    source = get_learning_source(memory_root, source_id)
    text = source.text[offset:offset + limit]
    end = offset + len(text)
    return LearningSourcePage(
        source, text, offset, source.text.count("\n", 0, offset) + 1, len(source.text),
        end if end < len(source.text) else None,
    )


def search_learning_sources(
    memory_root: Path, query: str, *, offset: int = 0, limit: int = 20,
) -> LearningSearchPage:
    """Search every source by literal terms; an empty query browses the collection."""
    if not isinstance(query, str):
        raise ValueError("learning search query must be text")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("learning search requires a nonnegative offset and a limit from 1 to 50")
    terms = tuple(dict.fromkeys(query.casefold().split()))
    ordered = _ordered_sources(learning_sources(memory_root).values())
    ranked = []
    for position, source in enumerate(ordered):
        haystack = (source.source_id + "\n" + source.text).casefold()
        matched = sum(term in haystack for term in terms)
        if matched or not terms:
            ranked.append((-matched, position, source))
    ranked.sort(key=lambda item: item[:2])
    selected = tuple(item[2] for item in ranked[offset:offset + limit])
    next_offset = offset + len(selected)
    return LearningSearchPage(selected, len(ranked), next_offset if next_offset < len(ranked) else None)


def format_learning_index(memory_root: Path, *, max_chars: int = 12_000) -> str:
    root = Path(memory_root).resolve()
    sources = learning_sources(root)
    if not sources:
        return ""
    lines = ["# Available learning material", ""]
    used = sum(len(line) + 1 for line in lines)
    shown = 0
    for source in _ordered_sources(sources.values()):
        relative = source.path.relative_to(root).as_posix()
        line = f"- `{source.source_id}` ({source.kind}; {relative}): {source.summary}"
        if used + len(line) + 220 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if shown < len(sources):
        lines.extend([
            "", f"Showing {shown} of {len(sources)} sources. Search the complete collection with "
            "search_learning(query, offset, limit), or use native file search under learning/. "
            "An empty query browses all sources.",
        ])
    return "\n".join(lines)


def learning_search_excerpt(source: LearningSource, query: str) -> str:
    compact = " ".join(source.text.split())
    terms = query.casefold().split()
    positions = [compact.casefold().find(term) for term in terms]
    found = [position for position in positions if position >= 0]
    start = max(0, min(found) - 80) if found else 0
    excerpt = compact[start:start + 240]
    return ("..." if start else "") + excerpt + ("..." if start + len(excerpt) < len(compact) else "")


def _ordered_sources(sources: Iterable[LearningSource]) -> list[LearningSource]:
    # Stable material stays discoverable in the compact directory; rough cases
    # follow newest first. All sources remain available through paged search.
    ordered = sorted(sources, key=lambda source: source.source_id)
    ordered.sort(key=lambda source: source.submitted_at, reverse=True)
    ordered.sort(key=lambda source: source.kind == "interaction evidence")
    return ordered


def _evidence_text(session_id: str, submitted_at: str, evidence: str) -> str:
    return (
        "Interaction evidence; its implications may still be unsettled.\n"
        f"Session: {session_id}\nSubmitted: {submitted_at}\n\n{evidence}"
    )


def _summary(text: str) -> str:
    # This is a navigational excerpt, never an inferred rule or relevance filter.
    compact = " ".join(text.split())
    return compact if len(compact) <= 240 else compact[:237] + "..."
