"""Deterministic, zero-LLM filters shared by discovery adapters."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from rolesail import config


DEFAULT_INCLUDE_TITLES = (
    "software engineer",
    "software engineering",
    "software developer",
    "frontend engineer",
    "frontend developer",
    "backend engineer",
    "backend developer",
    "full stack engineer",
    "full stack developer",
    "web engineer",
    "web developer",
    "ai engineer",
    "ai developer",
    "machine learning engineer",
    "machine learning developer",
    "ml engineer",
    "ml developer",
    "sde",
)


@dataclass(frozen=True)
class TitleEligibility:
    """Result of applying the configured title policy to one job."""

    accepted: bool
    reason: str


def normalize_title(value: str) -> str:
    """Normalize common title spelling variants for phrase matching."""
    text = str(value).casefold()
    text = re.sub(r"\bs\s*\.?\s*d\s*\.?\s*e\s*\.?\b", "software development engineer", text)
    text = re.sub(r"\bfront[\s-]*end\b", "frontend", text)
    text = re.sub(r"\bback[\s-]*end\b", "backend", text)
    text = re.sub(r"\bfull[\s-]*stack\b|\bfullstack\b", "fullstack", text)
    text = re.sub(r"\ba\s*\.?\s*i\s*\.?\b", "ai", text)
    text = re.sub(r"\bm\s*\.?\s*l\s*\.?\b", "ml", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def load_include_titles(search_cfg: dict | None = None) -> list[str]:
    """Load explicit title terms, falling back to legacy search queries."""
    search_cfg = search_cfg or config.load_search_config()
    if "include_titles" in search_cfg:
        values = search_cfg.get("include_titles") or []
    else:
        values = []
        for query in search_cfg.get("queries", []):
            if isinstance(query, dict):
                value = query.get("query")
            else:
                value = query
            if value:
                values.append(value)
        if not values:
            values = list(DEFAULT_INCLUDE_TITLES)
    return [str(value).strip() for value in values if str(value).strip()]


def load_excluded_titles(search_cfg: dict | None = None) -> list[str]:
    """Load configured title exclusions."""
    search_cfg = search_cfg or config.load_search_config()
    return [
        str(value).strip()
        for value in search_cfg.get("exclude_titles", [])
        if str(value).strip()
    ]


def classify_title(title: str | None, search_cfg: dict | None = None) -> TitleEligibility:
    """Return whether a title passes the strict configured title allowlist."""
    if not title or not str(title).strip():
        return TitleEligibility(False, "missing_title")

    search_cfg = search_cfg or config.load_search_config()
    normalized = normalize_title(str(title))
    padded = f" {normalized} "

    for excluded in load_excluded_titles(search_cfg):
        normalized_excluded = normalize_title(excluded)
        if normalized_excluded and f" {normalized_excluded} " in padded:
            return TitleEligibility(False, f"excluded_title:{normalized_excluded}")

    for included in load_include_titles(search_cfg):
        normalized_included = normalize_title(included)
        included_tokens = normalized_included.split()
        title_tokens = set(normalized.split())
        if normalized_included and (
            f" {normalized_included} " in padded
            or all(token in title_tokens for token in included_tokens)
        ):
            return TitleEligibility(True, "accepted")

    return TitleEligibility(False, "no_target_title_match")


def reconcile_unscored_jobs(
    conn: sqlite3.Connection,
    search_cfg: dict | None = None,
) -> dict[str, int]:
    """Re-evaluate unscored jobs so config changes affect the paid-work queues."""
    search_cfg = search_cfg or config.load_search_config()
    rows = conn.execute(
        "SELECT url, title, discovery_status, discovery_rejection_reason, discovery_checked_at "
        "FROM jobs WHERE fit_score IS NULL "
        "AND tailored_resume_path IS NULL AND applied_at IS NULL "
        "AND COALESCE(strategy, '') != 'external_upload'"
    ).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    accepted = 0
    rejected = 0
    changed = 0

    for row in rows:
        result = classify_title(row[1], search_cfg)
        status = "accepted" if result.accepted else "rejected"
        reason = None if result.accepted else result.reason
        classification_changed = row[2] != status or row[3] != reason
        if classification_changed:
            changed += 1
        if classification_changed or not row[4]:
            conn.execute(
                "UPDATE jobs SET discovery_status = ?, discovery_rejection_reason = ?, "
                "discovery_checked_at = ? WHERE url = ?",
                (status, reason, now, row[0]),
            )
        if result.accepted:
            accepted += 1
        else:
            rejected += 1

    conn.commit()
    return {"checked": len(rows), "accepted": accepted, "rejected": rejected, "changed": changed}
