"""Detail page enrichment: scrapes full descriptions and apply URLs.

For each job URL in the database, navigates to the detail page and extracts:
  - full_description: the complete job posting text
  - application_url: the "Apply" button/link URL

Three-tier extraction cascade (cheapest first):
  Tier 1: JSON-LD JobPosting structured data (0 tokens)
  Tier 2: Deterministic CSS pattern matching (0 tokens)
  Tier 3: LLM-assisted extraction (1 LLM call)
"""

# Compatibility facade: extraction and URL helper names remain importable here.
# ruff: noqa: F401

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from rolesail import config
from rolesail.database import init_db
from rolesail.discovery.filters import reconcile_unscored_jobs

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Sites that block scraping -- skip detail extraction entirely
SKIP_DETAIL_SITES = {"glassdoor", "google", "Workopolis"}

# Module-level proxy config (set from CLI or caller)
_PROXY_CONFIG: dict | None = None


def set_proxy(proxy_str: str | None):
    """Set proxy config from an external caller."""
    global _PROXY_CONFIG
    if proxy_str:
        from rolesail.discovery.jobspy import parse_proxy
        _PROXY_CONFIG = parse_proxy(proxy_str)


# -- URL resolution ----------------------------------------------------------

# -- Detail page intelligence ------------------------------------------------
from rolesail.enrichment.extractors import (
    APPLY_SELECTORS,
    DESCRIPTION_SELECTORS,
    DETAIL_EXTRACT_PROMPT,
    _apple_jobs_data,
    _find_job_posting,
    _job_location,
    _organization_logo,
    clean_content_html,
    clean_description,
    collect_detail_intelligence,
    extract_apply_url_deterministic,
    extract_description_deterministic,
    extract_from_apple_hydration,
    extract_from_json_ld,
    extract_from_meta_details,
    extract_from_microsoft_details,
    extract_job_metadata,
    extract_main_content,
    extract_with_llm,
    reset_failed_linkedin_descriptions,
    reset_incomplete_amazon_descriptions,
    reset_incomplete_apple_descriptions,
    reset_incomplete_meta_descriptions,
    reset_incomplete_microsoft_descriptions,
)
from rolesail.enrichment.urls import (
    _load_base_urls,
    normalize_application_url,
    resolve_all_urls,
    resolve_url,
    resolve_wttj_urls,
)

# -- Orchestration -----------------------------------------------------------

SITE_DELAYS = {
    "RemoteOK": 3.0,
    "WelcomeToTheJungle": 2.0,
    "Job Bank Canada": 1.5,
    "CareerJet Canada": 3.0,
    "Hacker News Jobs": 1.0,
    "BuiltIn Remote": 2.0,
}

RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
PERMANENT_FAILURES = {404, 410, 451}


def scrape_detail_page(page, url: str) -> dict:
    """Full cascade for one detail page."""
    result: dict = {
        "full_description": None,
        "application_url": None,
        "status": "error",
        "tier_used": None,
        "error": None,
        "title": None,
        "company": None,
        "company_logo": None,
        "location": None,
        "posted_at": None,
    }
    t0 = time.time()

    try:
        resp = page.goto(url, timeout=45000)
        if resp and resp.status in PERMANENT_FAILURES:
            result["error"] = f"HTTP {resp.status}"
            result["elapsed"] = time.time() - t0
            return result
        if resp and resp.status == 403:
            result["error"] = "HTTP 403"
            result["elapsed"] = time.time() - t0
            return result
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
    except Exception as e:
        err_str = str(e)
        if "timeout" in err_str.lower():
            result["error"] = "timeout"
        else:
            result["error"] = err_str[:200]
        result["elapsed"] = time.time() - t0
        return result

    intel = collect_detail_intelligence(page)
    result.update(extract_job_metadata(intel))

    # Meta's JSON-LD `description` is only the intro. Prefer the complete set
    # of rendered sections collected from the job detail container.
    meta_result = extract_from_meta_details(intel)
    if meta_result and meta_result.get("full_description"):
        result.update(meta_result)
        result["tier_used"] = 1
        result["status"] = "ok"
        result["elapsed"] = time.time() - t0
        return result

    # Microsoft's JSON-LD omits the Overview. Prefer the complete PCS/X API
    # payload used to render the page.
    microsoft_result = extract_from_microsoft_details(intel)
    if microsoft_result and microsoft_result.get("full_description"):
        result.update(microsoft_result)
        result["tier_used"] = 1
        result["status"] = "ok"
        result["elapsed"] = time.time() - t0
        return result

    # Apple exposes each description section separately in its router payload.
    # Run this before JSON-LD, which may contain only the Summary section.
    apple_result = extract_from_apple_hydration(intel)
    if apple_result and apple_result.get("full_description"):
        result.update(apple_result)
        result["tier_used"] = 1
        result["status"] = "ok"
        result["elapsed"] = time.time() - t0
        return result

    # Tier 1: JSON-LD
    json_ld_result = extract_from_json_ld(intel)
    if json_ld_result and json_ld_result.get("full_description"):
        result.update(json_ld_result)
        result["tier_used"] = 1
        if not result.get("application_url"):
            apply = extract_apply_url_deterministic(page)
            if apply:
                result["application_url"] = apply
        result["status"] = "ok" if result.get("application_url") else "partial"
        result["elapsed"] = time.time() - t0
        return result

    # Tier 2: Deterministic CSS
    desc = extract_description_deterministic(page)
    apply = extract_apply_url_deterministic(page)

    if desc:
        result["full_description"] = desc
        result["application_url"] = apply
        result["tier_used"] = 2
        result["status"] = "ok" if apply else "partial"
        result["elapsed"] = time.time() - t0
        return result

    tier2_apply = apply

    # Tier 3: LLM
    llm_result = extract_with_llm(page, url)
    result["full_description"] = llm_result.get("full_description")
    result["application_url"] = llm_result.get("application_url") or tier2_apply
    result["tier_used"] = 3

    if result.get("full_description"):
        result["status"] = "ok" if result.get("application_url") else "partial"
    elif result.get("application_url"):
        result["status"] = "partial"
    else:
        result["status"] = "error"
        result["error"] = "no data extracted"

    result["elapsed"] = time.time() - t0
    return result


def scrape_site_batch(
    conn: sqlite3.Connection | None,
    site: str,
    jobs: list[tuple],
    delay: float = 2.0,
    max_jobs: int | None = None,
) -> dict:
    """Process all jobs for one site using shared browser context.

    If conn is None, creates its own DB connection.
    """
    stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    if max_jobs:
        jobs = jobs[:max_jobs]

    if not jobs:
        return stats

    own_conn = conn is None
    if own_conn:
        conn = init_db()

    now = datetime.now(timezone.utc).isoformat()

    try:
        with sync_playwright() as p:
            launch_opts: dict = {"headless": True}
            if _PROXY_CONFIG:
                launch_opts["proxy"] = _PROXY_CONFIG["playwright"]
            browser = p.chromium.launch(**launch_opts)
            context = browser.new_context(user_agent=UA)
            page = context.new_page()

            for i, (url, title) in enumerate(jobs):
                log.info("[%d/%d] %s", i + 1, len(jobs), title[:50] if title else url[:50])

                result = scrape_detail_page(page, url)
                stats["processed"] += 1

                tier = result.get("tier_used")
                status = result["status"]
                elapsed = result.get("elapsed", 0)

                if tier:
                    stats["tiers"][tier] = stats["tiers"].get(tier, 0) + 1

                tier_str = f"T{tier}" if tier else "--"
                desc_len = len(result.get("full_description") or "")
                apply_str = "yes" if result.get("application_url") else "no"
                err_str = f" | err={result.get('error')}" if result.get("error") else ""

                log.info("  %s | %s | desc=%s chars | apply=%s | %.1fs%s",
                         status, tier_str, f"{desc_len:,}", apply_str, elapsed, err_str)

                if status in ("ok", "partial"):
                    stats[status] += 1
                    enriched_location = result.get("location")
                    existing_location = conn.execute(
                        "SELECT location FROM jobs WHERE url = ?",
                        (url,),
                    ).fetchone()
                    effective_location = enriched_location or (
                        existing_location[0] if existing_location else None
                    )
                    location_allowed = config.location_is_allowed(effective_location)
                    is_external_ashby = (
                        (urlparse(url).hostname or "").lower() == "jobs.ashbyhq.com"
                    )
                    conn.execute(
                        "UPDATE jobs SET full_description = ?, application_url = ?, "
                        "detail_scraped_at = ?, detail_error = NULL, "
                        "title = CASE WHEN strategy = 'external_upload' "
                        "THEN COALESCE(?, title) ELSE title END, "
                        "company = CASE WHEN strategy = 'external_upload' "
                        "AND ? = 1 THEN COALESCE(?, company) "
                        "ELSE COALESCE(company, ?) END, "
                        "company_logo = COALESCE(company_logo, ?), "
                        "location = CASE WHEN strategy = 'external_upload' "
                        "THEN COALESCE(?, location) ELSE location END, "
                        "posted_at = COALESCE(posted_at, ?), "
                        "discovery_status = CASE WHEN strategy = 'external_upload' AND ? = 0 "
                        "THEN 'rejected' ELSE discovery_status END, "
                        "discovery_rejection_reason = CASE "
                        "WHEN strategy = 'external_upload' AND ? = 0 THEN 'outside_allowed_countries' "
                        "ELSE discovery_rejection_reason END, "
                        "discovery_checked_at = CASE WHEN strategy = 'external_upload' "
                        "THEN ? ELSE discovery_checked_at END "
                        "WHERE url = ?",
                        (
                            result.get("full_description"),
                            normalize_application_url(
                                result.get("application_url"), url
                            ),
                            now,
                            result.get("title"),
                            is_external_ashby,
                            result.get("company"),
                            result.get("company"),
                            result.get("company_logo"),
                            result.get("location"),
                            result.get("posted_at"),
                            location_allowed,
                            location_allowed,
                            now,
                            url,
                        ),
                    )
                else:
                    stats["error"] += 1
                    conn.execute(
                        "UPDATE jobs SET detail_error = ?, detail_scraped_at = ? WHERE url = ?",
                        (result.get("error", "unknown"), now, url),
                    )

                conn.commit()

                if i < len(jobs) - 1:
                    time.sleep(delay)

            browser.close()
    finally:
        if own_conn:
            conn.close()

    return stats


def _run_detail_scraper(
    conn: sqlite3.Connection,
    sites: list[str] | None = None,
    max_per_site: int | None = None,
    workers: int = 1,
) -> dict:
    """Groups pending jobs by site and processes each batch.

    Sequential by default. When workers > 1, processes multiple site batches
    in parallel using ThreadPoolExecutor (each thread gets its own browser
    and DB connection).

    Returns aggregate stats dict.
    """
    skip_filter = " AND ".join(f"site != '{s}'" for s in SKIP_DETAIL_SITES)
    where = (
        "WHERE detail_scraped_at IS NULL "
        "AND COALESCE(discovery_status, 'accepted') = 'accepted' "
        f"AND {skip_filter}"
    )
    rows = conn.execute(
        f"SELECT url, title, site FROM jobs {where} ORDER BY site"
    ).fetchall()

    if not rows:
        log.info("No pending jobs to scrape.")
        return {"processed": 0, "ok": 0, "partial": 0, "error": 0}

    site_jobs: dict[str, list[tuple]] = {}
    for row in rows:
        url, title, site = row[0], row[1], row[2]
        if sites and site not in sites:
            continue
        site_jobs.setdefault(site, []).append((url, title))

    log.info("Pending: %d jobs across %d sites (workers=%d)", len(rows), len(site_jobs), workers)
    for site, jobs in site_jobs.items():
        log.info("  %s: %d jobs", site, len(jobs))

    known_order = [
        "RemoteOK", "Job Bank Canada", "BuiltIn Remote",
        "WelcomeToTheJungle", "CareerJet Canada", "Hacker News Jobs",
    ]
    order = [s for s in known_order if s in site_jobs]
    order += [s for s in sorted(site_jobs.keys()) if s not in order]

    total_stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    def _merge_stats(stats: dict) -> None:
        for k in ("processed", "ok", "partial", "error"):
            total_stats[k] += stats[k]
        for t, count in stats["tiers"].items():
            total_stats["tiers"][t] = total_stats["tiers"].get(t, 0) + count

    if workers > 1 and len(order) > 1:
        # Parallel mode: each site batch runs in its own thread with its own
        # DB connection (conn=None tells scrape_site_batch to create one)
        def _scrape_site(site: str) -> dict:
            jobs = site_jobs[site]
            delay = SITE_DELAYS.get(site, 2.0)
            log.info("%s -- %d jobs (delay=%.1fs)", site, len(jobs), delay)
            stats = scrape_site_batch(None, site, jobs, delay=delay, max_jobs=max_per_site)
            log.info("%s summary: %d ok, %d partial, %d error | T1=%d T2=%d T3=%d",
                     site, stats["ok"], stats["partial"], stats["error"],
                     stats["tiers"].get(1, 0), stats["tiers"].get(2, 0), stats["tiers"].get(3, 0))
            return stats

        with ThreadPoolExecutor(max_workers=min(workers, len(order))) as pool:
            futures = {pool.submit(_scrape_site, site): site for site in order}
            for future in as_completed(futures):
                _merge_stats(future.result())
    else:
        # Sequential mode (default)
        for site in order:
            jobs = site_jobs[site]
            delay = SITE_DELAYS.get(site, 2.0)
            log.info("%s -- %d jobs (delay=%.1fs)", site, len(jobs), delay)

            stats = scrape_site_batch(conn, site, jobs, delay=delay, max_jobs=max_per_site)
            _merge_stats(stats)

            log.info("Site summary: %d ok, %d partial, %d error | T1=%d T2=%d T3=%d",
                     stats["ok"], stats["partial"], stats["error"],
                     stats["tiers"].get(1, 0), stats["tiers"].get(2, 0), stats["tiers"].get(3, 0))

    log.info("TOTAL: %d processed | %d ok | %d partial | %d error",
             total_stats["processed"], total_stats["ok"], total_stats["partial"], total_stats["error"])
    log.info("Tier distribution: T1=%d T2=%d T3=%d",
             total_stats["tiers"].get(1, 0), total_stats["tiers"].get(2, 0), total_stats["tiers"].get(3, 0))

    llm_calls = total_stats["tiers"].get(3, 0)
    total = total_stats["processed"]
    if total > 0:
        savings = ((total - llm_calls) / total) * 100
        log.info("LLM calls: %d/%d (%.0f%% saved)", llm_calls, total, savings)

    return total_stats


# -- Streaming detail scraper (for sequential pipeline) ----------------------

def stream_detail(
    upstream_done,
    my_done,
    proxy_str: str | None = None,
    poll_interval: float = 5.0,
) -> None:
    """Streaming detail scraper: polls DB for un-scraped jobs, scrapes sites sequentially.

    Args:
        upstream_done: Event set when discover+extract done. None = run once.
        my_done: Event to set when this stage completes.
        proxy_str: Proxy in host:port:user:pass format.
        poll_interval: Seconds to sleep when no pending jobs found.
    """
    if proxy_str:
        set_proxy(proxy_str)

    conn = init_db()
    audit = reconcile_unscored_jobs(conn)
    if audit["rejected"]:
        log.info("Discovery title gate excludes %d pending job(s) from enrichment", audit["rejected"])

    reset_count = reset_incomplete_apple_descriptions(conn)
    if reset_count:
        log.info("Apple: requeued %d incomplete descriptions", reset_count)
    amazon_reset_count = reset_incomplete_amazon_descriptions(conn)
    if amazon_reset_count:
        log.info("Amazon: requeued %d incomplete descriptions", amazon_reset_count)
    microsoft_reset_count = reset_incomplete_microsoft_descriptions(conn)
    if microsoft_reset_count:
        log.info("Microsoft: requeued %d incomplete descriptions", microsoft_reset_count)
    meta_reset_count = reset_incomplete_meta_descriptions(conn)
    if meta_reset_count:
        log.info("Meta: requeued %d incomplete descriptions", meta_reset_count)
    linkedin_reset_count = reset_failed_linkedin_descriptions(conn)
    if linkedin_reset_count:
        log.info("LinkedIn: requeued %d failed descriptions", linkedin_reset_count)

    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute",
             url_stats['resolved'], url_stats['already_absolute'])

    total_ok = 0
    total_err = 0
    t0 = time.time()

    try:
        while True:
            skip_filter = " AND ".join(f"site != '{s}'" for s in SKIP_DETAIL_SITES)
            rows = conn.execute(
                "SELECT url, title, site FROM jobs "
                "WHERE detail_scraped_at IS NULL "
                "AND COALESCE(discovery_status, 'accepted') = 'accepted' "
                f"AND {skip_filter} "
                "ORDER BY site LIMIT 200"
            ).fetchall()

            if rows:
                site_jobs: dict[str, list[tuple]] = {}
                for row in rows:
                    url, title, site = row[0], row[1], row[2]
                    site_jobs.setdefault(site, []).append((url, title))

                for site, jobs in site_jobs.items():
                    delay = SITE_DELAYS.get(site, 2.0)
                    log.info("%s: %d jobs (delay=%.1fs)", site, len(jobs), delay)

                    try:
                        stats = scrape_site_batch(conn, site, jobs, delay=delay)
                        total_ok += stats["ok"] + stats["partial"]
                        total_err += stats["error"]
                        log.info("%s: %d ok, %d partial, %d error",
                                 site, stats['ok'], stats['partial'], stats['error'])
                    except Exception as e:
                        log.error("%s: CRASHED: %s", site, e)

            upstream_finished = upstream_done is None or upstream_done.is_set()
            if upstream_finished and not rows:
                break
            if not rows:
                time.sleep(poll_interval)
    finally:
        elapsed = time.time() - t0
        if total_ok or total_err:
            log.info("DONE: %d ok, %d errors in %.1fs", total_ok, total_err, elapsed)
        conn.close()
        my_done.set()


# -- Public entry point ------------------------------------------------------

def run_enrichment(limit: int = 100, workers: int = 3) -> dict:
    """Main entry point for detail page enrichment.

    Fetches pending jobs from the database (those without full_description),
    resolves relative URLs, then runs the three-tier extraction cascade on
    each detail page.

    Args:
        limit: Maximum number of jobs per site to process.
        workers: Number of parallel threads for site batch processing. Default 3.

    Returns:
        Dict with stats: processed, ok, partial, error, tiers.
    """
    conn = init_db()
    audit = reconcile_unscored_jobs(conn)
    if audit["rejected"]:
        log.info("Discovery title gate excludes %d pending job(s) from enrichment", audit["rejected"])

    reset_count = reset_incomplete_apple_descriptions(conn)
    if reset_count:
        log.info("Apple: requeued %d incomplete descriptions", reset_count)
    amazon_reset_count = reset_incomplete_amazon_descriptions(conn)
    if amazon_reset_count:
        log.info("Amazon: requeued %d incomplete descriptions", amazon_reset_count)
    microsoft_reset_count = reset_incomplete_microsoft_descriptions(conn)
    if microsoft_reset_count:
        log.info("Microsoft: requeued %d incomplete descriptions", microsoft_reset_count)
    meta_reset_count = reset_incomplete_meta_descriptions(conn)
    if meta_reset_count:
        log.info("Meta: requeued %d incomplete descriptions", meta_reset_count)
    linkedin_reset_count = reset_failed_linkedin_descriptions(conn)
    if linkedin_reset_count:
        log.info("LinkedIn: requeued %d failed descriptions", linkedin_reset_count)

    # URL resolution first
    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute, %d failed",
             url_stats["resolved"], url_stats["already_absolute"], url_stats["failed"])

    # WTTJ special handling
    wttj_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE site = 'WelcomeToTheJungle'"
    ).fetchone()[0]
    if wttj_count > 0:
        sample = conn.execute(
            "SELECT url FROM jobs WHERE site = 'WelcomeToTheJungle' LIMIT 1"
        ).fetchone()
        if sample and not sample[0].startswith("http"):
            updated = resolve_wttj_urls(conn)
            log.info("WTTJ: %d URLs updated", updated)

    # Run the detail scraper
    stats = _run_detail_scraper(conn, max_per_site=limit, workers=workers)

    return stats
