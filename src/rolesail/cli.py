"""RoleSail CLI — the main entry point."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from rolesail import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="rolesail",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: migrate/create dirs, load env, and initialize the DB."""
    from rolesail.config import ensure_dirs, load_env
    from rolesail.database import init_db

    migrated = ensure_dirs()
    if migrated:
        console.print(
            "[green]Migrated ApplyPilot data to RoleSail.[/green] "
            "Your original files were left unchanged."
        )
    load_env()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]RoleSail[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """RoleSail — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from rolesail.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(3, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    score_workers: int = typer.Option(
        3,
        "--score-workers",
        help="Concurrent scoring requests (paced by LLM_RPM/LLM_TPM).",
    ),
    stream: bool = typer.Option(
        True,
        "--stream/--no-stream",
        help="Run stages concurrently (default). Use --no-stream for sequential execution.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from rolesail.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from rolesail.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    if score_workers < 1:
        console.print("[red]--score-workers must be at least 1.[/red]")
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        score_workers=score_workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max jobs to process per run (counts both applied and failed; default 1). Use --continuous to run indefinitely."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("opus", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    unmark_applied: Optional[str] = typer.Option(None, "--unmark-applied", help="Return an applied job URL to the active queue."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from rolesail.config import check_tier, PROFILE_PATH as _profile_path
    from rolesail.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from rolesail.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if unmark_applied:
        from rolesail.apply.launcher import unmark_job
        unmark_job(unmark_applied)
        console.print(f"[green]Returned to active jobs:[/green] {unmark_applied}")
        return

    if mark_failed:
        from rolesail.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from rolesail.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]rolesail init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]rolesail run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from rolesail.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from rolesail.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from rolesail.database import get_stats

    stats = get_stats()

    console.print("\n[bold]RoleSail Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard(
    port: int = typer.Option(8765, "--port", help="Local dashboard server port."),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open the browser automatically."),
) -> None:
    """Run the interactive dashboard on localhost."""
    _bootstrap()

    from rolesail.dashboard_server import serve_dashboard

    try:
        serve_dashboard(port=port, open_browser=not no_open)
    except OSError as exc:
        console.print(f"[red]Could not start dashboard:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command()
def gmail_connect(
    credentials: Path = typer.Option(..., "--credentials", help="Google OAuth Desktop app credentials JSON."),
) -> None:
    """Connect a personal Gmail account for draft creation only."""
    _bootstrap()

    from rolesail.outreach.gmail import connect_gmail

    try:
        email = connect_gmail(credentials)
    except Exception as exc:  # noqa: BLE001 - OAuth providers can raise several error classes
        console.print(f"[red]Gmail connection failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Connected Gmail Drafts:[/green] {email}")
    console.print("RoleSail will create drafts only. Schedule or send them yourself in Gmail.")


@app.command()
def outreach(
    url: str = typer.Option(..., "--url", help="Applied job URL."),
    prepare: bool = typer.Option(False, "--prepare", help="Prepare or re-prepare an unsent batch."),
    retry: bool = typer.Option(False, "--retry", help="Retry failed preparation or legacy Apollo sends."),
) -> None:
    """Inspect or recover an outreach batch. Sending still requires dashboard approval."""
    _bootstrap()
    from rolesail.outreach.service import enqueue_for_job, get_batch, prepare_batch, retry_batch

    batch = get_batch(url)
    if prepare:
        batch = batch or enqueue_for_job(url)
        if not batch:
            console.print("[red]Outreach is disabled or the job is not applied.[/red]")
            raise typer.Exit(code=1)
        batch = prepare_batch(batch["id"])
    elif retry:
        if not batch:
            console.print("[red]No outreach batch exists for this job.[/red]")
            raise typer.Exit(code=1)
        batch = retry_batch(batch["id"])
    if not batch:
        console.print("[yellow]No outreach batch exists for this job.[/yellow]")
        return
    console.print(f"[bold]Outreach:[/bold] {batch['status']}")
    if batch.get("error"):
        console.print(f"[red]{batch['error']}[/red]")
    for recipient in batch.get("recipients", []):
        name = " ".join(filter(None, (recipient.get("first_name"), recipient.get("last_name"))))
        console.print(f"  {recipient['status']:<12} {name or recipient.get('email')} — {recipient.get('title') or ''}")


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    _bootstrap()

    import shutil
    from rolesail.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_TEX_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path, load_profile,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'rolesail init' to create"))

    # Resume
    if RESUME_TEX_PATH.exists():
        results.append(("resume.tex", ok_mark, str(RESUME_TEX_PATH)))
    elif RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'rolesail init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'rolesail init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.rolesail/.env (run 'rolesail init')"))

    # LaTeX tailoring and informational visual audit
    tectonic_bin = shutil.which("tectonic")
    if tectonic_bin:
        results.append(("Tectonic", ok_mark, tectonic_bin))
    else:
        results.append(("Tectonic", fail_mark, "Required to compile tailored LaTeX resumes"))
    pdftotext_bin = shutil.which("pdftotext")
    if pdftotext_bin:
        results.append(("pdftotext", ok_mark, pdftotext_bin))
    else:
        results.append(("pdftotext", warn_mark, "Optional: install Poppler for visual line diagnostics"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # Apollo outreach (optional and only network-checked when enabled)
    outreach_enabled = os.environ.get("OUTREACH_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if outreach_enabled:
        if not os.environ.get("APOLLO_API_KEY"):
            results.append(("Apollo API", fail_mark, "Set APOLLO_API_KEY before enabling outreach"))
        else:
            try:
                from rolesail.outreach.apollo import ApolloClient
                apollo = ApolloClient()
                apollo.health()
                results.append(("Apollo API", ok_mark, "People discovery available"))
                account_id = os.environ.get("APOLLO_EMAIL_ACCOUNT_ID")
                if account_id:
                    account = next((item for item in apollo.email_accounts() if str(item.get("id")) == account_id), None)
                    if account:
                        address = account.get("email") or account.get("email_address") or account_id
                        results.append(("Apollo mailbox", ok_mark, f"Legacy Apollo sends use {address}"))
                    else:
                        results.append(("Apollo mailbox", fail_mark, "Configured mailbox is not linked to this Apollo user"))
                else:
                    results.append(("Apollo mailbox", "[dim]optional[/dim]", "Only needed for legacy Apollo scheduled sends"))
            except Exception as exc:
                results.append(("Apollo API", fail_mark, str(exc)[:120]))
        from rolesail.outreach.gmail import connected_account
        gmail_email = connected_account()
        results.append((
            "Gmail Drafts",
            ok_mark if gmail_email else "[dim]optional[/dim]",
            gmail_email or "Run rolesail gmail-connect --credentials PATH to create drafts",
        ))
        try:
            samples = load_profile().get("outreach", {}).get("writing_samples", [])
            marker = ok_mark if len(samples) >= 3 else fail_mark
            results.append(("Outreach style", marker, f"{len(samples)} writing samples configured; 3 required"))
        except (OSError, ValueError):
            results.append(("Outreach style", fail_mark, "Profile could not be loaded"))
    else:
        results.append(("Apollo outreach", "[dim]optional[/dim]", "Set OUTREACH_ENABLED=true after configuration"))

    # --- Render results ---
    console.print()
    console.print("[bold]RoleSail Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from rolesail.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
