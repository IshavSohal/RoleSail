"""Dashboard background discovery, pipeline, and tailoring tasks."""

from __future__ import annotations

import copy
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from rolesail.database import get_connection

if TYPE_CHECKING:
    from rolesail.dashboard.http import DashboardHTTPServer

log = logging.getLogger(__name__)
MAX_TAILORING_QUEUE_SIZE = 100


class TailoringQueueFullError(RuntimeError):
    """Raised when the session-only tailoring queue reaches its bound."""


def run_outreach_dispatcher(
    stop_event: threading.Event,
    wake_event: threading.Event,
) -> None:
    """Run the restart-safe local outreach dispatcher."""
    from rolesail.outreach.service import (
        dispatch_due_outreach,
        recover_outreach_dispatcher,
        refresh_inflight_outreach,
    )

    try:
        recover_outreach_dispatcher()
    except Exception:
        log.exception("Could not recover scheduled outreach")
    while not stop_event.is_set():
        try:
            refresh_inflight_outreach()
            dispatch_due_outreach()
        except Exception:
            log.exception("Scheduled outreach dispatcher iteration failed")
        wake_event.wait(30)
        wake_event.clear()


def _execute_discovery(server: DashboardHTTPServer, workers: int) -> None:
    """Run discovery (then enrich + score when LLM is configured)."""
    from rolesail.config import get_tier
    from rolesail.pipeline import _run_discover, _run_enrich, _run_score

    try:
        result = _run_discover(workers=workers)
        if get_tier() >= 2:
            enrich_result = _run_enrich(workers=workers)
            if isinstance(enrich_result, dict) and str(
                enrich_result.get("status", "")
            ).startswith("error"):
                raise RuntimeError(enrich_result["status"])
            score_result = _run_score()
            if isinstance(score_result, dict) and str(
                score_result.get("status", "")
            ).startswith("error"):
                raise RuntimeError(score_result["status"])
            result = {**result, "scored": True}
        else:
            result = {**result, "scored": False}
    except Exception as exc:
        log.exception("Dashboard discovery failed")
        with server.discovery_lock:
            server.discovery_state = {
                **server.discovery_state,
                "status": "error",
                "error": str(exc)[:500],
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        return

    with server.discovery_lock:
        server.discovery_state = {
            **server.discovery_state,
            "status": "complete",
            "result": result,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }


def start_discovery(server: DashboardHTTPServer, workers: int = 3) -> dict:
    """Start one background discovery run, rejecting overlapping runs."""
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 8:
        raise ValueError("Workers must be an integer between 1 and 8")

    with server.discovery_lock:
        if server.discovery_state["status"] == "running":
            return dict(server.discovery_state)
        server.discovery_state = {
            "status": "running",
            "workers": workers,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        state = dict(server.discovery_state)

    server.discovery_pool.submit(_execute_discovery, server, workers)
    return state


def _execute_dashboard_pipeline(server: DashboardHTTPServer, run_id: str, workers: int) -> None:
    """Run the dashboard's one-click discover/enrich/score workflow."""
    from rolesail.pipeline import run_pipeline
    from rolesail.usage import update_run

    try:
        result = run_pipeline(
            stages=["discover", "enrich", "score"],
            workers=workers,
            score_workers=workers,
            run_id=run_id,
        )
        errors = result.get("errors") or {}
        status = "partial" if errors else "complete"
        update_run(run_id, status=status, result=result,
                   error=json.dumps(errors) if errors else None)
    except Exception as exc:
        log.exception("Dashboard pipeline failed")
        update_run(run_id, status="error", error=str(exc))


def start_dashboard_pipeline(server: DashboardHTTPServer, workers: int = 3) -> dict:
    """Start one persistent discover/enrich/score run."""
    from rolesail.usage import create_run, get_run

    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 8:
        raise ValueError("Workers must be an integer between 1 and 8")
    with server.pipeline_lock:
        latest = get_run()
        if latest and latest["status"] == "running":
            raise RuntimeError("A pipeline run is already in progress")
        run = create_run(["discover", "enrich", "score"])
        server.pipeline_pool.submit(_execute_dashboard_pipeline, server, run["id"], workers)
        return run


def _tailoring_target_error(
    target_url: str,
    replace_existing: bool = False,
) -> str | None:
    """Return why a queued job cannot be tailored, or None when eligible."""
    row = get_connection().execute(
        "SELECT applied_at, full_description, tailored_resume_path, "
        "COALESCE(tailor_attempts, 0) AS tailor_attempts "
        "FROM jobs WHERE url = ?",
        (target_url,),
    ).fetchone()
    if not row:
        return "Job not found"
    if row["applied_at"]:
        return "This job is already marked as applied"
    if not row["full_description"]:
        return "This job needs a full description before tailoring"
    if row["tailored_resume_path"] and not replace_existing:
        return "This job already has a tailored resume"
    if row["tailor_attempts"] >= 5:
        return "This job has reached the tailoring attempt limit"
    return None


def _tailoring_status_locked(server: DashboardHTTPServer) -> dict:
    """Build a JSON-safe queue snapshot while ``tailoring_lock`` is held."""
    if server.tailoring_current is not None:
        status = "running"
    elif server.tailoring_queue:
        status = "queued"
    else:
        status = "idle"

    queued = []
    for position, request in enumerate(server.tailoring_queue, start=1):
        queued.append({**copy.deepcopy(request), "queue_position": position})

    return {
        "status": status,
        "current": copy.deepcopy(server.tailoring_current),
        "queued": queued,
        "queue_length": len(queued),
        "recent": copy.deepcopy(list(server.tailoring_recent)),
    }


def tailoring_status(server: DashboardHTTPServer) -> dict:
    """Return the current session-only tailoring queue state."""
    with server.tailoring_lock:
        return _tailoring_status_locked(server)


def _run_tailoring_request(request: dict) -> tuple[str, dict | None, str | None]:
    """Execute one request and convert stale individual jobs into skips."""
    target_url = request["target_url"]
    if target_url:
        reason = _tailoring_target_error(
            target_url,
            request.get("replace_existing", False),
        )
        if reason:
            return "skipped", {"reason": reason}, None

    try:
        from rolesail.scoring.tailor import TailoringCancelled, run_tailoring
        from rolesail.usage import usage_context

        with usage_context(stage="tailor"):
            result = run_tailoring(
                min_score=request["min_score"],
                limit=request["limit"],
                validation_mode=request["validation_mode"],
                target_url=target_url,
                replace_existing=request.get("replace_existing", False),
                cancel_check=lambda: request.get("cancel_requested", False),
            )
        return "complete", result, None
    except TailoringCancelled:
        return "cancelled", None, None
    except Exception as exc:
        log.exception("Dashboard tailoring request failed")
        return "error", None, str(exc)[:500]


def _drain_tailoring_queue(server: DashboardHTTPServer) -> None:
    """Process queued tailoring requests in FIFO order on one worker thread."""
    while True:
        with server.tailoring_lock:
            if server.tailoring_stopping or not server.tailoring_queue:
                server.tailoring_processor_running = False
                server.tailoring_current = None
                return

            request = server.tailoring_queue.popleft()
            request["status"] = "running"
            request["started_at"] = datetime.now(timezone.utc).isoformat()
            server.tailoring_current = request

        try:
            status, result, error = _run_tailoring_request(request)
        except Exception as exc:
            log.exception("Dashboard tailoring queue worker failed")
            status, result, error = "error", None, str(exc)[:500]
        finished_at = datetime.now(timezone.utc).isoformat()

        with server.tailoring_lock:
            request["status"] = status
            request["result"] = result
            request["error"] = error
            request["finished_at"] = finished_at
            server.tailoring_recent.appendleft(copy.deepcopy(request))
            server.tailoring_current = None


def start_tailoring(
    server: DashboardHTTPServer,
    min_score: int = 7,
    limit: int = 20,
    validation_mode: str = "normal",
    target_url: str | None = None,
    replace_existing: bool = False,
) -> dict:
    """Enqueue a tailoring request for the dashboard's single FIFO worker."""
    if not isinstance(min_score, int) or isinstance(min_score, bool) or not 1 <= min_score <= 10:
        raise ValueError("Minimum score must be an integer between 1 and 10")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("Tailoring limit must be an integer between 1 and 100")
    if validation_mode not in {"strict", "normal", "lenient"}:
        raise ValueError("Validation mode must be strict, normal, or lenient")
    if not isinstance(replace_existing, bool):
        raise ValueError("Replace existing must be a boolean")
    if replace_existing and target_url is None:
        raise ValueError("Replace existing is only supported for a single job")
    if target_url is not None:
        if not isinstance(target_url, str) or not target_url.strip():
            raise ValueError("Job URL is required")
        target_url = target_url.strip()
        reason = _tailoring_target_error(target_url, replace_existing)
        if reason:
            raise ValueError(reason)

    with server.tailoring_lock:
        if server.tailoring_stopping:
            raise RuntimeError("The tailoring queue is shutting down")

        outstanding = [
            request
            for request in ([server.tailoring_current] + list(server.tailoring_queue))
            if request is not None
        ]
        if target_url:
            duplicate = next(
                (request for request in outstanding if request["target_url"] == target_url),
                None,
            )
            if duplicate:
                position = 0
                if duplicate["status"] == "queued":
                    position = list(server.tailoring_queue).index(duplicate) + 1
                return {
                    **copy.deepcopy(duplicate),
                    "deduplicated": True,
                    "queue_position": position,
                }
        elif any(request["kind"] == "batch" for request in outstanding):
            raise RuntimeError("A bulk tailoring request is already active or queued")

        if len(server.tailoring_queue) >= MAX_TAILORING_QUEUE_SIZE:
            raise TailoringQueueFullError("The tailoring queue is full")

        request = {
            "id": uuid.uuid4().hex,
            "kind": "job" if target_url else "batch",
            "status": "queued",
            "min_score": min_score,
            "limit": limit,
            "validation_mode": validation_mode,
            "target_url": target_url,
            "replace_existing": replace_existing,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "cancel_requested": False,
        }
        server.tailoring_queue.append(request)
        queue_position = len(server.tailoring_queue)
        should_start_processor = not server.tailoring_processor_running
        if should_start_processor:
            server.tailoring_processor_running = True
        response = {
            **copy.deepcopy(request),
            "deduplicated": False,
            "queue_position": queue_position,
        }

    if should_start_processor:
        try:
            server.tailoring_pool.submit(_drain_tailoring_queue, server)
        except RuntimeError:
            with server.tailoring_lock:
                server.tailoring_processor_running = False
                try:
                    server.tailoring_queue.remove(request)
                except ValueError:
                    pass
            raise
    return response


def cancel_tailoring(server: DashboardHTTPServer, target_url: str) -> dict:
    """Cancel an outstanding single-job tailoring request."""
    if not isinstance(target_url, str) or not target_url.strip():
        raise ValueError("Job URL is required")
    target_url = target_url.strip()

    with server.tailoring_lock:
        current = server.tailoring_current
        if current is not None and current.get("target_url") == target_url:
            current["cancel_requested"] = True
            return {
                "id": current["id"],
                "url": target_url,
                "status": "cancelling",
            }

        queued = next(
            (request for request in server.tailoring_queue if request.get("target_url") == target_url),
            None,
        )
        if queued is not None:
            server.tailoring_queue.remove(queued)
            queued["status"] = "cancelled"
            queued["finished_at"] = datetime.now(timezone.utc).isoformat()
            server.tailoring_recent.appendleft(copy.deepcopy(queued))
            return {
                "id": queued["id"],
                "url": target_url,
                "status": "cancelled",
            }

    raise ValueError("No active tailoring request was found for this job")


def _run_outreach_dispatcher(stop_event: threading.Event, wake_event: threading.Event) -> None:
    """Run the restart-safe local outreach dispatcher."""
    from rolesail.outreach.service import (
        dispatch_due_outreach,
        recover_outreach_dispatcher,
        refresh_inflight_outreach,
    )

    try:
        recover_outreach_dispatcher()
    except Exception:
        log.exception("Could not recover scheduled outreach")
    while not stop_event.is_set():
        try:
            refresh_inflight_outreach()
            dispatch_due_outreach()
        except Exception:
            log.exception("Scheduled outreach dispatcher iteration failed")
        wake_event.wait(30)
        wake_event.clear()
