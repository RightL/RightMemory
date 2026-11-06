"""Independent retained-text budgets for maintenance publication."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_curation_token_limits


class CurationBudgetExceededError(ValueError):
    """Maintenance retained text exceeds its independently configured limit."""


def domain_budget(
    memory_root: Path, domain: str, *, state_root: Path | None = None,
) -> dict[str, Any]:
    from .retained_text import count_retained_text

    limit = load_curation_token_limits(state_root or memory_root).for_domain(domain)
    count = count_retained_text(memory_root, domain)
    remaining = limit - count["retained_text_tokens"]
    return {
        **count, "token_limit": limit, "remaining_tokens": remaining,
        "over_limit": remaining < 0,
    }


def require_domain_budget(
    memory_root: Path, domain: str, *, state_root: Path | None = None,
) -> dict[str, Any]:
    budget = domain_budget(memory_root, domain, state_root=state_root)
    if budget["over_limit"]:
        raise CurationBudgetExceededError(
            f"{domain} retains {budget['retained_text_tokens']:,} tokens, exceeding its "
            f"{budget['token_limit']:,}-token limit by {-budget['remaining_tokens']:,}. "
            "Delete lower-value material or compact retained content before completing maintenance. "
            "The active state and pending evidence have not been replaced by this result."
        )
    return budget
