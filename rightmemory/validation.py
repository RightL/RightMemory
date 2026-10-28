from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tools import MemoryTools


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: tuple[str, ...]
    report: str
    memory_report: str | None
    document_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "report": self.report,
            "memory_report": self.memory_report,
            "document_errors": list(self.document_errors),
        }


def validate_memory_root(memory_root: Path) -> ValidationResult:
    """Run the complete standalone validation boundary without a model or writes."""
    # Sync also consumes the root document contract. Import lazily so this
    # validator can be used by publication boundaries without an import cycle.
    from .sync import REQUIRED_ROOT_DOCUMENTS

    root = Path(memory_root).expanduser().resolve()
    document_errors = [
        f"required root document is not a regular file: {name}"
        for name in REQUIRED_ROOT_DOCUMENTS
        if (root / name).is_symlink() or not (root / name).is_file()
    ]
    corrections_path = root / "corrections.md"
    if corrections_path.is_symlink() or (
        corrections_path.exists() and not corrections_path.is_file()
    ):
        document_errors.append("optional root document is not a regular file: corrections.md")

    memory_report = None
    errors = list(document_errors)
    if not document_errors:
        try:
            memory_report = MemoryTools(root, role="sync-reconciler").validate_memory()
        except (OSError, UnicodeError) as exc:
            errors.append(f"cannot read RightMemory documents: {exc}")
        else:
            if not memory_report.startswith("validation passed:"):
                # MemoryTools owns validation and its original report remains
                # available to callers; expose the same diagnostics as data.
                diagnostics = memory_report.removeprefix("validation failed:\n- ")
                errors.extend(diagnostics.split("\n- "))

    ok = not errors
    report = (
        memory_report
        if ok
        else "validation failed:\n" + "\n".join(f"- {error}" for error in errors)
    )
    assert report is not None
    return ValidationResult(
        ok=ok,
        errors=tuple(errors),
        report=report,
        memory_report=memory_report,
        document_errors=tuple(document_errors),
    )
