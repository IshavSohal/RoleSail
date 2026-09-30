from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rolesail.database import (
    get_jobs_by_stage,
    init_db,
    is_job_within_retention_window,
    normalize_posted_at,
    normalize_relative_posted_dates,
)
from rolesail.discovery import workday
from rolesail.discovery.filters import (
    classify_title,
    load_include_titles,
    reconcile_unscored_jobs,
)

APP_AI_CONFIG = {
    "include_titles": [
        "software engineer",
        "software developer",
        "frontend engineer",
        "backend engineer",
        "full stack developer",
        "web developer",
        "ai engineer",
        "machine learning engineer",
        "ml engineer",
        "sde",
    ],
    "exclude_titles": ["senior", "staff", "principal", "lead", "manager"],
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Posted Today", "2026-08-17"),
        ("Posted Yesterday", "2026-08-16"),
        ("Posted 2 Days Ago", "2026-08-15"),
        ("Posted 30+ Days Ago", "2026-07-18"),
    ],
)
def test_normalize_posted_at_converts_relative_labels(value, expected) -> None:
    assert normalize_posted_at(value, "2026-08-17T12:00:00+00:00") == expected


def test_normalize_relative_posted_dates_updates_existing_jobs(tmp_path) -> None:
    conn = init_db(tmp_path / "relative-posted.db")
    conn.execute(
        "INSERT INTO jobs (url, discovered_at, posted_at) VALUES (?, ?, ?)",
        (
            "https://example.com/job",
            "2026-08-17T12:00:00+00:00",
            "Posted Yesterday",
        ),
    )
    conn.commit()

    assert normalize_relative_posted_dates(conn) == 1
    assert conn.execute("SELECT posted_at FROM jobs").fetchone()[0] == "2026-08-16"
    assert normalize_relative_posted_dates(conn) == 0


def test_discovery_retention_rejects_only_known_stale_postings() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

    assert is_job_within_retention_window("2026-08-28", now=now)
    assert not is_job_within_retention_window("2026-08-26", now=now)
    assert not is_job_within_retention_window(
        "2 weeks ago",
        reference_at="2026-09-03T12:00:00+00:00",
        now=now,
    )
    assert is_job_within_retention_window(None, now=now)
    assert is_job_within_retention_window("not-a-date", now=now)


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer I",
        "Front-End Engineer",
        "Engineer, Frontend",
        "Back End Engineer",
        "Full-Stack Developer",
        "Web Developer",
        "Applied AI Engineer",
        "Machine-Learning Engineer",
        "ML Engineer",
        "S.D.E. I",
    ],
)
def test_target_software_title_variants_are_accepted(title: str) -> None:
    assert classify_title(title, APP_AI_CONFIG).reason == "accepted"


@pytest.mark.parametrize(
    "title",
    [
        "ASIC DFT Technical Leader",
        "Senior Analyst – Operations Excellence",
        "Technician, Robotics",
        "CNC Technician- Midnight Shift",
        "Production Operator",
        "Controls Technician (1st Shift)",
        "Maintenance Technician - 2nd shift",
        "Mould Maker",
        "Buyer, Sr.",
    ],
)
def test_irrelevant_reported_titles_are_rejected(title: str) -> None:
    assert classify_title(title, APP_AI_CONFIG).reason != "accepted"


def test_exclusion_overrides_valid_target_title() -> None:
    result = classify_title("Senior Software Engineer", APP_AI_CONFIG)
    assert not result.accepted
    assert result.reason == "excluded_title:senior"


def test_missing_title_is_rejected() -> None:
    assert classify_title(None, APP_AI_CONFIG).reason == "missing_title"


def test_legacy_config_falls_back_to_query_titles() -> None:
    legacy = {"queries": [{"query": "Backend Engineer", "tier": 1}]}
    assert load_include_titles(legacy) == ["Backend Engineer"]
    assert classify_title("Backend Engineer, Payments", legacy).accepted
    assert not classify_title("Frontend Engineer", legacy).accepted


def test_explicit_include_titles_override_search_queries() -> None:
    search_cfg = {
        "queries": [{"query": "Developer", "tier": 1}],
        "include_titles": ["AI Engineer"],
    }
    assert classify_title("AI Engineer", search_cfg).accepted
    assert not classify_title("CRM Developer", search_cfg).accepted


def test_reconciliation_rejects_and_reaccepts_unscored_jobs(tmp_path) -> None:
    conn = init_db(tmp_path / "jobs.db")
    conn.executemany(
        "INSERT INTO jobs (url, title, full_description) VALUES (?, ?, ?)",
        [
            ("https://example.com/software", "Software Engineer", "Description"),
            ("https://example.com/cnc", "CNC Technician", "Description"),
        ],
    )
    conn.commit()

    first = reconcile_unscored_jobs(conn, APP_AI_CONFIG)
    assert first["accepted"] == 1
    assert first["rejected"] == 1
    assert [job["title"] for job in get_jobs_by_stage(conn, "pending_score")] == [
        "Software Engineer"
    ]

    expanded = {**APP_AI_CONFIG, "include_titles": [*APP_AI_CONFIG["include_titles"], "CNC Technician"]}
    second = reconcile_unscored_jobs(conn, expanded)
    assert second["accepted"] == 2
    assert second["rejected"] == 0


def test_reconciliation_leaves_scored_tailored_and_applied_jobs_untouched(tmp_path) -> None:
    conn = init_db(tmp_path / "completed.db")
    conn.executemany(
        "INSERT INTO jobs (url, title, fit_score, tailored_resume_path, applied_at) "
        "VALUES (?, 'CNC Technician', ?, ?, ?)",
        [
            ("https://example.com/scored", 7, None, None),
            ("https://example.com/tailored", None, "resume.tex", None),
            ("https://example.com/applied", None, None, "2026-08-01T00:00:00+00:00"),
        ],
    )
    conn.commit()

    result = reconcile_unscored_jobs(conn, APP_AI_CONFIG)

    assert result["checked"] == 0
    statuses = conn.execute(
        "SELECT discovery_status FROM jobs ORDER BY url"
    ).fetchall()
    assert [row[0] for row in statuses] == ["accepted", "accepted", "accepted"]


def test_reconciliation_leaves_manual_external_imports_accepted(tmp_path) -> None:
    conn = init_db(tmp_path / "external-import.db")
    conn.execute(
        "INSERT INTO jobs (url, title, strategy, discovery_status) VALUES (?, ?, ?, ?)",
        (
            "https://example.com/imported-job",
            "Imported job from example.com",
            "external_upload",
            "accepted",
        ),
    )
    conn.commit()

    result = reconcile_unscored_jobs(conn, APP_AI_CONFIG)

    assert result["checked"] == 0
    row = conn.execute(
        "SELECT discovery_status, discovery_rejection_reason FROM jobs"
    ).fetchone()
    assert tuple(row) == ("accepted", None)


def test_workday_rejects_titles_before_returning_jobs_for_detail_fetch(monkeypatch) -> None:
    monkeypatch.setattr(
        workday,
        "workday_search",
        lambda *_args, **_kwargs: {
            "total": 2,
            "jobPostings": [
                {
                    "title": "Software Engineer",
                    "locationsText": "Toronto, ON, Canada",
                    "externalPath": "/software",
                },
                {
                    "title": "CNC Technician",
                    "locationsText": "Toronto, ON, Canada",
                    "externalPath": "/cnc",
                },
            ],
        },
    )
    monkeypatch.setattr(workday.config, "load_search_config", lambda: APP_AI_CONFIG)

    jobs = workday.search_employer(
        "example",
        {"name": "Example", "base_url": "https://example.com", "tenant": "x", "site_id": "x"},
        "software engineer",
        location_filter=False,
    )

    assert [job["title"] for job in jobs] == ["Software Engineer"]
