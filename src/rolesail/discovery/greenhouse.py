"""Direct-employer discovery for Greenhouse and proprietary career sites.

Greenhouse exposes a free, unauthenticated JSON board API per company:

    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

When `content=true`, every posting is returned with its full HTML description,
location, departments, offices, and an `absolute_url` that doubles as the apply
URL -- so no enrichment call (and no LLM tokens) are required.

Greenhouse companies are loaded from `config/greenhouse_companies.yaml`.
Employers with proprietary career systems are loaded from
`config/bigtech_companies.yaml`. Both use the filters in `searches.yaml`.
"""

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import yaml

from rolesail import config
from rolesail.config import CONFIG_DIR
from rolesail.database import get_connection, init_db, is_job_within_retention_window
from rolesail.discovery.filters import classify_title, reconcile_unscored_jobs

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
API_BASE = "https://boards-api.greenhouse.io/v1/boards"


# -- Company registry from YAML ---------------------------------------------

def load_companies() -> dict:
    """Load Greenhouse company registry from config/greenhouse_companies.yaml."""
    path = CONFIG_DIR / "greenhouse_companies.yaml"
    if not path.exists():
        log.warning("greenhouse_companies.yaml not found at %s", path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("companies", {})


def load_bigtech_companies() -> dict:
    """Load companies backed by proprietary career-site adapters."""
    path = CONFIG_DIR / "bigtech_companies.yaml"
    if not path.exists():
        log.warning("bigtech_companies.yaml not found at %s", path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("companies", {})


# -- Filtering helpers -------------------------------------------------------

def _load_location_filter(search_cfg: dict | None = None) -> tuple[list[str], list[str]]:
    """Load location accept/reject lists from search config."""
    if search_cfg is None:
        search_cfg = config.load_search_config()
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    return accept, reject


def _location_ok(
    location: str | None,
    accept: list[str],
    reject: list[str],
    search_cfg: dict | None = None,
) -> bool:
    """Check if a job location passes the user's location filter."""
    policy = dict(search_cfg or {})
    policy.setdefault("location_accept", accept)
    policy.setdefault("location_reject_non_remote", reject)
    return config.location_is_allowed(location, policy)


def _greenhouse_location(
    job: dict,
    accept: list[str],
    reject: list[str],
    search_cfg: dict,
    enforce_filter: bool,
) -> tuple[bool, str | None]:
    """Resolve a Greenhouse display location against its structured offices.

    Some boards use a broad label such as ``London, Montreal, Singapore`` even
    though Greenhouse also supplies country-qualified office locations. When
    filtering is enabled, retain only the eligible offices so a valid Canadian
    or US option is not rejected because the same role is offered elsewhere.
    """
    loc_obj = job.get("location") or {}
    display_location = loc_obj.get("name") if isinstance(loc_obj, dict) else None
    if not enforce_filter or _location_ok(
        display_location, accept, reject, search_cfg
    ):
        return True, display_location

    eligible_offices: list[str] = []
    for office in job.get("offices") or []:
        if not isinstance(office, dict):
            continue
        office_location = office.get("location")
        office_city = str(office_location or "").split(",", 1)[0].strip().casefold()
        if (
            office_location
            and (
                not display_location
                or office_city in display_location.casefold()
            )
            and office_location not in eligible_offices
            and _location_ok(office_location, accept, reject, search_cfg)
        ):
            eligible_offices.append(office_location)

    if eligible_offices:
        return True, "; ".join(eligible_offices)
    return False, display_location


def _load_query_terms(search_cfg: dict | None = None) -> list[str]:
    """Pull each `query` from `searches.yaml` and lowercase for substring matching.

    Returns the list of raw query strings (e.g. "software engineer", "backend
    developer"). Used for case-insensitive substring matching against job titles.
    """
    if search_cfg is None:
        search_cfg = config.load_search_config()
    queries = search_cfg.get("queries", [])
    terms = []
    for q in queries:
        if isinstance(q, dict) and q.get("query"):
            terms.append(str(q["query"]).lower().strip())
        elif isinstance(q, str):
            terms.append(q.lower().strip())
    return [t for t in terms if t]


def _load_excluded_titles(search_cfg: dict | None = None) -> list[str]:
    """Load the `exclude_titles` list from search config."""
    if search_cfg is None:
        search_cfg = config.load_search_config()
    excludes = search_cfg.get("exclude_titles", [])
    return [str(e).lower().strip() for e in excludes if e]


def _title_matches(title: str | None, terms: list[str], excludes: list[str]) -> bool:
    """Case-insensitive substring match: title must contain any query term and
    must not contain any excluded phrase.
    """
    policy = {
        "include_titles": terms,
        "exclude_titles": excludes,
    }
    return classify_title(title, policy).accepted


# -- HTTP fetch --------------------------------------------------------------

from rolesail.discovery.shared import _http_get_json, _normalize_description


def fetch_company_jobs(board_token: str) -> list[dict]:
    """Fetch all jobs for a Greenhouse board, with descriptions inlined.

    Returns the raw `jobs` list from the API (each item has keys like `id`,
    `title`, `absolute_url`, `location`, `content`, `updated_at`, `departments`,
    `offices`, `metadata`).
    """
    url = f"{API_BASE}/{board_token}/jobs?content=true"
    data = _http_get_json(url)
    return data.get("jobs", []) or []


# -- Description normalization ----------------------------------------------



# -- Proprietary career-site adapters ----------------------------------------

from rolesail.discovery.bigtech import (
    _fetch_amazon_jobs,
    _fetch_apple_jobs,
    _fetch_google_jobs,
    _fetch_ibm_jobs,
    _fetch_linkedin_jobs,
    _fetch_meta_jobs,
    _fetch_microsoft_job_details,
    _fetch_microsoft_jobs,
    _fetch_netflix_jobs,
    _fetch_successfactors_jobs,
    fetch_amazon_job,  # noqa: F401 - compatibility import
)

BIGTECH_FETCHERS = {
    "google": _fetch_google_jobs,
    "amazon": _fetch_amazon_jobs,
    "apple": _fetch_apple_jobs,
    "meta": _fetch_meta_jobs,
    "microsoft": _fetch_microsoft_jobs,
    "netflix": _fetch_netflix_jobs,
    "ibm": _fetch_ibm_jobs,
    "linkedin": _fetch_linkedin_jobs,
    "successfactors": _fetch_successfactors_jobs,
}


# -- Per-company processing --------------------------------------------------

def _process_company(
    key: str,
    company: dict,
    terms: list[str],
    excludes: list[str],
    accept_locs: list[str],
    reject_locs: list[str],
    search_cfg: dict,
    location_filter: bool,
) -> dict:
    """Fetch + filter + store jobs for one Greenhouse company."""
    name = company.get("name", key)
    token = company.get("board_token", key)
    result = {"company": name, "found": 0, "kept": 0, "title_rejected": 0,
              "location_rejected": 0, "new": 0, "existing": 0, "error": None}

    try:
        raw_jobs = fetch_company_jobs(token)
    except Exception as e:
        log.error("%s: API error: %s", name, e)
        result["error"] = str(e)
        return result

    result["found"] = len(raw_jobs)
    log.info("%s: %d total postings", name, len(raw_jobs))

    if not raw_jobs:
        return result

    now = datetime.now(timezone.utc).isoformat()
    rows: list[tuple] = []

    for job in raw_jobs:
        title = job.get("title") or ""

        if not classify_title(title, search_cfg).accepted:
            result["title_rejected"] += 1
            continue
        enforce_location = location_filter or config.location_filter_is_mandatory(search_cfg)
        location_accepted, location = _greenhouse_location(
            job,
            accept_locs,
            reject_locs,
            search_cfg,
            enforce_location,
        )
        if not location_accepted:
            result["location_rejected"] += 1
            continue

        url = job.get("absolute_url") or ""
        if not url:
            continue

        full_description = _normalize_description(job.get("content"))
        short_desc = full_description[:500] if full_description else None

        detail_scraped_at = now if full_description and len(full_description) > 200 else None
        full_for_db = full_description if detail_scraped_at else None

        rows.append((
            url,
            title or None,
            name,
            None,  # salary -- Greenhouse doesn't expose this on the boards API
            short_desc,
            location,
            name,
            "greenhouse_api",
            now,
            full_for_db,
            url,
            detail_scraped_at,
        ))

    result["kept"] = len(rows)

    conn = get_connection()
    new = 0
    existing = 0
    for row in rows:
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, company, salary, description, location, site, "
                "strategy, discovered_at, full_description, application_url, "
                "detail_scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1
    conn.commit()

    result["new"] = new
    result["existing"] = existing
    log.info(
        "%s: %d found, %d title-rejected, %d location-rejected, %d kept -> %d new, %d dupes",
        name, result["found"], result["title_rejected"], result["location_rejected"],
        len(rows), new, existing,
    )
    return result


def _process_bigtech_company(
    key: str,
    company: dict,
    terms: list[str],
    excludes: list[str],
    accept_locs: list[str],
    reject_locs: list[str],
    search_cfg: dict,
    location_filter: bool,
) -> dict:
    """Fetch, normalize, filter, and store one proprietary career site."""
    name = company.get("name", key)
    provider = company.get("provider", key)
    result = {
        "company": name,
        "found": 0,
        "kept": 0,
        "title_rejected": 0,
        "location_rejected": 0,
        "new": 0,
        "existing": 0,
        "error": None,
    }
    fetcher = BIGTECH_FETCHERS.get(provider)
    if not fetcher:
        result["error"] = f"unsupported provider: {provider}"
        return result

    try:
        raw_jobs = fetcher(company, terms)
    except Exception as e:
        log.error("%s: careers adapter error: %s", name, e)
        result["error"] = str(e)
        return result

    result["found"] = len(raw_jobs)
    now = datetime.now(timezone.utc).isoformat()
    rows: list[tuple[tuple, str, bool]] = []
    for job in raw_jobs:
        title = job.get("title") or ""
        location = job.get("location") or None
        if not classify_title(title, search_cfg).accepted:
            result["title_rejected"] += 1
            continue
        enforce_location = location_filter or config.location_filter_is_mandatory(search_cfg)
        if enforce_location and not _location_ok(location, accept_locs, reject_locs, search_cfg):
            result["location_rejected"] += 1
            continue
        url = job.get("url") or ""
        if not url:
            continue
        if not is_job_within_retention_window(job.get("posted_at"), reference_at=now):
            continue

        # Microsoft's JobPosting JSON-LD omits Overview. Fetch the complete
        # structured HTML only after filtering, so rejected search results do
        # not incur an unnecessary detail request.
        if provider == "microsoft" and not job.get("content_is_full"):
            _fetch_microsoft_job_details(job)

        description = _normalize_description(job.get("content"))
        content_is_full = job.get("content_is_full", True)
        full_description = description if content_is_full else ""
        detail_scraped_at = now if len(full_description) > 200 else None
        row = (
            url,
            title or None,
            name,
            job.get("salary"),
            description[:500] if description else None,
            location,
            name,
            f"{provider}_careers",
            now,
            job.get("posted_at"),
            full_description if detail_scraped_at else None,
            job.get("application_url") or url,
            detail_scraped_at,
        )
        rows.append((row, description, content_is_full))

    result["kept"] = len(rows)
    conn = get_connection()
    for row, description, content_is_full in rows:
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, company, salary, description, location, site, "
                "strategy, discovered_at, posted_at, full_description, application_url, "
                "detail_scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            result["new"] += 1
        except sqlite3.IntegrityError:
            # Older versions stored Apple's search-result summary as the full
            # description and marked the job enriched. Make those rows pending
            # again, without overwriting a genuinely enriched description.
            if not content_is_full:
                conn.execute(
                    "UPDATE jobs SET full_description = NULL, detail_scraped_at = NULL "
                    "WHERE url = ? AND full_description = ?",
                    (row[0], description),
                )
            elif provider in {"amazon", "microsoft"}:
                # These APIs supply complete, authoritative descriptions.
                # Refresh rows created by older versions that stored only a
                # partial description and therefore skipped enrichment.
                conn.execute(
                    "UPDATE jobs SET salary = COALESCE(?, salary), description = ?, "
                    "full_description = ?, application_url = COALESCE(?, application_url), "
                    "detail_scraped_at = ? WHERE url = ?",
                    (row[3], row[4], row[10], row[11], row[12], row[0]),
                )
            elif provider == "successfactors":
                # A dashboard upload may have enriched the posting before the
                # employer adapter sees it. Preserve that enrichment and score,
                # but replace generic upload metadata with authoritative fields
                # from the SuccessFactors result row.
                conn.execute(
                    "UPDATE jobs SET title = ?, company = ?, site = ?, strategy = ?, "
                    "description = COALESCE(description, ?), "
                    "posted_at = COALESCE(posted_at, ?), "
                    "location = COALESCE(?, location) WHERE url = ?",
                    (
                        row[1], row[2], row[6], row[7], row[4], row[9],
                        row[5], row[0],
                    ),
                )
            conn.execute(
                "UPDATE jobs SET posted_at = COALESCE(posted_at, ?), "
                "location = COALESCE(?, location) WHERE url = ?",
                (row[9], row[5], row[0]),
            )
            result["existing"] += 1
    conn.commit()
    log.info(
        "%s: %d found, %d title-rejected, %d location-rejected, %d kept -> %d new, %d dupes",
        name,
        result["found"],
        result["title_rejected"],
        result["location_rejected"],
        result["kept"],
        result["new"],
        result["existing"],
    )
    return result


# -- Public entry point ------------------------------------------------------

def run_greenhouse_discovery(
    companies: dict | None = None,
    workers: int = 3,
) -> dict:
    """Main entry point for Greenhouse-based discovery.

    Loads the company registry from `config/greenhouse_companies.yaml` (or uses
    the provided dict), then loads search queries + location filters from the
    user's `searches.yaml` and pulls every matching posting in a single API
    call per company.

    Args:
        companies: Override the company registry. If None, loads from YAML.
        workers: Number of parallel threads for company scraping. Default 3.

    Returns:
        Dict with stats: found, kept, new, existing, errors, companies.
    """
    if companies is None:
        companies = load_companies()

    if not companies:
        log.warning("No Greenhouse companies configured. Create config/greenhouse_companies.yaml.")
        return {"found": 0, "kept": 0, "new": 0, "existing": 0, "errors": 0, "companies": 0}

    search_cfg = config.load_search_config()
    reconcile_unscored_jobs(init_db(), search_cfg)
    terms = _load_query_terms(search_cfg)
    excludes = _load_excluded_titles(search_cfg)
    accept_locs, reject_locs = _load_location_filter(search_cfg)
    location_filter = search_cfg.get("greenhouse_location_filter", True)

    log.info("Greenhouse crawl: %d companies | %d query terms | %d excludes | workers=%d",
             len(companies), len(terms), len(excludes), workers)

    keys = list(companies.keys())
    grand: dict = {"found": 0, "kept": 0, "title_rejected": 0,
                   "location_rejected": 0, "new": 0, "existing": 0, "errors": 0,
                   "companies": len(keys)}
    t0 = time.time()

    if workers > 1 and len(keys) > 1:
        completed = 0
        with ThreadPoolExecutor(max_workers=min(workers, len(keys))) as pool:
            futures = {
                pool.submit(
                    _process_company, key, companies[key],
                    terms, excludes, accept_locs, reject_locs, search_cfg, location_filter,
                ): key
                for key in keys
            }
            for fut in as_completed(futures):
                r = fut.result()
                completed += 1
                grand["found"] += r["found"]
                grand["kept"] += r["kept"]
                grand["title_rejected"] += r["title_rejected"]
                grand["location_rejected"] += r["location_rejected"]
                grand["new"] += r["new"]
                grand["existing"] += r["existing"]
                if r["error"]:
                    grand["errors"] += 1
                if completed % 5 == 0 or completed == len(keys):
                    elapsed = time.time() - t0
                    log.info("Greenhouse progress: %d/%d (%d new, %d dupes, %d errors) [%.0fs]",
                             completed, len(keys), grand["new"], grand["existing"],
                             grand["errors"], elapsed)
    else:
        for i, key in enumerate(keys, 1):
            r = _process_company(
                key, companies[key],
                terms, excludes, accept_locs, reject_locs, search_cfg, location_filter,
            )
            grand["found"] += r["found"]
            grand["kept"] += r["kept"]
            grand["title_rejected"] += r["title_rejected"]
            grand["location_rejected"] += r["location_rejected"]
            grand["new"] += r["new"]
            grand["existing"] += r["existing"]
            if r["error"]:
                grand["errors"] += 1
            if i % 5 == 0 or i == len(keys):
                elapsed = time.time() - t0
                log.info("Greenhouse progress: %d/%d (%d new, %d dupes, %d errors) [%.0fs]",
                         i, len(keys), grand["new"], grand["existing"],
                         grand["errors"], elapsed)

    elapsed = time.time() - t0
    log.info(
        "Greenhouse crawl done in %.0fs: %d found, %d title-rejected, "
        "%d location-rejected, %d kept, %d new, %d dupes, %d errors",
        elapsed, grand["found"], grand["title_rejected"], grand["location_rejected"],
        grand["kept"], grand["new"], grand["existing"], grand["errors"],
    )
    return grand


def run_bigtech_discovery(
    companies: dict | None = None,
    workers: int = 3,
) -> dict:
    """Discover jobs from configured proprietary big-tech career sites."""
    if companies is None:
        companies = load_bigtech_companies()
    if not companies:
        return {
            "found": 0,
            "kept": 0,
            "new": 0,
            "existing": 0,
            "errors": 0,
            "companies": 0,
        }

    search_cfg = config.load_search_config()
    reconcile_unscored_jobs(init_db(), search_cfg)
    terms = _load_query_terms(search_cfg)
    excludes = _load_excluded_titles(search_cfg)
    accept_locs, reject_locs = _load_location_filter(search_cfg)
    location_filter = search_cfg.get(
        "bigtech_location_filter",
        search_cfg.get("greenhouse_location_filter", True),
    )
    keys = list(companies)
    grand = {
        "found": 0,
        "kept": 0,
        "title_rejected": 0,
        "location_rejected": 0,
        "new": 0,
        "existing": 0,
        "errors": 0,
        "companies": len(keys),
    }

    def add_result(result: dict) -> None:
        for field in ("found", "kept", "title_rejected", "location_rejected", "new", "existing"):
            grand[field] += result[field]
        if result["error"]:
            grand["errors"] += 1

    log.info(
        "Big-tech crawl: %d companies | %d query terms | %d excludes | workers=%d",
        len(keys),
        len(terms),
        len(excludes),
        workers,
    )
    if workers > 1 and len(keys) > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(keys))) as pool:
            futures = [
                pool.submit(
                    _process_bigtech_company,
                    key,
                    companies[key],
                    terms,
                    excludes,
                    accept_locs,
                    reject_locs,
                    search_cfg,
                    location_filter,
                )
                for key in keys
            ]
            for future in as_completed(futures):
                add_result(future.result())
    else:
        for key in keys:
            add_result(
                _process_bigtech_company(
                    key,
                    companies[key],
                    terms,
                    excludes,
                    accept_locs,
                    reject_locs,
                    search_cfg,
                    location_filter,
                )
            )

    log.info(
        "Big-tech crawl done: %d found, %d title-rejected, %d location-rejected, "
        "%d kept, %d new, %d dupes, %d errors",
        grand["found"],
        grand["title_rejected"],
        grand["location_rejected"],
        grand["kept"],
        grand["new"],
        grand["existing"],
        grand["errors"],
    )
    return grand
