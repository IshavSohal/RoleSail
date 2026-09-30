"""Discovery adapters for public Ashby and Lever job boards."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import yaml

from rolesail import config
from rolesail.config import CONFIG_DIR
from rolesail.database import get_connection, init_db, is_job_within_retention_window
from rolesail.discovery.filters import classify_title, reconcile_unscored_jobs
from rolesail.discovery.greenhouse import (
    _http_request,
    _normalize_description,
)

log = logging.getLogger(__name__)


def _load_companies(filename: str) -> dict:
    """Load an ATS company registry from the package config directory."""
    path = CONFIG_DIR / filename
    if not path.exists():
        log.warning("%s not found at %s", filename, path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    companies = data.get("companies", {})
    return companies if isinstance(companies, dict) else {}


def load_ashby_companies() -> dict:
    """Load configured Ashby job boards."""
    return _load_companies("ashby_companies.yaml")


def load_lever_companies() -> dict:
    """Load configured Lever job boards."""
    return _load_companies("lever_companies.yaml")


def _location_name(value: object) -> str:
    """Return a useful display value for an ATS location object or string."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    if value.get("location"):
        return str(value["location"]).strip()
    address = value.get("address") or value.get("postalAddress") or {}
    if isinstance(address, dict) and isinstance(address.get("postalAddress"), dict):
        address = address["postalAddress"]
    if not isinstance(address, dict):
        return ""
    return ", ".join(
        str(address[field]).strip()
        for field in ("addressLocality", "addressRegion", "addressCountry")
        if address.get(field)
    )


def _join_locations(primary: object, secondary: object = None) -> str:
    values = [_location_name(primary)]
    if isinstance(secondary, list):
        values.extend(_location_name(item) for item in secondary)
    return "; ".join(dict.fromkeys(value for value in values if value))


def _ashby_salary(job: dict) -> str | None:
    compensation = job.get("compensation")
    if not isinstance(compensation, dict):
        return None
    value = (
        compensation.get("compensationTierSummary")
        or compensation.get("scrapeableCompensationSalarySummary")
    )
    return str(value) if value else None


def _lever_salary(job: dict) -> str | None:
    salary = job.get("salaryRange")
    if not isinstance(salary, dict):
        return None
    minimum, maximum = salary.get("min"), salary.get("max")
    if minimum is None and maximum is None:
        return None
    currency = str(salary.get("currency") or "").strip()
    interval = str(salary.get("interval") or "").replace("per-", "").replace("-salary", "")
    amount = " - ".join(f"{value:,}" for value in (minimum, maximum) if value is not None)
    return " ".join(part for part in (currency, amount, interval) if part)


def _lever_description(job: dict) -> str:
    """Assemble all text sections exposed by the Lever Postings API.

    ``descriptionPlain`` only contains the opening/body. Lever publishes
    requirements, responsibilities, benefits, compensation, and similar
    sections separately in ``lists``.
    """
    parts: list[str] = []

    description = job.get("descriptionPlain") or job.get("description")
    if normalized := _normalize_description(description):
        parts.append(normalized)

    for section in job.get("lists") or []:
        if not isinstance(section, dict):
            continue
        heading = _normalize_description(section.get("text"))
        content = _normalize_description(section.get("content"))
        section_text = "\n".join(part for part in (heading, content) if part)
        if section_text:
            parts.append(section_text)

    salary_description = (
        job.get("salaryDescriptionPlain") or job.get("salaryDescription")
    )
    if normalized := _normalize_description(salary_description):
        parts.append(normalized)

    additional = job.get("additionalPlain") or job.get("additional")
    if normalized := _normalize_description(additional):
        parts.append(normalized)

    return "\n\n".join(parts)


def fetch_ashby_jobs(company: dict) -> list[dict]:
    """Fetch and normalize all listed postings from one public Ashby board."""
    board = str(company.get("board") or company.get("board_name") or "").strip()
    if not board:
        raise ValueError("Ashby company is missing its board slug")
    params = urllib.parse.urlencode({"includeCompensation": "true"})
    url = f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(board)}?{params}"
    payload = json.loads(_http_request(url, headers={"Accept": "application/json"}))
    jobs = []
    for item in payload.get("jobs", []) or []:
        if item.get("isListed") is False:
            continue
        description = item.get("descriptionPlain") or item.get("descriptionHtml") or ""
        jobs.append({
            "title": item.get("title"),
            "location": _join_locations(item.get("location"), item.get("secondaryLocations")),
            "url": item.get("jobUrl"),
            "application_url": item.get("applyUrl") or item.get("jobUrl"),
            "content": description,
            "salary": _ashby_salary(item),
            "posted_at": item.get("publishedAt"),
        })
    return jobs


def fetch_lever_jobs(company: dict) -> list[dict]:
    """Fetch and normalize every published posting from one Lever site."""
    site = str(company.get("site") or company.get("site_name") or "").strip()
    if not site:
        raise ValueError("Lever company is missing its site slug")
    region = str(company.get("region", "global")).lower()
    if region not in {"global", "eu"}:
        raise ValueError("Lever region must be 'global' or 'eu'")
    origin = "https://api.eu.lever.co" if region == "eu" else "https://api.lever.co"
    limit = max(1, int(company.get("page_size", 100)))
    jobs: dict[str, dict] = {}
    skip = 0
    while True:
        params = urllib.parse.urlencode({"mode": "json", "skip": skip, "limit": limit})
        payload = json.loads(
            _http_request(
                f"{origin}/v0/postings/{urllib.parse.quote(site)}?{params}",
                headers={"Accept": "application/json"},
            )
        )
        if not isinstance(payload, list):
            raise TypeError("Lever postings response was not a list")
        for item in payload:
            job_id = str(item.get("id") or item.get("hostedUrl") or "")
            if not job_id:
                continue
            categories = item.get("categories") or {}
            locations = categories.get("allLocations") or [categories.get("location")]
            created_at = item.get("createdAt")
            posted_at = None
            if created_at is not None:
                try:
                    posted_at = datetime.fromtimestamp(int(created_at) / 1000, UTC).isoformat()
                except (TypeError, ValueError, OverflowError):
                    pass
            content = _lever_description(item)
            jobs[job_id] = {
                "title": item.get("text"),
                "location": _join_locations(None, locations),
                "url": item.get("hostedUrl"),
                "application_url": item.get("applyUrl") or item.get("hostedUrl"),
                "content": content,
                "salary": _lever_salary(item),
                "posted_at": posted_at,
            }
        if len(payload) < limit:
            break
        skip += limit
    return list(jobs.values())


ATS_FETCHERS = {"ashby": fetch_ashby_jobs, "lever": fetch_lever_jobs}


def _process_company(
    key: str,
    company: dict,
    provider: str,
    search_cfg: dict,
    location_filter: bool,
) -> dict:
    """Fetch, filter, and persist one ATS company's public postings."""
    name = company.get("name", key)
    result = {"company": name, "found": 0, "kept": 0, "title_rejected": 0,
              "location_rejected": 0, "new": 0, "existing": 0, "error": None}
    try:
        raw_jobs = ATS_FETCHERS[provider](company)
    except Exception as exc:  # noqa: BLE001 - isolate failures to one external board
        log.error("%s: %s adapter error: %s", name, provider, exc)
        result["error"] = str(exc)
        return result

    result["found"] = len(raw_jobs)
    now = datetime.now(UTC).isoformat()
    conn = get_connection()
    for job in raw_jobs:
        title = job.get("title") or ""
        location = job.get("location") or None
        if not classify_title(title, search_cfg).accepted:
            result["title_rejected"] += 1
            continue
        enforce_location = location_filter or config.location_filter_is_mandatory(search_cfg)
        if enforce_location and not config.location_is_allowed(location, search_cfg):
            result["location_rejected"] += 1
            continue
        url = job.get("url") or ""
        if not url:
            continue
        if not is_job_within_retention_window(job.get("posted_at"), reference_at=now):
            continue
        description = _normalize_description(job.get("content"))
        detail_scraped_at = now if len(description) > 200 else None
        row = (
            url, title or None, name, job.get("salary"),
            description[:500] if description else None, location, name,
            f"{provider}_api", now, job.get("posted_at"),
            description if detail_scraped_at else None,
            job.get("application_url") or url, detail_scraped_at,
        )
        result["kept"] += 1
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, company, salary, description, location, site, "
                "strategy, discovered_at, posted_at, full_description, application_url, "
                "detail_scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            result["new"] += 1
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE jobs SET posted_at = COALESCE(posted_at, ?), "
                "salary = COALESCE(?, salary), description = COALESCE(?, description), "
                "full_description = COALESCE(?, full_description), "
                "application_url = COALESCE(?, application_url), "
                "detail_scraped_at = COALESCE(?, detail_scraped_at) WHERE url = ?",
                (
                    job.get("posted_at"), job.get("salary"),
                    description[:500] if description else None,
                    description if detail_scraped_at else None,
                    job.get("application_url"), detail_scraped_at, url,
                ),
            )
            result["existing"] += 1
    conn.commit()
    return result


def run_ats_discovery(provider: str, companies: dict | None = None, workers: int = 3) -> dict:
    """Discover jobs for all configured companies on an ATS provider."""
    if provider not in ATS_FETCHERS:
        raise ValueError(f"Unsupported ATS provider: {provider}")
    if companies is None:
        companies = load_ashby_companies() if provider == "ashby" else load_lever_companies()
    if not companies:
        return {"found": 0, "kept": 0, "title_rejected": 0, "location_rejected": 0,
                "new": 0, "existing": 0, "errors": 0, "companies": 0}

    search_cfg = config.load_search_config()
    reconcile_unscored_jobs(init_db(), search_cfg)
    location_filter = search_cfg.get(
        f"{provider}_location_filter",
        search_cfg.get("greenhouse_location_filter", True),
    )
    grand = {"found": 0, "kept": 0, "title_rejected": 0, "location_rejected": 0,
             "new": 0, "existing": 0, "errors": 0, "companies": len(companies)}

    def add_result(result: dict) -> None:
        for field in ("found", "kept", "title_rejected", "location_rejected", "new", "existing"):
            grand[field] += result[field]
        grand["errors"] += bool(result["error"])

    started = time.time()
    items = list(companies.items())
    if workers > 1 and len(items) > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
            futures = [
                pool.submit(
                    _process_company, key, company, provider, search_cfg, location_filter,
                )
                for key, company in items
            ]
            for future in as_completed(futures):
                add_result(future.result())
    else:
        for key, company in items:
            add_result(_process_company(
                key, company, provider, search_cfg, location_filter,
            ))
    log.info("%s crawl done in %.0fs: %s", provider.title(), time.time() - started, grand)
    return grand


def run_ashby_discovery(companies: dict | None = None, workers: int = 3) -> dict:
    """Discover jobs from configured Ashby boards."""
    return run_ats_discovery("ashby", companies, workers)


def run_lever_discovery(companies: dict | None = None, workers: int = 3) -> dict:
    """Discover jobs from configured Lever boards."""
    return run_ats_discovery("lever", companies, workers)
