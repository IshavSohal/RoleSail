"""RoleSail Pipeline Orchestrator.

Runs pipeline stages in sequence or concurrently (streaming mode).

Usage (via CLI):
    rolesail run                        # all stages, concurrent (default)
    rolesail run --no-stream            # all stages, sequential
    rolesail run discover enrich        # specific stages
    rolesail run score tailor cover     # LLM-only stages
    rolesail run --dry-run              # preview without executing
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from rolesail.config import ensure_dirs, load_env
from rolesail.database import (
    JOB_RETENTION_DAYS,
    delete_jobs_older_than,
    get_connection,
    get_stats,
    init_db,
)

log = logging.getLogger(__name__)
console = Console()


# ---------------------------------------------------------------------------
# Stage definitions
# ---------------------------------------------------------------------------

STAGE_ORDER = ("discover", "enrich", "score", "tailor", "cover", "pdf")

STAGE_META: dict[str, dict] = {
    "discover": {"desc": "Job discovery (direct employers + startup feeds)"},
    "enrich":   {"desc": "Detail enrichment (full descriptions + apply URLs)"},
    "score":    {"desc": "LLM scoring (fit 1-10)"},
    "tailor":   {"desc": "Resume tailoring (LLM + validation)"},
    "cover":    {"desc": "Cover letter generation"},
    "pdf":      {"desc": "PDF conversion (tailored resumes + cover letters)"},
}

# Upstream dependency: a stage only finishes when its upstream is done AND
# it has no remaining pending work.
_UPSTREAM: dict[str, str | None] = {
    "discover": None,
    "enrich":   "discover",
    "score":    "enrich",
    "tailor":   "score",
    "cover":    "tailor",
    "pdf":      "cover",
}


# ---------------------------------------------------------------------------
# Individual stage runners
# ---------------------------------------------------------------------------

def _run_discover(workers: int = 3) -> dict:
    """Stage: direct-employer job discovery."""
    from rolesail.config import load_search_config
    from rolesail.discovery.filters import reconcile_unscored_jobs

    conn = init_db()
    audit = reconcile_unscored_jobs(conn, load_search_config())
    if audit["checked"]:
        log.info(
            "Discovery title audit: %d checked, %d accepted, %d rejected, %d changed",
            audit["checked"], audit["accepted"], audit["rejected"], audit["changed"],
        )
    stats: dict = {
        "greenhouse": None, "workday": None, "ashby": None, "lever": None,
        "bigtech": None, "startup_jobs": None,
    }
    totals = {
        key: 0
        for key in (
            "found", "kept", "title_rejected", "location_rejected",
            "new", "existing", "errors", "companies",
        )
    }

    console.print("  [cyan]Greenhouse boards crawl...[/cyan]")
    try:
        from rolesail.discovery.greenhouse import run_greenhouse_discovery
        result = run_greenhouse_discovery(workers=workers)
        stats["greenhouse"] = "ok"
        for key in totals:
            totals[key] += result.get(key, 0)
    except Exception as e:
        log.error("Greenhouse crawl failed: %s", e)
        console.print(f"  [red]Greenhouse error:[/red] {e}")
        stats["greenhouse"] = f"error: {e}"

    console.print("  [cyan]Workday corporate scraper...[/cyan]")
    try:
        from rolesail.discovery.workday import run_workday_discovery
        result = run_workday_discovery(workers=workers)
        stats["workday"] = "ok"
        for key in totals:
            totals[key] += result.get(key, 0)
    except Exception as e:
        log.error("Workday scraper failed: %s", e)
        console.print(f"  [red]Workday error:[/red] {e}")
        stats["workday"] = f"error: {e}"

    for provider in ("ashby", "lever"):
        console.print(f"  [cyan]{provider.title()} boards crawl...[/cyan]")
        try:
            from rolesail.discovery.ats import run_ats_discovery
            result = run_ats_discovery(provider, workers=workers)
            stats[provider] = "ok"
            for key in totals:
                totals[key] += result.get(key, 0)
        except Exception as e:
            log.error("%s crawl failed: %s", provider.title(), e)
            console.print(f"  [red]{provider.title()} error:[/red] {e}")
            stats[provider] = f"error: {e}"

    console.print("  [cyan]Big-tech career sites crawl...[/cyan]")
    try:
        from rolesail.discovery.greenhouse import run_bigtech_discovery
        result = run_bigtech_discovery(workers=workers)
        stats["bigtech"] = "ok"
        for key in totals:
            totals[key] += result.get(key, 0)
    except Exception as e:
        log.error("Big-tech crawl failed: %s", e)
        console.print(f"  [red]Big-tech error:[/red] {e}")
        stats["bigtech"] = f"error: {e}"

    # Run the aggregate feed after direct-employer sources so an exact
    # company/title match can prefer the canonical ATS record.
    console.print("  [cyan]Startup Jobs feed...[/cyan]")
    try:
        from rolesail.discovery.startup_jobs import run_startup_jobs_discovery
        result = run_startup_jobs_discovery()
        skipped = result.get("skipped")
        stats["startup_jobs"] = f"skipped: {skipped}" if skipped else "ok"
        for key in totals:
            totals[key] += result.get(key, 0)
    except Exception as e:
        log.error("Startup Jobs discovery failed: %s", e)
        console.print(f"  [red]Startup Jobs error:[/red] {e}")
        stats["startup_jobs"] = f"error: {e}"

    stats.update(totals)

    # Smart extract
    # console.print("  [cyan]Smart extract (AI-powered scraping)...[/cyan]")
    # try:
    #     from rolesail.discovery.smartextract import run_smart_extract
    #     run_smart_extract(workers=workers)
    #     stats["smartextract"] = "ok"
    # except Exception as e:
    #     log.error("Smart extract failed: %s", e)
    #     console.print(f"  [red]Smart extract error:[/red] {e}")
    #     stats["smartextract"] = f"error: {e}"

    return stats


def _run_enrich(workers: int = 3) -> dict:
    """Stage: Detail enrichment — scrape full descriptions and apply URLs."""
    try:
        from rolesail.enrichment.detail import run_enrichment
        run_enrichment(workers=workers)
        return {"status": "ok"}
    except Exception as e:
        log.error("Enrichment failed: %s", e)
        return {"status": f"error: {e}"}


def _run_score(workers: int = 3) -> dict:
    """Stage: LLM scoring — assign fit scores 1-10."""
    try:
        from rolesail.scoring.scorer import run_scoring
        run_scoring(workers=workers)
        return {"status": "ok"}
    except Exception as e:
        log.error("Scoring failed: %s", e)
        return {"status": f"error: {e}"}


def _run_tailor(min_score: int = 7, validation_mode: str = "normal") -> dict:
    """Stage: Resume tailoring — generate tailored resumes for high-fit jobs."""
    try:
        from rolesail.scoring.tailor import run_tailoring
        run_tailoring(min_score=min_score, validation_mode=validation_mode)
        return {"status": "ok"}
    except Exception as e:
        log.error("Tailoring failed: %s", e)
        return {"status": f"error: {e}"}


def _run_cover(min_score: int = 7, validation_mode: str = "normal") -> dict:
    """Stage: Cover letter generation."""
    try:
        from rolesail.scoring.cover_letter import run_cover_letters
        run_cover_letters(min_score=min_score, validation_mode=validation_mode)
        return {"status": "ok"}
    except Exception as e:
        log.error("Cover letter generation failed: %s", e)
        return {"status": f"error: {e}"}


def _run_pdf() -> dict:
    """Stage: PDF conversion — convert tailored resumes and cover letters to PDF."""
    try:
        from rolesail.scoring.pdf import batch_convert
        batch_convert()
        return {"status": "ok"}
    except Exception as e:
        log.error("PDF conversion failed: %s", e)
        return {"status": f"error: {e}"}


# Map stage names to their runner functions
_STAGE_RUNNERS: dict[str, callable] = {
    "discover": _run_discover,
    "enrich":   _run_enrich,
    "score":    _run_score,
    "tailor":   _run_tailor,
    "cover":    _run_cover,
    "pdf":      _run_pdf,
}


# ---------------------------------------------------------------------------
# Stage resolution
# ---------------------------------------------------------------------------

def _resolve_stages(stage_names: list[str]) -> list[str]:
    """Resolve 'all' and validate/order stage names.

    When discovery is requested and an LLM is configured (tier 2+), also
    include enrich and score so newly found jobs are fit-scored in the same run.
    """
    if "all" in stage_names:
        return list(STAGE_ORDER)

    resolved = []
    for name in stage_names:
        if name not in STAGE_META:
            console.print(
                f"[red]Unknown stage:[/red] '{name}'. "
                f"Available: {', '.join(STAGE_ORDER)}, all"
            )
            raise SystemExit(1)
        if name not in resolved:
            resolved.append(name)

    # Discovery should also score (and enrich first) when AI is available.
    if "discover" in resolved and "score" not in resolved:
        from rolesail.config import get_tier
        if get_tier() >= 2:
            for stage in ("enrich", "score"):
                if stage not in resolved:
                    resolved.append(stage)

    # Maintain canonical order
    return [s for s in STAGE_ORDER if s in resolved]


# ---------------------------------------------------------------------------
# Streaming pipeline helpers
# ---------------------------------------------------------------------------

class _StageTracker:
    """Thread-safe tracker for which stages have finished producing work."""

    def __init__(self):
        self._events: dict[str, threading.Event] = {
            stage: threading.Event() for stage in STAGE_ORDER
        }
        self._results: dict[str, dict] = {}
        self._lock = threading.Lock()

    def mark_done(self, stage: str, result: dict | None = None) -> None:
        with self._lock:
            self._results[stage] = result or {"status": "ok"}
        self._events[stage].set()

    def is_done(self, stage: str) -> bool:
        return self._events[stage].is_set()

    def wait(self, stage: str, timeout: float | None = None) -> bool:
        return self._events[stage].wait(timeout=timeout)

    def get_results(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._results)


# SQL to count pending work for each stage
_PENDING_SQL: dict[str, str] = {
    "enrich": (
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL "
        "AND COALESCE(discovery_status, 'accepted') = 'accepted'"
    ),
    "score":  (
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND fit_score IS NULL "
        "AND COALESCE(discovery_status, 'accepted') = 'accepted'"
    ),
    "tailor": (
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= ? "
        "AND full_description IS NOT NULL "
        "AND applied_at IS NULL "
        "AND tailored_resume_path IS NULL "
        "AND COALESCE(tailor_attempts, 0) < 5"
    ),
    "cover": (
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_attempts, 0) < 5"
    ),
    "pdf": (
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
        "AND tailored_resume_path LIKE '%.txt'"
    ),
}

# How long to sleep between polling loops in streaming mode (seconds)
_STREAM_POLL_INTERVAL = 10


def _count_pending(stage: str, min_score: int = 7) -> int:
    """Count pending work items for a stage."""
    sql = _PENDING_SQL.get(stage)
    if sql is None:
        return 0
    conn = get_connection()
    if "?" in sql:
        return conn.execute(sql, (min_score,)).fetchone()[0]
    return conn.execute(sql).fetchone()[0]


def _run_stage_streaming(
    stage: str,
    tracker: _StageTracker,
    stop_event: threading.Event,
    min_score: int = 7,
    workers: int = 3,
    score_workers: int = 3,
    validation_mode: str = "normal",
    run_id: str | None = None,
) -> None:
    """Run a single stage in streaming mode: loop until upstream done + no work.

    For discover: runs once, then marks done.
    For all others: polls DB for pending work, runs the batch processor,
    and repeats until upstream is done and no pending work remains.
    """
    runner = _STAGE_RUNNERS[stage]
    kwargs: dict = {}
    if stage in ("tailor", "cover"):
        kwargs["min_score"] = min_score
        kwargs["validation_mode"] = validation_mode
    if stage in ("discover", "enrich"):
        kwargs["workers"] = workers
    if stage == "score":
        kwargs["workers"] = score_workers

    upstream = _UPSTREAM[stage]
    from rolesail.usage import update_run, usage_context

    if stage == "discover":
        # Discover runs once (its sub-scrapers already do their full crawl)
        try:
            if run_id:
                update_run(run_id, current_stage=stage)
            with usage_context(run_id, stage):
                result = runner(**kwargs)
            tracker.mark_done(stage, result)
        except Exception as e:
            log.exception("Stage '%s' crashed", stage)
            tracker.mark_done(stage, {"status": f"error: {e}"})
        return

    # For downstream stages: loop until upstream done + no pending work
    passes = 0
    while not stop_event.is_set():
        # Wait for upstream to start producing work (first pass only)
        if passes == 0 and upstream and not tracker.is_done(upstream):
            # Wait a bit for upstream to produce some work before first run
            tracker.wait(upstream, timeout=_STREAM_POLL_INTERVAL)

        pending = _count_pending(stage, min_score)

        if pending > 0:
            try:
                if run_id:
                    update_run(run_id, current_stage=stage)
                with usage_context(run_id, stage):
                    runner(**kwargs)
                passes += 1
            except Exception as e:
                log.error("Stage '%s' error (pass %d): %s", stage, passes, e)
                passes += 1
        else:
            # No work right now
            upstream_done = upstream is None or tracker.is_done(upstream)
            if upstream_done:
                # No work and upstream is done — this stage is finished
                break
            # Upstream still running, wait and retry
            if stop_event.wait(timeout=_STREAM_POLL_INTERVAL):
                break  # Stop requested

    tracker.mark_done(stage, {"status": "ok", "passes": passes})


# ---------------------------------------------------------------------------
# Pipeline orchestrators
# ---------------------------------------------------------------------------

def _run_sequential(ordered: list[str], min_score: int, workers: int = 3,
                    score_workers: int = 3, validation_mode: str = "normal",
                    run_id: str | None = None) -> dict:
    """Execute stages one at a time (original behavior)."""
    results: list[dict] = []
    errors: dict[str, str] = {}
    pipeline_start = time.time()

    for name in ordered:
        from rolesail.usage import update_run, usage_context
        if run_id:
            update_run(run_id, current_stage=name)
        meta = STAGE_META[name]
        console.print(f"\n{'=' * 70}")
        console.print(f"  [bold]STAGE: {name}[/bold] — {meta['desc']}")
        console.print(f"  Started: {datetime.now().strftime('%H:%M:%S')}")
        console.print(f"{'=' * 70}")

        t0 = time.time()
        runner = _STAGE_RUNNERS[name]

        try:
            kwargs: dict = {}
            if name in ("tailor", "cover"):
                kwargs["min_score"] = min_score
                kwargs["validation_mode"] = validation_mode
            if name in ("discover", "enrich"):
                kwargs["workers"] = workers
            if name == "score":
                kwargs["workers"] = score_workers
            with usage_context(run_id, name):
                result = runner(**kwargs)
            elapsed = time.time() - t0

            status = "ok"
            if isinstance(result, dict):
                status = result.get("status", "ok")
                if name == "discover":
                    sub_errors = [
                        f"{k}: {v}" for k, v in result.items()
                        if isinstance(v, str) and v.startswith("error")
                    ]
                    if sub_errors:
                        status = "partial"

        except Exception as e:
            elapsed = time.time() - t0
            status = f"error: {e}"
            log.exception("Stage '%s' crashed", name)
            console.print(f"\n  [red]STAGE FAILED:[/red] {e}")

        results.append({"stage": name, "status": status, "elapsed": elapsed})
        if status not in ("ok", "partial"):
            errors[name] = status

        console.print(f"\n  Stage '{name}' completed in {elapsed:.1f}s — {status}")

    total_elapsed = time.time() - pipeline_start
    return {"stages": results, "errors": errors, "elapsed": total_elapsed}


def _run_streaming(ordered: list[str], min_score: int, workers: int = 3,
                   score_workers: int = 3, validation_mode: str = "normal",
                   run_id: str | None = None) -> dict:
    """Execute stages concurrently with DB as conveyor belt."""
    tracker = _StageTracker()
    stop_event = threading.Event()
    pipeline_start = time.time()

    console.print(f"\n  [bold cyan]STREAMING MODE[/bold cyan] — stages run concurrently")
    console.print(f"  Poll interval: {_STREAM_POLL_INTERVAL}s\n")

    # Mark stages NOT in `ordered` as done so downstream doesn't wait for them
    for stage in STAGE_ORDER:
        if stage not in ordered:
            tracker.mark_done(stage, {"status": "skipped"})

    # Launch each stage in its own thread
    threads: dict[str, threading.Thread] = {}
    start_times: dict[str, float] = {}

    for name in ordered:
        start_times[name] = time.time()
        t = threading.Thread(
            target=_run_stage_streaming,
            args=(name, tracker, stop_event, min_score, workers, score_workers, validation_mode, run_id),
            name=f"stage-{name}",
            daemon=True,
        )
        threads[name] = t
        t.start()
        console.print(f"  [dim]Started thread:[/dim] {name}")

    # Wait for all threads to finish
    try:
        for name in ordered:
            threads[name].join()
            elapsed = time.time() - start_times[name]
            console.print(
                f"  [green]Completed:[/green] {name} ({elapsed:.1f}s)"
            )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted — stopping stages...[/yellow]")
        stop_event.set()
        for t in threads.values():
            t.join(timeout=10)

    total_elapsed = time.time() - pipeline_start

    # Build results from tracker
    all_results = tracker.get_results()
    results: list[dict] = []
    errors: dict[str, str] = {}

    for name in ordered:
        r = all_results.get(name, {"status": "unknown"})
        elapsed = time.time() - start_times.get(name, pipeline_start)
        status = r.get("status", "ok")

        results.append({"stage": name, "status": status, "elapsed": elapsed})
        if status not in ("ok", "partial", "skipped"):
            errors[name] = status

    return {"stages": results, "errors": errors, "elapsed": total_elapsed}


def run_pipeline(
    stages: list[str] | None = None,
    min_score: int = 7,
    dry_run: bool = False,
    stream: bool = True,
    workers: int = 3,
    score_workers: int = 3,
    validation_mode: str = "normal",
    run_id: str | None = None,
) -> dict:
    """Run pipeline stages.

    Args:
        stages: List of stage names, or None / ["all"] for full pipeline.
        min_score: Minimum fit score for tailor/cover stages.
        dry_run: If True, preview stages without executing.
        stream: If True, run stages concurrently (streaming mode). Defaults to True.
        workers: Number of parallel threads for discovery/enrichment stages. Defaults to 3.
        score_workers: Maximum concurrent LLM requests in the scoring stage.

    Returns:
        Dict with keys: stages (list of result dicts), errors (dict), elapsed (float).
    """
    # Bootstrap
    load_env()
    ensure_dirs()
    init_db()

    if score_workers < 1:
        raise ValueError("score_workers must be at least 1")

    # Resolve stages
    if stages is None:
        stages = ["all"]
    ordered = _resolve_stages(stages)

    deleted_jobs = 0
    if not dry_run:
        deleted_jobs = delete_jobs_older_than(days=JOB_RETENTION_DAYS)

    # Banner
    mode = "streaming" if stream else "sequential"
    console.print()
    console.print(Panel.fit(
        f"[bold]RoleSail Pipeline[/bold] ({mode})",
        border_style="blue",
    ))
    console.print(f"  Min score:  {min_score}")
    console.print(f"  Workers:    {workers}")
    console.print(f"  Score workers: {score_workers}")
    console.print(f"  Validation: {validation_mode}")
    console.print(f"  Stages:     {' -> '.join(ordered)}")
    if deleted_jobs:
        console.print(
            f"  Cleanup:    deleted {deleted_jobs} jobs older than {JOB_RETENTION_DAYS} days"
        )

    # Pre-run stats
    pre_stats = get_stats()
    console.print(f"  DB:        {pre_stats['total']} jobs, {pre_stats['pending_detail']} pending enrichment")

    if dry_run:
        console.print(f"\n  [yellow]DRY RUN[/yellow] — would execute ({mode}):")
        for name in ordered:
            meta = STAGE_META[name]
            console.print(f"    {name:<12s}  {meta['desc']}")
        console.print(f"\n  No changes made.")
        return {"stages": [], "errors": {}, "elapsed": 0.0}

    # Execute
    if stream:
        result = _run_streaming(ordered, min_score, workers=workers,
                                score_workers=score_workers,
                                validation_mode=validation_mode, run_id=run_id)
    else:
        result = _run_sequential(ordered, min_score, workers=workers,
                                 score_workers=score_workers,
                                 validation_mode=validation_mode, run_id=run_id)

    # Summary table
    console.print(f"\n{'=' * 70}")
    summary = Table(title="Pipeline Summary", show_header=True, header_style="bold")
    summary.add_column("Stage", style="bold")
    summary.add_column("Status")
    summary.add_column("Time", justify="right")

    for r in result["stages"]:
        elapsed_str = f"{r['elapsed']:.1f}s"
        status_display = r["status"][:30]
        if r["status"] == "ok":
            style = "green"
        elif r["status"] in ("partial", "skipped"):
            style = "yellow"
        else:
            style = "red"
        summary.add_row(r["stage"], f"[{style}]{status_display}[/{style}]", elapsed_str)

    summary.add_row("", "", "")
    summary.add_row("[bold]Total[/bold]", "", f"[bold]{result['elapsed']:.1f}s[/bold]")
    console.print(summary)

    # Final DB stats
    final = get_stats()
    console.print(f"\n  [bold]DB Final State:[/bold]")
    console.print(f"    Total jobs:     {final['total']}")
    console.print(f"    Discovery filtered: {final['discovery_rejected']}")
    console.print(f"    With desc:      {final['with_description']}")
    console.print(f"    Scored:         {final['scored']}")
    console.print(f"    Tailored:       {final['tailored']}")
    console.print(f"    Cover letters:  {final['with_cover_letter']}")
    console.print(f"    Ready to apply: {final['ready_to_apply']}")
    console.print(f"    Applied:        {final['applied']}")
    console.print(f"{'=' * 70}\n")

    return result
