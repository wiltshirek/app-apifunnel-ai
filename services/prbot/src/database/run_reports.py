"""Run report persistence for PR Bot workflow runs.

Collection: prbot_run_reports

Each document tracks a dispatched workflow run through its lifecycle:
dispatched → queued → in_progress → completed | failed | timed_out | cancelled.

At dispatch time a minimal record is created with a dispatch_id (our own
correlation key, since GitHub's run_id isn't available synchronously).
When the workflow callback arrives, the record is updated with full results
and agent logs are processed into paginated line arrays.
"""

import base64
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SUMMARY_PROJECTION = {
    "_id": 0,
    "log_lines": 0,
    "agent_log_b64": 0,
}

_MAX_LOG_LINES = 50_000


async def save_run_report(report: Dict[str, Any]) -> str:
    """Persist a run report to MongoDB. Returns the inserted document ID."""
    from ..db import get_db

    db = await get_db()

    report["dispatched_at"] = datetime.now(timezone.utc)

    result = await db.prbot_run_reports.insert_one(report)
    doc_id = str(result.inserted_id)
    logger.info(
        "run_report saved: repo=%s dispatch_id=%s status=%s doc_id=%s",
        report.get("repo"), report.get("dispatch_id"),
        report.get("status"), doc_id,
    )
    return doc_id


async def get_run_reports(
    repo: str,
    limit: int = 20,
    skip: int = 0,
) -> List[Dict[str, Any]]:
    """Fetch recent run reports for a repo, newest first.

    Excludes log_lines from the projection to keep list responses lightweight.
    """
    from ..db import get_db

    db = await get_db()

    cursor = db.prbot_run_reports.find(
        {"repo": repo},
        _SUMMARY_PROJECTION,
    ).sort("dispatched_at", -1).skip(skip).limit(limit)

    return await cursor.to_list(length=limit)


async def complete_run_report(
    report_token: str,
    report_data: Dict[str, Any],
) -> bool:
    """Merge workflow results into the dispatched record and clear the token.

    Finds the record by report_token, decodes agent_log_b64 into structured
    log_lines, merges the incoming data, sets completed_at, and clears the
    token so it can't be reused.
    Returns True if a matching record was found and updated.
    """
    from ..db import get_db

    db = await get_db()

    report_data.pop("report_token", None)
    report_data["completed_at"] = datetime.now(timezone.utc)

    _process_agent_logs(report_data)

    result = await db.prbot_run_reports.update_one(
        {"report_token": report_token},
        {
            "$set": report_data,
            "$unset": {"report_token": ""},
        },
    )

    if result.matched_count == 0:
        logger.warning("complete_run_report: no matching report_token found")
        return False

    logger.info(
        "complete_run_report: updated repo=%s run_id=%s log_lines=%s",
        report_data.get("repo"), report_data.get("run_id"),
        report_data.get("log_line_count", 0),
    )
    return True


async def update_run_status(
    report_token: str,
    status: str,
    extra: Optional[Dict[str, Any]] = None,
) -> bool:
    """Apply a partial status update without clearing the token.

    Used for mid-run callbacks (phase 2): status=queued or in_progress.
    """
    from ..db import get_db

    db = await get_db()

    update: Dict[str, Any] = {"status": status}
    if status == "in_progress":
        update["started_at"] = datetime.now(timezone.utc)
    if extra:
        update.update(extra)

    result = await db.prbot_run_reports.update_one(
        {"report_token": report_token},
        {"$set": update},
    )
    return result.matched_count > 0


async def get_run_report(identifier: str) -> Optional[Dict[str, Any]]:
    """Fetch a single run report by dispatch_id or run_id.

    Tries dispatch_id first (dsp_ prefix), falls back to run_id.
    Excludes raw log_lines — use get_run_log_lines() for log access.
    """
    from ..db import get_db

    db = await get_db()

    if identifier.startswith("dsp_"):
        query = {"dispatch_id": identifier}
    else:
        query = {"run_id": identifier}

    doc = await db.prbot_run_reports.find_one(query, _SUMMARY_PROJECTION)
    return doc


async def get_run_report_by_run_id(run_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single run report by GitHub Actions run ID.

    Kept for backward compatibility. Excludes log_lines.
    """
    from ..db import get_db

    db = await get_db()

    doc = await db.prbot_run_reports.find_one(
        {"run_id": run_id},
        _SUMMARY_PROJECTION,
    )
    return doc


async def get_run_log_lines(
    identifier: str,
    page: int = 1,
    page_size: int = 100,
    all_lines: bool = False,
) -> Optional[Dict[str, Any]]:
    """Fetch paginated log lines for a run.

    Returns a dict with pagination metadata and the lines array, or None
    if the run isn't found.

    Args:
        identifier: dispatch_id (dsp_...) or run_id.
        page: 1-indexed page number, or -1 for last page. Ignored when all_lines=True.
        page_size: Lines per page (max 500). Ignored when all_lines=True.
        all_lines: If True, return every log line in one response.
    """
    from ..db import get_db

    db = await get_db()

    if identifier.startswith("dsp_"):
        query = {"dispatch_id": identifier}
    else:
        query = {"run_id": identifier}

    doc = await db.prbot_run_reports.find_one(
        query,
        {
            "_id": 0,
            "dispatch_id": 1,
            "run_id": 1,
            "status": 1,
            "log_lines": 1,
            "log_line_count": 1,
            "log_truncated": 1,
        },
    )
    if not doc:
        return None

    lines = doc.get("log_lines") or []
    total = doc.get("log_line_count", len(lines))

    result: Dict[str, Any] = {
        "dispatch_id": doc.get("dispatch_id"),
        "run_id": doc.get("run_id"),
        "status": doc.get("status"),
        "total_lines": total,
        "truncated": doc.get("log_truncated", False),
    }

    if all_lines:
        result["total_pages"] = None
        result["page"] = None
        result["page_size"] = None
        result["lines"] = lines
    else:
        total_pages = max(1, math.ceil(total / page_size)) if total > 0 else 0

        if page == -1:
            page = total_pages

        page = max(1, min(page, total_pages)) if total_pages > 0 else 1
        start = (page - 1) * page_size
        end = start + page_size

        result["total_pages"] = total_pages
        result["page"] = page
        result["page_size"] = page_size
        result["lines"] = lines[start:end]

    return result


def _process_agent_logs(report_data: Dict[str, Any]) -> None:
    """Decode agent_log_b64 into structured log_lines array.

    Modifies report_data in place: adds log_lines, log_line_count,
    log_truncated. Removes agent_log_b64.
    """
    raw_b64 = report_data.pop("agent_log_b64", None)
    if not raw_b64:
        report_data["log_lines"] = []
        report_data["log_line_count"] = 0
        report_data["log_truncated"] = False
        return

    try:
        decoded = base64.b64decode(raw_b64).decode("utf-8", errors="replace")
    except Exception:
        logger.warning("Failed to decode agent_log_b64, storing empty logs")
        report_data["log_lines"] = []
        report_data["log_line_count"] = 0
        report_data["log_truncated"] = False
        return

    raw_lines = decoded.splitlines()
    truncated = len(raw_lines) > _MAX_LOG_LINES
    if truncated:
        raw_lines = raw_lines[-_MAX_LOG_LINES:]

    completed_at = report_data.get("completed_at")
    ts = completed_at.isoformat() if isinstance(completed_at, datetime) else str(completed_at) if completed_at else None

    log_lines = []
    for i, text in enumerate(raw_lines, start=1):
        log_lines.append({"n": i, "t": ts, "text": text})

    report_data["log_lines"] = log_lines
    report_data["log_line_count"] = len(log_lines)
    report_data["log_truncated"] = truncated


async def ensure_indexes() -> None:
    """Create required indexes on prbot_run_reports.

    Safe to call multiple times — create_index is idempotent.
    """
    from ..db import get_db

    db = await get_db()
    coll = db.prbot_run_reports

    await coll.create_index(
        "dispatch_id",
        unique=True,
        partialFilterExpression={"dispatch_id": {"$exists": True}},
        name="dispatch_id_unique_sparse",
    )
    await coll.create_index(
        [("repo", 1), ("dispatched_at", -1)],
        name="repo_dispatched_at",
    )
    logger.info("prbot_run_reports indexes ensured")
