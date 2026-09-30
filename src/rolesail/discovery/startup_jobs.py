"""Startup-focused discovery through the official Startup Jobs REST API.

The free API exposes recent startup listings and requires attribution. RoleSail
therefore keeps the Startup Jobs listing URL as the canonical URL and records
``Startup Jobs`` as the source shown in the dashboard.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.parse
from datetime import UTC, datetime

from rolesail import config
from rolesail.database import get_connection, init_db, is_job_within_retention_window
from rolesail.discovery.filters import classify_title, reconcile_unscored_jobs
from rolesail.discovery.greenhouse import _http_request, _normalize_description

log = logging.getLogger(__name__)

API_URL = "https://api.startup.jobs/v1/jobs"
SOURCE_NAME = "Startup Jobs"


def _query_terms(search_cfg: dict, source_cfg: dict) -> list[str]:
    """Return a bounded, ordered set of API search terms."""
    configured = source_cfg.get("queries")
    values = configured if isinstance(configured, list) else search_cfg.get("queries", [])
    if not values:
        values = search_cfg.get("include_titles", [])
    terms: list[str] = []
    for item in values:
        value = item.get("query") if isinstance(item, dict) else item
        value = str(value or "").strip()
        if value and value.casefold() not in {term.casefold() for term in terms}:
            terms.append(value)
    max_queries = max(1, int(source_cfg.get("max_queries", 10)))
    return terms[:max_queries]


def _location(job: dict) -> str:
    location = job.get("location") or {}
    parts = []
    if str(job.get("workplace_type") or "").casefold() == "remote":
        parts.append("Remote")
    if isinstance(location, dict):
        parts.extend(
            str(location.get(field)).strip()
            for field in ("city", "state", "country")
            if location.get(field)
        )
    return ", ".join(dict.fromkeys(parts))


def fetch_startup_jobs(
    api_key: str,
    queries: list[str],
    *,
    page_size: int = 50,
    max_pages_per_query: int = 1,
) -> list[dict]:
    """Fetch and normalize recent listings, deduplicating overlapping queries."""
    if not api_key.strip():
        raise ValueError("Startup Jobs API key is required")

    page_size = min(50, max(1, int(page_size)))
    max_pages_per_query = max(1, int(max_pages_per_query))
    jobs: dict[str, dict] = {}
    for query in queries:
        cursor = None
        for _page in range(max_pages_per_query):
            params: dict[str, object] = {"q": query, "limit": page_size}
            if cursor is not None:
                params["starting_after"] = cursor
            payload = json.loads(
                _http_request(
                    f"{API_URL}?{urllib.parse.urlencode(params)}",
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                )
            )
            for item in payload.get("data", []) or []:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                job_id = str(item.get("id") or url)
                if not url or not job_id:
                    continue
                company = item.get("company") or {}
                jobs[job_id] = {
                    "title": item.get("title"),
                    "company": company.get("name") if isinstance(company, dict) else None,
                    "company_logo": company.get("logo_url") if isinstance(company, dict) else None,
                    "location": _location(item),
                    "url": url,
                    # The API requires applications and displayed titles to link
                    # through the Startup Jobs listing page.
                    "application_url": url,
                    "content": item.get("description_html") or "",
                    "salary": item.get("salary"),
                    "posted_at": item.get("published_at"),
                }
            if not payload.get("has_more") or payload.get("next_cursor") is None:
                break
            cursor = payload["next_cursor"]
    return list(jobs.values())


def _has_canonical_duplicate(conn: sqlite3.Connection, job: dict) -> bool:
    """Prefer a direct-employer record already stored for the same role."""
    if not job.get("title") or not job.get("company"):
        return False
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE lower(trim(title)) = lower(trim(?)) "
        "AND lower(trim(company)) = lower(trim(?)) AND strategy != 'startup_jobs_api' LIMIT 1",
        (job["title"], job["company"]),
    ).fetchone()
    return row is not None


def run_startup_jobs_discovery(api_key: str | None = None) -> dict:
    """Discover recent startup roles using the official, attributed API."""
    search_cfg = config.load_search_config()
    source_cfg = search_cfg.get("startup_jobs") or {}
    empty = {
        "found": 0, "kept": 0, "title_rejected": 0, "location_rejected": 0,
        "new": 0, "existing": 0, "errors": 0, "companies": 0,
    }
    if source_cfg.get("enabled", True) is False:
        return {**empty, "skipped": "disabled"}

    api_key = api_key or os.environ.get("STARTUP_JOBS_API_KEY", "")
    if not api_key:
        return {**empty, "skipped": "STARTUP_JOBS_API_KEY is not configured"}

    queries = _query_terms(search_cfg, source_cfg)
    if not queries:
        return {**empty, "skipped": "no search queries configured"}

    try:
        jobs = fetch_startup_jobs(
            api_key,
            queries,
            page_size=source_cfg.get("page_size", 50),
            max_pages_per_query=source_cfg.get("max_pages_per_query", 1),
        )
    except Exception as exc:  # noqa: BLE001 - isolate this external source
        log.error("Startup Jobs API error: %s", exc)
        return {**empty, "errors": 1, "error": str(exc)}

    conn = get_connection()
    reconcile_unscored_jobs(init_db(), search_cfg)
    now = datetime.now(UTC).isoformat()
    stats = {**empty, "found": len(jobs)}
    companies: set[str] = set()
    enforce_location = source_cfg.get(
        "location_filter", config.location_filter_is_mandatory(search_cfg)
    ) or config.location_filter_is_mandatory(search_cfg)

    for job in jobs:
        title = str(job.get("title") or "")
        if not classify_title(title, search_cfg).accepted:
            stats["title_rejected"] += 1
            continue
        location = job.get("location") or None
        if enforce_location and not config.location_is_allowed(location, search_cfg):
            stats["location_rejected"] += 1
            continue
        if not is_job_within_retention_window(job.get("posted_at"), reference_at=now):
            continue
        stats["kept"] += 1
        if _has_canonical_duplicate(conn, job):
            stats["existing"] += 1
            continue

        description = _normalize_description(job.get("content"))
        detail_scraped_at = now if len(description) > 200 else None
        url = job["url"]
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, company, company_logo, salary, description, "
                "location, site, strategy, discovered_at, posted_at, full_description, "
                "application_url, detail_scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    url, title, job.get("company"), job.get("company_logo"),
                    job.get("salary"), description[:500] if description else None,
                    location, SOURCE_NAME, "startup_jobs_api", now, job.get("posted_at"),
                    description if detail_scraped_at else None, url, detail_scraped_at,
                ),
            )
            stats["new"] += 1
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE jobs SET salary = COALESCE(?, salary), "
                "company_logo = COALESCE(?, company_logo), posted_at = COALESCE(posted_at, ?), "
                "full_description = COALESCE(?, full_description), "
                "detail_scraped_at = COALESCE(?, detail_scraped_at) WHERE url = ?",
                (
                    job.get("salary"), job.get("company_logo"), job.get("posted_at"),
                    description if detail_scraped_at else None, detail_scraped_at, url,
                ),
            )
            stats["existing"] += 1
        if job.get("company"):
            companies.add(str(job["company"]))

    conn.commit()
    stats["companies"] = len(companies)
    log.info("Startup Jobs discovery complete: %s", stats)
    return stats
