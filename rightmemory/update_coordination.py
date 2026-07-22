from __future__ import annotations

import re


UPDATE_QUEUE_CANDIDATE_PATH_RE = re.compile(
    r"update_queue/candidates/[0-9a-f]{32}\.json"
)
UPDATE_QUEUE_RECOVERY_PATH_RE = re.compile(
    r"update_queue/recovery/update-batch-[0-9a-f]{64}\.json"
)
UPDATE_REVIEW_PATH_RE = re.compile(
    r"update_reviews/review-[0-9a-f]{64}\.md"
)


def is_update_coordination_path(path: str) -> bool:
    """Return whether a tracked path is non-semantic Update coordination state."""
    return path == "update_queue/lease.json" or bool(
        UPDATE_QUEUE_CANDIDATE_PATH_RE.fullmatch(path)
        or UPDATE_QUEUE_RECOVERY_PATH_RE.fullmatch(path)
        or UPDATE_REVIEW_PATH_RE.fullmatch(path)
    )
