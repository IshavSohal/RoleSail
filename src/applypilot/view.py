"""Compatibility helpers for dashboard presentation.

The interactive dashboard is now a packaged React application served by
``applypilot.dashboard_server``.  Date and classification helpers remain
importable here for callers that used the original HTML generator module.
"""

from __future__ import annotations

import re
import shutil
import webbrowser
from html import escape
from pathlib import Path

from rich.console import Console

from applypilot.config import APP_DIR
from applypilot.dashboard_data import (
    applied_view,
    format_applied_at,
    format_posted_at,
    posted_at_sort_key,
)

console = Console()
WEB_DIST_DIR = Path(__file__).with_name("web_dist")

_JOB_SECTION_HEADING_RE = re.compile(
    r"^(?:summary|job summary|description|role overview|position overview|overview|"
    r"about (?:this|the) (?:role|job|position|team|company)|about us|about the team|"
    r"key job responsibilities|job responsibilities|key responsibilities|responsibilities|"
    r"duties|duties and responsibilities|what you(?:'|’)ll do|what you will do|"
    r"minimum qualifications|basic qualifications|required qualifications|"
    r"preferred qualifications|desired qualifications|qualifications|minimum requirements|"
    r"required skills|preferred skills|skills and experience|education|experience|who you are|"
    r"what we(?:'|’)re looking for|what we are looking for|what we offer|benefits|"
    r"compensation|salary|salary range|pay range|pay transparency|work/life balance|"
    r"work-life balance|diverse experiences|inclusive team culture|mentorship and career growth|"
    r"mentorship & career growth|why aws|why join us)\s*[:?]?\s*$",
    re.IGNORECASE,
)


def format_job_description_html(description: str | None) -> str:
    """Escape a description and emphasize recognized standalone headings."""
    if not description:
        return ""
    rendered: list[str] = []
    for line in str(description).splitlines():
        display = re.sub(r"^\s*#{1,6}\s+", "", line).strip()
        markdown_heading = display != line.strip()
        if display and (
            markdown_heading or _JOB_SECTION_HEADING_RE.fullmatch(display)
        ):
            rendered.append(
                f'<strong class="description-section-heading">{escape(display)}</strong>'
            )
        else:
            rendered.append(escape(line))
    return "<br>".join(rendered)


def generate_dashboard(output_path: str | None = None) -> str:
    """Copy the packaged SPA shell for legacy callers.

    The copied shell still needs ``applypilot dashboard`` to serve its assets
    and APIs. New code should launch the dashboard server directly.
    """
    source = WEB_DIST_DIR / "index.html"
    if not source.is_file():
        raise RuntimeError("Dashboard assets are missing; build the frontend first")
    output = Path(output_path) if output_path else APP_DIR / "dashboard.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    absolute = str(output.resolve())
    console.print(f"[green]Dashboard shell written to {absolute}[/green]")
    return absolute


def open_dashboard(output_path: str | None = None) -> None:
    """Open a copied SPA shell for compatibility with pre-server callers."""
    path = generate_dashboard(output_path)
    console.print("[yellow]Run `applypilot dashboard` for the interactive app.[/yellow]")
    webbrowser.open(f"file:///{path}")


__all__ = [
    "applied_view",
    "format_applied_at",
    "format_job_description_html",
    "format_posted_at",
    "generate_dashboard",
    "open_dashboard",
    "posted_at_sort_key",
]
