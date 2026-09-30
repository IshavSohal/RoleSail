"""Tests for external dashboard job imports."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
import yaml

from rolesail import config, dashboard_server
from rolesail.apply.prompt import _build_salary_section
from rolesail.config import location_filter_is_mandatory, location_is_allowed
from rolesail.dashboard_data import load_dashboard_jobs
from rolesail.dashboard_server import (
    DashboardHTTPServer,
    DashboardRequestHandler,
    cancel_tailoring,
    clear_tailored_resume,
    delete_job,
    import_external_job,
    job_import_status,
    load_dashboard_company_logo,
    load_dashboard_resume,
    load_dashboard_settings,
    mark_job_applied,
    normalize_job_url,
    save_dashboard_profile,
    save_dashboard_resume,
    save_dashboard_searches,
    start_tailoring,
    tailoring_status,
    unmark_job_applied,
)
from rolesail.database import get_connection, init_db
from rolesail.enrichment.detail import extract_job_metadata, scrape_detail_page
from rolesail.view import applied_view, format_applied_at, format_posted_at


@pytest.fixture
def db(tmp_path):
    connection = init_db(tmp_path / "rolesail.db")
    yield connection
    connection.close()


def test_normalize_job_url() -> None:
    assert (
        normalize_job_url(" HTTPS://Example.COM/jobs/123?source=test#apply ")
        == "https://example.com/jobs/123?source=test"
    )


@pytest.mark.parametrize(
    "url",
    ["", "example.com/job", "file:///tmp/job", "https://user:pass@example.com/job"],
)
def test_normalize_job_url_rejects_invalid_values(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_job_url(url)


def test_import_external_job_and_duplicate(db) -> None:
    first = import_external_job("https://example.com/jobs/123", db)
    second = import_external_job("https://example.com/jobs/123#apply", db)

    assert first["created"] is True
    assert first["status"] == "pending"
    assert second["created"] is False
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1

    row = db.execute(
        "SELECT strategy, site, application_url FROM jobs"
    ).fetchone()
    assert row["strategy"] == "external_upload"
    assert row["site"] == "example.com"
    assert row["application_url"] == "https://example.com/jobs/123"
    assert job_import_status(first["url"], db)["status"] == "pending"


def test_import_external_konrad_job_uses_canonical_source(db) -> None:
    imported = import_external_job(
        "https://www.konrad.com/careers/job/full-stack-developer_7860136003",
        db,
    )

    row = db.execute(
        "SELECT company, site FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert tuple(row) == ("Konrad", "Konrad")


def test_import_external_salesforce_job_uses_canonical_source(db) -> None:
    imported = import_external_job(
        "https://www.salesforce.com/company/careers/jobs/JR356939/ai-builder/",
        db,
    )

    row = db.execute(
        "SELECT company, site FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert tuple(row) == ("Salesforce", "Salesforce")


def test_import_external_ashby_job_uses_board_as_company(db) -> None:
    imported = import_external_job(
        "https://jobs.ashbyhq.com/Spectral%20Labs/"
        "99c79fda-2125-4e09-9313-97e91b730d75",
        db,
    )

    row = db.execute(
        "SELECT title, company, site FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert tuple(row) == (
        "Imported job from Spectral Labs",
        "Spectral Labs",
        "Spectral Labs",
    )


def test_reimport_repairs_existing_ashby_company_placeholder(db) -> None:
    url = (
        "https://jobs.ashbyhq.com/Spectral%20Labs/"
        "99c79fda-2125-4e09-9313-97e91b730d75"
    )
    db.execute(
        "INSERT INTO jobs (url, title, company, site, full_description) "
        "VALUES (?, 'Imported job from jobs.ashbyhq.com', "
        "'jobs.ashbyhq.com', 'jobs.ashbyhq.com', 'Existing description')",
        (url,),
    )
    db.commit()

    imported = import_external_job(url, db)

    assert imported["created"] is False
    row = db.execute(
        "SELECT title, company, site FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    assert tuple(row) == (
        "Imported job from Spectral Labs",
        "Spectral Labs",
        "Spectral Labs",
    )


def test_backfill_konrad_metadata_uses_greenhouse_location(db, monkeypatch) -> None:
    imported = import_external_job(
        "https://www.konrad.com/careers/job/full-stack-developer_7860136003",
        db,
    )
    monkeypatch.setattr(
        "rolesail.discovery.greenhouse.fetch_company_jobs",
        lambda board: [
            {
                "id": 7860136003,
                "title": "Full Stack Developer",
                "location": {"name": "Toronto"},
            }
        ]
        if board == "konradgroup"
        else [],
    )

    dashboard_server._backfill_external_employer_metadata(db, imported["url"])

    row = db.execute(
        "SELECT title, company, site, location FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert tuple(row) == (
        "Full Stack Developer",
        "Konrad",
        "Konrad",
        "Toronto",
    )


def test_duplicate_incomplete_import_is_requeued(db) -> None:
    imported = import_external_job("https://example.com/jobs/retry", db)
    db.execute(
        "UPDATE jobs SET detail_scraped_at = ?, detail_error = ?, "
        "fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
        (
            "2026-08-01T12:00:00+00:00",
            "old extraction error",
            0,
            "old scoring error",
            "2026-08-01T12:01:00+00:00",
            imported["url"],
        ),
    )
    db.commit()

    retried = import_external_job(imported["url"], db)

    assert retried["created"] is False
    assert retried["status"] == "pending"
    assert retried["enrichment_pending"] is True
    row = db.execute(
        "SELECT detail_scraped_at, detail_error, fit_score, score_reasoning, scored_at "
        "FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert tuple(row) == (None, None, None, None, None)


def test_duplicate_failed_score_is_requeued_without_rescraping(db) -> None:
    imported = import_external_job("https://example.com/jobs/retry-score", db)
    db.execute(
        "UPDATE jobs SET full_description = ?, detail_scraped_at = ?, fit_score = 0, "
        "score_reasoning = ?, scored_at = ? WHERE url = ?",
        (
            "Python API role",
            "2026-08-01T12:00:00+00:00",
            "LLM error: unable to open database file",
            "2026-08-01T12:01:00+00:00",
            imported["url"],
        ),
    )
    db.commit()

    retried = import_external_job(imported["url"], db)

    assert retried["status"] == "scoring"
    assert retried["enrichment_pending"] is True
    row = db.execute(
        "SELECT full_description, detail_scraped_at, fit_score, score_reasoning, scored_at "
        "FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert tuple(row) == (
        "Python API role",
        "2026-08-01T12:00:00+00:00",
        None,
        None,
        None,
    )


def test_enrich_external_job_automatically_scores_import(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "automatic-score.db"
    connection = init_db(db_path)
    imported = import_external_job("https://example.com/jobs/score-me", connection)
    connection.close()
    calls = []

    def fake_scrape(conn, _site, jobs, delay):
        assert jobs == [(imported["url"], "Imported job from example.com")]
        assert delay == 0
        conn.execute(
            "UPDATE jobs SET full_description = ?, detail_scraped_at = ? WHERE url = ?",
            ("Python API role", "2026-08-09T12:00:00+00:00", imported["url"]),
        )
        conn.commit()

    monkeypatch.setattr(dashboard_server, "get_connection", lambda: get_connection(db_path))
    monkeypatch.setattr(config, "get_tier", lambda: 2)
    monkeypatch.setattr(config, "location_is_allowed", lambda _location: True)
    monkeypatch.setattr("rolesail.enrichment.detail.scrape_site_batch", fake_scrape)
    monkeypatch.setattr(
        "rolesail.scoring.scorer.run_scoring",
        lambda **kwargs: calls.append(kwargs) or {"scored": 1},
    )

    dashboard_server.enrich_external_job(imported["url"])

    assert calls == [{"target_url": imported["url"], "workers": 1}]


def test_enrich_external_job_scores_when_location_is_unknown(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "unknown-location.db"
    connection = init_db(db_path)
    imported = import_external_job("https://example.com/jobs/unknown-location", connection)
    connection.execute(
        "UPDATE jobs SET full_description = ?, detail_scraped_at = ?, "
        "discovery_status = 'rejected', "
        "discovery_rejection_reason = 'outside_allowed_countries' WHERE url = ?",
        ("Python API role", "2026-08-09T12:00:00+00:00", imported["url"]),
    )
    connection.commit()
    connection.close()
    calls = []

    monkeypatch.setattr(dashboard_server, "get_connection", lambda: get_connection(db_path))
    monkeypatch.setattr(config, "get_tier", lambda: 2)
    monkeypatch.setattr(
        config,
        "location_is_allowed",
        lambda _location: (_ for _ in ()).throw(AssertionError("unknown location was filtered")),
    )
    monkeypatch.setattr(
        "rolesail.enrichment.detail.scrape_site_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("job was rescraped")),
    )
    monkeypatch.setattr(
        "rolesail.scoring.scorer.run_scoring",
        lambda **kwargs: calls.append(kwargs) or {"scored": 1},
    )

    dashboard_server.enrich_external_job(imported["url"])

    assert calls == [{"target_url": imported["url"], "workers": 1}]
    row = get_connection(db_path).execute(
        "SELECT discovery_status, discovery_rejection_reason FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert tuple(row) == ("accepted", None)


def test_enrich_external_amazon_job_uses_exact_api_record(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "amazon-import.db"
    connection = init_db(db_path)
    url = "https://www.amazon.jobs/en/jobs/10502743/software-engineer"
    imported = import_external_job(url, connection)
    connection.close()

    monkeypatch.setattr(dashboard_server, "get_connection", lambda: get_connection(db_path))
    monkeypatch.setattr(config, "get_tier", lambda: 1)
    monkeypatch.setattr(
        "rolesail.discovery.greenhouse.fetch_amazon_job",
        lambda job_id: {
            "id": job_id,
            "title": "Software Development Engineer, Early Career - 2026",
            "company": "Amazon",
            "location": "Vancouver, British Columbia, CAN",
            "content": (
                "Build services for customers.<br/><br/>"
                "Basic Qualifications<br/><br/>Programming experience.<br/><br/>"
                "Preferred Qualifications<br/><br/>Internship experience. " * 5
            ),
            "content_is_full": True,
            "salary": "CAN, BC, Vancouver - 89,700.00 - 149,800.00 CAD annually",
            "application_url": "https://account.amazon.jobs/jobs/10502743/apply",
            "posted_at": "August 15, 2026",
        },
    )

    def fail_generic_scrape(*_args, **_kwargs):
        raise AssertionError("generic scraper should not run for a complete Amazon API record")

    monkeypatch.setattr(
        "rolesail.enrichment.detail.scrape_site_batch",
        fail_generic_scrape,
    )

    dashboard_server.enrich_external_job(imported["url"])

    row = get_connection(db_path).execute(
        "SELECT title, company, site, salary, location, full_description, "
        "application_url, detail_error FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert row["title"] == "Software Development Engineer, Early Career - 2026"
    assert row["company"] == "Amazon"
    assert row["site"] == "Amazon"
    assert row["salary"] == "CAN, BC, Vancouver - 89,700.00 - 149,800.00 CAD annually"
    assert row["location"] == "Vancouver, British Columbia, CAN"
    assert "Basic Qualifications" in row["full_description"]
    assert row["application_url"] == "https://account.amazon.jobs/jobs/10502743/apply"
    assert row["detail_error"] is None


def test_enrich_external_salesforce_job_uses_workday_api(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "salesforce-import.db"
    connection = init_db(db_path)
    url = (
        "https://www.salesforce.com/company/careers/jobs/JR356939/"
        "ai-builder-emerging-talent/"
    )
    imported = import_external_job(url, connection)
    connection.close()

    monkeypatch.setattr(dashboard_server, "get_connection", lambda: get_connection(db_path))
    monkeypatch.setattr(config, "get_tier", lambda: 1)
    monkeypatch.setattr(
        "rolesail.discovery.workday.workday_search",
        lambda employer, search_text, limit: {
            "jobPostings": [{
                "title": "AI Builder, Emerging Talent",
                "externalPath": "/job/AI-Builder_JR356939-1",
                "bulletFields": ["JR356939"],
            }]
        },
    )
    monkeypatch.setattr(
        "rolesail.discovery.workday.workday_detail",
        lambda employer, path: {
            "jobPostingInfo": {
                "title": "AI Builder, Emerging Talent",
                "jobReqId": "JR356939",
                "location": "California - San Francisco",
                "additionalLocations": ["Illinois - Chicago", "New York - New York"],
                "jobDescription": "<p>Build production AI agents for customers.</p>" * 20,
                "externalUrl": "https://salesforce.wd12.myworkdayjobs.com/job/JR356939",
                "startDate": "2026-08-24",
            }
        },
    )

    monkeypatch.setattr(
        "rolesail.enrichment.detail.scrape_site_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic scraper should not run for a Workday-backed URL")
        ),
    )

    dashboard_server.enrich_external_job(imported["url"])

    row = get_connection(db_path).execute(
        "SELECT title, company, site, location, full_description, application_url, "
        "posted_at, detail_error FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert row["title"] == "AI Builder, Emerging Talent"
    assert row["company"] == "Salesforce"
    assert row["site"] == "Salesforce"
    assert row["location"] == (
        "California - San Francisco; Illinois - Chicago; New York - New York"
    )
    assert "Build production AI agents" in row["full_description"]
    assert row["application_url"].endswith("/JR356939")
    assert row["posted_at"] == "2026-08-24"
    assert row["detail_error"] is None


def test_detail_scrape_preserves_http_403_error() -> None:
    class Response:
        status = 403

    class Page:
        def goto(self, _url, timeout):
            assert timeout == 45000
            return Response()

    result = scrape_detail_page(Page(), "https://example.com/jobs/blocked")

    assert result["status"] == "error"
    assert result["error"] == "HTTP 403"


def test_import_status_waits_for_automatic_score(db, monkeypatch) -> None:
    imported = import_external_job("https://example.com/jobs/scoring-status", db)
    db.execute(
        "UPDATE jobs SET full_description = ?, detail_scraped_at = ? WHERE url = ?",
        ("Python API role", "2026-08-09T12:00:00+00:00", imported["url"]),
    )
    db.commit()
    monkeypatch.setattr(config, "get_tier", lambda: 2)

    assert job_import_status(imported["url"], db)["status"] == "scoring"

    db.execute("UPDATE jobs SET fit_score = 8 WHERE url = ?", (imported["url"],))
    db.commit()
    status = job_import_status(imported["url"], db)
    assert status["status"] == "complete"
    assert status["score"] == 8


def test_mark_job_applied(db) -> None:
    imported = import_external_job("https://example.com/jobs/applied", db)

    result = mark_job_applied(imported["url"], db)
    repeated = mark_job_applied(imported["url"], db)

    assert result["updated"] is True
    assert result["status"] == "applied"
    assert repeated["applied_at"] == result["applied_at"]
    row = db.execute(
        "SELECT applied_at, apply_status FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert row["applied_at"] == result["applied_at"]
    assert row["apply_status"] == "manually_applied"


def test_mark_job_applied_reports_missing_job(db) -> None:
    result = mark_job_applied("https://example.com/jobs/missing", db)
    assert result["updated"] is False
    assert result["status"] == "missing"


def test_unmark_job_applied(db) -> None:
    imported = import_external_job("https://example.com/jobs/unapply", db)
    mark_job_applied(imported["url"], db)

    result = unmark_job_applied(imported["url"], db)

    assert result == {
        "updated": True,
        "url": imported["url"],
        "title": imported["title"],
        "status": "active",
        "applied_at": None,
    }
    row = db.execute(
        "SELECT applied_at, apply_status FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert row["applied_at"] is None
    assert row["apply_status"] is None


def test_unmark_job_applied_reports_missing_job(db) -> None:
    result = unmark_job_applied("https://example.com/jobs/missing", db)
    assert result["updated"] is False
    assert result["status"] == "missing"


def test_delete_job(db) -> None:
    imported = import_external_job("https://example.com/jobs/delete-me", db)

    result = delete_job(imported["url"], db)
    missing = delete_job(imported["url"], db)

    assert result["deleted"] is True
    assert result["status"] == "deleted"
    assert missing["deleted"] is False
    assert missing["status"] == "missing"
    assert (
        db.execute(
            "SELECT COUNT(*) FROM jobs WHERE url = ?",
            (imported["url"],),
        ).fetchone()[0]
        == 0
    )


def test_delete_job_requires_url(db) -> None:
    with pytest.raises(ValueError, match="Job URL is required"):
        delete_job("", db)


def test_clear_tailored_resume_deletes_files_and_preserves_attempts(
    db, tmp_path, monkeypatch
) -> None:
    tailored_dir = tmp_path / "tailored"
    tailored_dir.mkdir()
    monkeypatch.setattr(config, "TAILORED_DIR", tailored_dir)

    stem = "Acme_Engineer_Tailored_Resume"
    tex_path = tailored_dir / f"{stem}.tex"
    siblings = [
        tex_path,
        tailored_dir / f"{stem}.pdf",
        tailored_dir / f"{stem}.txt",
        tailored_dir / f"{stem}_REPORT.json",
        tailored_dir / f"{stem}_JOB.txt",
    ]
    for path in siblings:
        path.write_text("artifact", encoding="utf-8")

    imported = import_external_job("https://example.com/jobs/clear-tailored", db)
    cover_path = str(tmp_path / "cover.txt")
    db.execute(
        """
        UPDATE jobs
        SET tailored_resume_path = ?,
            tailored_at = ?,
            tailor_attempts = ?,
            cover_letter_path = ?,
            cover_letter_at = ?
        WHERE url = ?
        """,
        (
            str(tex_path),
            "2026-01-01T00:00:00+00:00",
            3,
            cover_path,
            "2026-01-02T00:00:00+00:00",
            imported["url"],
        ),
    )
    db.commit()

    result = clear_tailored_resume(imported["url"], db)

    assert result["cleared"] is True
    assert result["status"] == "cleared"
    assert set(result["deleted_files"]) == {str(path) for path in siblings}
    for path in siblings:
        assert not path.exists()

    row = db.execute(
        """
        SELECT tailored_resume_path, tailored_at, tailor_attempts,
               cover_letter_path, cover_letter_at
        FROM jobs WHERE url = ?
        """,
        (imported["url"],),
    ).fetchone()
    assert row["tailored_resume_path"] is None
    assert row["tailored_at"] is None
    assert row["tailor_attempts"] == 3
    assert row["cover_letter_path"] == cover_path
    assert row["cover_letter_at"] == "2026-01-02T00:00:00+00:00"


def test_clear_tailored_resume_reports_missing_and_not_tailored(db) -> None:
    missing = clear_tailored_resume("https://example.com/jobs/missing", db)
    assert missing == {
        "cleared": False,
        "url": "https://example.com/jobs/missing",
        "status": "missing",
    }

    imported = import_external_job("https://example.com/jobs/untailored", db)
    not_tailored = clear_tailored_resume(imported["url"], db)
    assert not_tailored == {
        "cleared": False,
        "url": imported["url"],
        "status": "not_tailored",
    }


def test_clear_tailored_resume_rejects_path_outside_tailored_dir(
    db, tmp_path, monkeypatch
) -> None:
    tailored_dir = tmp_path / "tailored"
    tailored_dir.mkdir()
    monkeypatch.setattr(config, "TAILORED_DIR", tailored_dir)

    outside = tmp_path / "outside.tex"
    outside.write_text("private", encoding="utf-8")
    imported = import_external_job("https://example.com/jobs/bad-path", db)
    db.execute(
        """
        UPDATE jobs
        SET tailored_resume_path = ?, tailored_at = ?, tailor_attempts = 2
        WHERE url = ?
        """,
        (str(outside), "2026-01-01T00:00:00+00:00", imported["url"]),
    )
    db.commit()

    with pytest.raises(PermissionError):
        clear_tailored_resume(imported["url"], db)

    assert outside.exists()
    row = db.execute(
        "SELECT tailored_resume_path, tailored_at, tailor_attempts FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert row["tailored_resume_path"] == str(outside)
    assert row["tailored_at"] == "2026-01-01T00:00:00+00:00"
    assert row["tailor_attempts"] == 2


@pytest.fixture
def settings_files(tmp_path, monkeypatch):
    profile_path = tmp_path / "profile.json"
    search_path = tmp_path / "searches.yaml"
    resume_path = tmp_path / "resume.txt"
    resume_tex_path = tmp_path / "resume.tex"
    resume_pdf_path = tmp_path / "resume.pdf"
    profile_path.write_text(
        json.dumps(
            {
                "personal": {
                    "full_name": "Example User",
                    "email": "user@example.com",
                    "password": "stored-secret",
                },
                "experience": {"target_role": "Software Engineer"},
                "skills_boundary": {
                    "programming_languages": ["Python", "Custom Language"],
                    "frameworks": ["FastAPI"],
                    "tools": ["Custom Platform"],
                },
                "resume_facts": {
                    "preserved_companies": ["Example Corp"],
                    "preserved_projects": ["RoleSail"],
                    "preserved_school": "Example University",
                    "real_metrics": ["50% faster"],
                },
                "custom_section": {"keep": True},
            }
        ),
        encoding="utf-8",
    )
    search_path.write_text(
        yaml.safe_dump(
            {
                "defaults": {
                    "location": "Canada",
                    "distance": 25,
                    "hours_old": 72,
                    "results_per_site": 50,
                },
                "queries": [{"query": "Software Engineer", "tier": 1}],
                "locations": [{"location": "Canada", "remote": True}],
                "allowed_countries": ["Canada", "United States"],
                "location_accept": ["Toronto"],
                "location_reject_non_remote": ["India"],
                "priority_titles": ["Backend Engineer", "Custom Title"],
                "exclude_titles": ["Senior Director"],
                "custom_search_key": "keep",
                "custom_null": None,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    resume_path.write_text("Existing resume\nExperience\n", encoding="utf-8")
    resume_tex_path.write_text(
        "\\documentclass{article}\n\\begin{document}\nResume\n\\end{document}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server.config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(dashboard_server.config, "SEARCH_CONFIG_PATH", search_path)
    monkeypatch.setattr(dashboard_server.config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(
        dashboard_server.config,
        "RESUME_TEX_PATH",
        resume_tex_path,
    )
    monkeypatch.setattr(dashboard_server.config, "RESUME_PDF_PATH", resume_pdf_path)
    monkeypatch.setattr(
        dashboard_server,
        "_compile_latex_resume",
        lambda content: b"%PDF-1.4\n% fake tectonic output\n",
    )
    return profile_path, search_path


def test_dashboard_settings_redact_and_preserve_password(settings_files) -> None:
    profile_path, _ = settings_files
    settings = load_dashboard_settings()

    assert settings["password_configured"] is True
    assert "password" not in settings["profile"]["personal"]

    profile = settings["profile"]
    profile["experience"]["target_role"] = "Backend Engineer"
    profile["experience"]["years_of_experience_total"] = "2.5"
    profile["compensation"] = {"salary_expectation": "100000"}
    saved = save_dashboard_profile(profile)

    stored = json.loads(profile_path.read_text(encoding="utf-8"))
    assert stored["personal"]["password"] == "stored-secret"
    assert stored["experience"]["target_role"] == "Backend Engineer"
    assert stored["experience"]["years_of_experience_total"] == 2.5
    assert stored["compensation"]["salary_expectation"] == 100000
    assert stored["custom_section"] == {"keep": True}
    assert "password" not in saved["profile"]["personal"]


def test_dashboard_search_settings_save_yaml(settings_files) -> None:
    _, search_path = settings_files
    searches = load_dashboard_settings()["searches"]
    searches["queries"].append({"query": "AI Engineer", "tier": "2"})
    searches["defaults"]["distance"] = "30"
    searches["locations"][0]["remote"] = "false"

    save_dashboard_searches(searches)

    stored = yaml.safe_load(search_path.read_text(encoding="utf-8"))
    assert stored["queries"][-1] == {"query": "AI Engineer", "tier": 2}
    assert stored["defaults"]["distance"] == 30
    assert stored["locations"][0]["remote"] is False
    assert stored["custom_search_key"] == "keep"


def test_dashboard_search_settings_reject_invalid_tier(settings_files) -> None:
    searches = load_dashboard_settings()["searches"]
    searches["queries"][0]["tier"] = 9
    with pytest.raises(ValueError, match="tiers"):
        save_dashboard_searches(searches)


def test_dashboard_search_settings_reject_non_text_values(settings_files) -> None:
    searches = load_dashboard_settings()["searches"]
    searches["queries"][0]["query"] = None
    with pytest.raises(ValueError, match="title"):
        save_dashboard_searches(searches)


def test_dashboard_tag_editor_lists_round_trip_custom_values(settings_files) -> None:
    profile_path, search_path = settings_files
    settings = load_dashboard_settings()
    settings["profile"]["skills_boundary"]["tools"].append("In-house Tool")
    settings["profile"]["resume_facts"]["real_metrics"].append("99.9% uptime")
    settings["searches"]["allowed_countries"].append("Mexico")
    settings["searches"]["priority_titles"].append("Developer Advocate (AI)")

    save_dashboard_profile(settings["profile"])
    save_dashboard_searches(settings["searches"])

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    searches = yaml.safe_load(search_path.read_text(encoding="utf-8"))
    assert profile["skills_boundary"]["programming_languages"] == [
        "Python",
        "Custom Language",
    ]
    assert profile["skills_boundary"]["tools"][-1] == "In-house Tool"
    assert profile["resume_facts"]["real_metrics"][-1] == "99.9% uptime"
    assert searches["allowed_countries"][-1] == "Mexico"
    assert searches["priority_titles"][-1] == "Developer Advocate (AI)"
    assert searches["exclude_titles"] == ["Senior Director"]


def test_dashboard_settings_reject_non_text_list_items(settings_files) -> None:
    settings = load_dashboard_settings()
    settings["profile"]["skills_boundary"] = {"tools": [123]}
    with pytest.raises(ValueError, match="only text"):
        save_dashboard_profile(settings["profile"])

    settings["searches"]["allowed_countries"] = ["Canada", 123]
    with pytest.raises(ValueError, match="only text"):
        save_dashboard_searches(settings["searches"])


def test_dashboard_settings_reject_invalid_numbers(settings_files) -> None:
    settings = load_dashboard_settings()
    settings["profile"]["compensation"] = {"salary_expectation": "-1"}
    with pytest.raises(ValueError, match="non-negative"):
        save_dashboard_profile(settings["profile"])

    settings["searches"]["defaults"]["distance"] = "nan"
    with pytest.raises(ValueError, match="non-negative"):
        save_dashboard_searches(settings["searches"])


def test_salary_prompt_accepts_numeric_profile_values() -> None:
    section = _build_salary_section(
        {
            "compensation": {
                "salary_expectation": 100000,
                "salary_currency": "CAD",
            }
        }
    )
    assert "120000" in section


def test_dashboard_search_settings_can_clear_numeric_default(settings_files) -> None:
    _, search_path = settings_files
    searches = load_dashboard_settings()["searches"]
    searches["defaults"]["distance"] = {"__rolesail_delete__": True}

    save_dashboard_searches(searches)

    stored = yaml.safe_load(search_path.read_text(encoding="utf-8"))
    assert "distance" not in stored["defaults"]
    assert "custom_null" in stored and stored["custom_null"] is None


def test_dashboard_resume_load_and_replace(settings_files) -> None:
    assert (
        dashboard_server.MAX_RESUME_REQUEST_BYTES
        >= dashboard_server.MAX_RESUME_BYTES * 6
    )
    result = load_dashboard_resume()
    assert result == {
        "exists": True,
        "filename": "resume.txt",
        "format": "txt",
        "content": "Existing resume\nExperience\n",
    }

    updated = save_dashboard_resume(
        "updated-resume.txt",
        "Updated resume\nProjects\n",
    )

    assert updated["content"] == "Updated resume\nProjects\n"
    assert (
        dashboard_server.config.RESUME_PATH.read_text(encoding="utf-8")
        == "Updated resume\nProjects\n"
    )

    latex = save_dashboard_resume(
        "updated-resume.tex",
        "\\documentclass{article}\n\\begin{document}\nUpdated\n\\end{document}\n",
    )
    assert latex["format"] == "tex"
    assert latex["pdf_available"] is True
    assert load_dashboard_resume("tex")["content"] == latex["content"]
    assert (
        dashboard_server.config.RESUME_PDF_PATH.read_bytes().startswith(b"%PDF-")
    )


@pytest.mark.parametrize("filename", ["resume.pdf", "../resume.txt", ""])
def test_dashboard_resume_rejects_invalid_filename(
    settings_files,
    filename,
) -> None:
    with pytest.raises(ValueError):
        save_dashboard_resume(filename, "Resume")


def test_dashboard_resume_rejects_empty_content(settings_files) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        save_dashboard_resume("resume.txt", " \n\t")


def test_remove_latex_comments_preserves_escaped_and_verbatim_percent() -> None:
    source = (
        "% heading comment\n"
        "Value 10\\% % inline comment\n"
        "Escaped slash \\\\% removed\n"
        "\\verb|literal % value| % trailing comment\n"
        "\\begin{verbatim}\n"
        "literal % value\n"
        "\\end{verbatim}\n"
        "\\begin{document}Done\\end{document}\n"
    )

    cleaned, count = dashboard_server.remove_latex_comments(source)

    assert count == 4
    assert cleaned == (
        "\n"
        "Value 10\\%\n"
        "Escaped slash \\\\\n"
        "\\verb|literal % value|\n"
        "\\begin{verbatim}\n"
        "literal % value\n"
        "\\end{verbatim}\n"
        "\\begin{document}Done\\end{document}\n"
    )


def test_dashboard_latex_upload_can_remove_comments(settings_files) -> None:
    source = (
        "% remove this\n"
        "\\documentclass{article}\n"
        "\\begin{document}Rate: 10\\% % and this\n"
        "\\end{document}\n"
    )

    result = save_dashboard_resume("resume.tex", source, remove_comments=True)

    assert result["comments_removed"] == 2
    assert "% remove this" not in result["content"]
    assert r"10\%" in result["content"]


def test_dashboard_resume_rejects_non_boolean_remove_comments(settings_files) -> None:
    with pytest.raises(ValueError, match="must be a boolean"):
        save_dashboard_resume("resume.tex", "Resume", remove_comments="yes")


def test_prepare_tex_for_tectonic_disables_pdftex_glyph_map() -> None:
    source = (
        "\\documentclass{article}\n"
        "\\input{glyphtounicode}\n"
        "\\pdfgentounicode=1\n"
        "\\begin{document}Hi\\end{document}\n"
    )
    prepared = dashboard_server._prepare_tex_for_tectonic(source)
    assert r"\input{glyphtounicode}" not in prepared.splitlines()
    assert "% \\input{glyphtounicode}" in prepared
    assert "% \\pdfgentounicode=1" in prepared
    assert "\\begin{document}Hi\\end{document}" in prepared


def test_dashboard_latex_compile_failure_keeps_previous_pdf(
    settings_files,
    monkeypatch,
) -> None:
    pdf_path = dashboard_server.config.RESUME_PDF_PATH
    tex_path = dashboard_server.config.RESUME_TEX_PATH
    original_tex = tex_path.read_text(encoding="utf-8")
    pdf_path.write_bytes(b"%PDF-1.4\n% previous\n")

    def fail_compile(_content: str) -> bytes:
        raise ValueError("LaTeX compilation failed:\nmissing package")

    monkeypatch.setattr(dashboard_server, "_compile_latex_resume", fail_compile)

    with pytest.raises(ValueError, match="compilation failed"):
        save_dashboard_resume(
            "broken.tex",
            "\\documentclass{article}\\begin{document}x\\end{document}",
        )

    assert tex_path.read_text(encoding="utf-8") == original_tex
    assert pdf_path.read_bytes() == b"%PDF-1.4\n% previous\n"


def test_extract_job_metadata_from_json_ld() -> None:
    metadata = extract_job_metadata(
        {
            "page_title": "Fallback title | Example",
            "json_ld": [
                {
                    "@type": "JobPosting",
                    "title": "Junior Software Engineer",
                    "datePosted": "2026-07-20",
                    "hiringOrganization": {
                        "name": "Example Corp",
                        "logo": {"@type": "ImageObject", "url": "https://example.com/logo.png"},
                    },
                    "jobLocation": {
                        "@type": "Place",
                        "address": {
                            "addressLocality": "Toronto",
                            "addressRegion": "ON",
                            "addressCountry": "Canada",
                        },
                    },
                }
            ],
        }
    )

    assert metadata == {
        "title": "Junior Software Engineer",
        "company": "Example Corp",
        "company_logo": "https://example.com/logo.png",
        "location": "Toronto, ON, Canada",
        "posted_at": "2026-07-20",
    }


def test_extract_job_metadata_uses_page_title_fallback() -> None:
    metadata = extract_job_metadata(
        {
            "page_title": "Backend Engineer | Example Careers",
            "page_icon": "https://example.com/favicon.ico",
            "json_ld": [],
        }
    )
    assert metadata["title"] == "Backend Engineer"
    assert metadata["company"] is None
    assert metadata["company_logo"] == "https://example.com/favicon.ico"


def test_extract_job_metadata_parses_greenhouse_page_title() -> None:
    metadata = extract_job_metadata(
        {
            "final_url": "https://job-boards.greenhouse.io/newsbreak/jobs/4615879006",
            "page_title": (
                "Job Application for Software Engineer, ML Infra "
                "(Junior & New Grad) at NewsBreak"
            ),
            "page_icon": "https://job-boards.greenhouse.io/favicon.ico",
            "json_ld": [],
        }
    )

    assert metadata["title"] == "Software Engineer, ML Infra (Junior & New Grad)"
    assert metadata["company"] == "NewsBreak"
    assert metadata["company_logo"] == "https://job-boards.greenhouse.io/favicon.ico"


def test_extract_job_metadata_parses_ashby_page_title() -> None:
    metadata = extract_job_metadata(
        {
            "final_url": (
                "https://jobs.ashbyhq.com/Spectral%20Labs/"
                "99c79fda-2125-4e09-9313-97e91b730d75"
            ),
            "page_title": "Engineer, New Grad @ Spectral Labs",
            "page_icon": "https://jobs.ashbyhq.com/favicon.ico",
            "json_ld": [],
        }
    )

    assert metadata["title"] == "Engineer, New Grad"
    assert metadata["company"] == "Spectral Labs"
    assert metadata["company_logo"] == "https://jobs.ashbyhq.com/favicon.ico"


def test_extract_job_metadata_resolves_relative_logo_url() -> None:
    metadata = extract_job_metadata({
        "final_url": "https://example.com/jobs/engineer",
        "json_ld": [{
            "@type": "JobPosting",
            "title": "Engineer",
            "hiringOrganization": {"name": "Example Corp", "logo": "/brand/logo.png"},
        }],
    })

    assert metadata["company_logo"] == "https://example.com/brand/logo.png"


def test_load_dashboard_company_logo_uses_stored_logo_url(db, monkeypatch) -> None:
    imported = import_external_job("https://example.com/jobs/logo", db)
    db.execute(
        "UPDATE jobs SET company = ?, company_logo = ? WHERE url = ?",
        ("Example Corp", "https://cdn.example.com/logo.png", imported["url"]),
    )
    db.commit()
    calls = []

    def fake_load(company, source_url):
        calls.append((company, source_url))
        return b"logo", "image/png"

    monkeypatch.setattr("rolesail.company_logos.load_company_logo", fake_load)

    assert load_dashboard_company_logo(imported["url"], db) == (b"logo", "image/png")
    assert calls == [("Example Corp", "https://cdn.example.com/logo.png")]


def test_load_dashboard_company_logo_backfills_existing_company(db, monkeypatch) -> None:
    imported = import_external_job("https://example.com/jobs/existing-logo", db)
    db.execute(
        "UPDATE jobs SET company = ? WHERE url = ?",
        ("OpenAI", imported["url"]),
    )
    db.commit()
    calls = []

    def fake_load(company, source_url):
        calls.append((company, source_url))
        return (b"logo", "image/x-icon") if source_url.endswith("favicon.ico") else None

    monkeypatch.setattr("rolesail.company_logos.load_company_logo", fake_load)

    assert load_dashboard_company_logo(imported["url"], db) == (b"logo", "image/x-icon")
    stored = db.execute(
        "SELECT company_logo FROM jobs WHERE url = ?", (imported["url"],)
    ).fetchone()[0]
    assert stored == "https://openai.com/favicon.ico"
    assert calls == [("OpenAI", "https://openai.com/favicon.ico")]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-20", "Posted Jul 20, 2026"),
        ("2026-07-20T14:30:00Z", "Posted Jul 20, 2026"),
        ("April  9, 2026", "Posted Apr 9, 2026"),
        ("Posted 2 Days Ago", "Posted Aug 15, 2026"),
        (None, ""),
    ],
)
def test_format_posted_at(value, expected) -> None:
    reference = "2026-08-17T12:00:00+00:00" if value == "Posted 2 Days Ago" else None
    assert format_posted_at(value, reference) == expected


def test_format_applied_at() -> None:
    assert format_applied_at("2026-07-29T15:30:00Z") == "Applied Jul 29, 2026"


@pytest.mark.parametrize(
    "location",
    [
        "Toronto, ON, Canada",
        "Vancouver, BC",
        "Seattle, WA",
        "Remote, US",
        "Austin, Texas, USA",
    ],
)
def test_country_location_policy_accepts_canada_and_us(location: str) -> None:
    config = {
        "allowed_countries": ["Canada", "United States"],
        "accept_remote_anywhere": False,
        "accept_unknown_locations": False,
    }
    assert location_is_allowed(location, config)


@pytest.mark.parametrize(
    "location",
    [
        "China - Remote",
        "Remote - India",
        "London, United Kingdom",
        "Tbilisi, Georgia",
        "Remote",
    ],
)
def test_country_location_policy_rejects_other_or_unknown_regions(location: str) -> None:
    config = {
        "allowed_countries": ["Canada", "United States"],
        "accept_remote_anywhere": False,
        "accept_unknown_locations": False,
    }
    assert not location_is_allowed(location, config)


@pytest.mark.parametrize("location", ["Bangalore, IN", "Pune, IN", "Kochi, IN"])
def test_country_location_policy_does_not_treat_india_code_as_indiana(location: str) -> None:
    config = {
        "allowed_countries": ["Canada", "United States"],
        "accept_unknown_locations": False,
    }
    assert not location_is_allowed(location, config)


def test_country_allowlist_rejects_unscoped_remote_by_default() -> None:
    config = {
        "allowed_countries": ["Canada", "United States"],
        "accept_unknown_locations": False,
    }
    assert not location_is_allowed("Remote - Europe", config)


def test_country_allowlist_makes_discovery_filter_mandatory() -> None:
    config = {
        "allowed_countries": ["Canada", "United States"],
        "greenhouse_location_filter": False,
        "bigtech_location_filter": False,
    }

    assert location_filter_is_mandatory(config)


def test_dashboard_api_imports_job(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "api.db"
    init_db(db_path)
    monkeypatch.setattr(
        dashboard_server,
        "get_connection",
        lambda: get_connection(db_path),
    )
    monkeypatch.setattr(dashboard_server, "enrich_external_job", lambda _url: None)

    def complete_discovery(server, workers):
        with server.discovery_lock:
            server.discovery_state = {
                **server.discovery_state,
                "status": "complete",
                "result": {"new": 3, "existing": 7},
                "finished_at": "2026-07-29T15:00:00+00:00",
            }

    monkeypatch.setattr(
        dashboard_server,
        "_execute_discovery",
        complete_discovery,
    )

    def complete_tailoring(request):
        return "complete", {"approved": 2, "failed": 1, "errors": 0}, None

    monkeypatch.setattr(
        dashboard_server,
        "_run_tailoring_request",
        complete_tailoring,
    )

    server = DashboardHTTPServer(
        ("127.0.0.1", 0),
        DashboardRequestHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        request = urllib.request.Request(
            f"{base_url}/api/jobs",
            data=json.dumps({"url": "https://example.com/jobs/api-test"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
            assert response.status == 201
            assert result["created"] is True

        query = urllib.parse.urlencode({"url": result["url"]})
        with urllib.request.urlopen(f"{base_url}/api/jobs/status?{query}") as response:
            status = json.load(response)
            assert status["status"] == "pending"

        connection = get_connection(db_path)
        connection.execute(
            "UPDATE jobs SET full_description = ?, fit_score = 4 WHERE url = ?",
            ("A complete job description suitable for tailoring", result["url"]),
        )
        connection.commit()

        with urllib.request.urlopen(f"{base_url}/api/jobs") as response:
            dashboard_jobs = json.load(response)
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert len(dashboard_jobs["jobs"]) == 1
            assert dashboard_jobs["jobs"][0]["url"] == result["url"]
            assert dashboard_jobs["jobs"][0]["score"] == 4
            assert dashboard_jobs["jobs"][0]["can_tailor"] is True

        individual_tailoring_request = urllib.request.Request(
            f"{base_url}/api/tailoring/job",
            data=json.dumps({"url": result["url"], "validation_mode": "normal"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(individual_tailoring_request) as response:
            individual = json.load(response)
            assert response.status == 202
            assert individual["target_url"] == result["url"]

        for _ in range(20):
            with urllib.request.urlopen(f"{base_url}/api/tailoring/status") as response:
                individual = json.load(response)
            if individual["status"] == "idle" and individual["recent"]:
                break
            time.sleep(0.01)
        assert individual["recent"][0]["target_url"] == result["url"]
        assert individual["recent"][0]["status"] == "complete"

        applied_request = urllib.request.Request(
            f"{base_url}/api/jobs/applied",
            data=json.dumps({"url": result["url"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(applied_request) as response:
            applied = json.load(response)
            assert response.status == 200
            assert applied["status"] == "applied"

        row = get_connection(db_path).execute(
            "SELECT applied_at FROM jobs WHERE url = ?",
            (result["url"],),
        ).fetchone()
        assert row["applied_at"] is not None

        unapplied_request = urllib.request.Request(
            f"{base_url}/api/jobs/applied",
            data=json.dumps({"url": result["url"], "applied": False}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(unapplied_request) as response:
            unapplied = json.load(response)
            assert response.status == 200
            assert unapplied["status"] == "active"

        row = get_connection(db_path).execute(
            "SELECT applied_at, apply_status FROM jobs WHERE url = ?",
            (result["url"],),
        ).fetchone()
        assert row["applied_at"] is None
        assert row["apply_status"] is None

        delete_request = urllib.request.Request(
            f"{base_url}/api/jobs/delete",
            data=json.dumps({"url": result["url"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(delete_request) as response:
            deleted = json.load(response)
            assert response.status == 200
            assert deleted["deleted"] is True

        assert (
            get_connection(db_path).execute(
                "SELECT COUNT(*) FROM jobs WHERE url = ?",
                (result["url"],),
            ).fetchone()[0]
            == 0
        )

        discovery_request = urllib.request.Request(
            f"{base_url}/api/discovery",
            data=json.dumps({"workers": 2}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(discovery_request) as response:
            discovery = json.load(response)
            assert response.status == 202
            assert discovery["status"] == "running"

        for _ in range(20):
            with urllib.request.urlopen(
                f"{base_url}/api/discovery/status"
            ) as response:
                discovery = json.load(response)
            if discovery["status"] == "complete":
                break
            time.sleep(0.01)
        assert discovery["result"] == {"new": 3, "existing": 7}

        tailoring_request = urllib.request.Request(
            f"{base_url}/api/tailoring",
            data=json.dumps(
                {"min_score": 7, "limit": 20, "validation_mode": "normal"}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(tailoring_request) as response:
            tailoring = json.load(response)
            assert response.status == 202
            assert tailoring["status"] == "queued"
            assert tailoring["kind"] == "batch"

        for _ in range(20):
            with urllib.request.urlopen(
                f"{base_url}/api/tailoring/status"
            ) as response:
                tailoring = json.load(response)
            if (
                tailoring["status"] == "idle"
                and tailoring["recent"]
                and tailoring["recent"][0]["kind"] == "batch"
            ):
                break
            time.sleep(0.01)
        assert tailoring["recent"][0]["result"] == {
            "approved": 2,
            "failed": 1,
            "errors": 0,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_tailoring_queue_runs_fifo_deduplicates_and_continues_after_error(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "tailoring-queue.db"
    conn = init_db(db_path)
    urls = [f"https://example.com/jobs/{index}" for index in range(3)]
    for url in urls:
        conn.execute(
            "INSERT INTO jobs (url, title, full_description, fit_score) "
            "VALUES (?, 'Engineer', 'Complete description', 8)",
            (url,),
        )
    conn.commit()
    monkeypatch.setattr(
        dashboard_server,
        "get_connection",
        lambda: get_connection(db_path),
    )

    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []
    active = 0
    max_active = 0
    calls_lock = threading.Lock()

    def execute(request):
        nonlocal active, max_active
        with calls_lock:
            active += 1
            max_active = max(max_active, active)
            calls.append(request["target_url"])
        if request["target_url"] == urls[0]:
            first_started.set()
            assert release_first.wait(timeout=2)
        with calls_lock:
            active -= 1
        if request["target_url"] == urls[1]:
            return "error", None, "simulated failure"
        return "complete", {"approved": 1, "failed": 0, "errors": 0}, None

    monkeypatch.setattr(dashboard_server, "_run_tailoring_request", execute)
    server = DashboardHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    try:
        first = start_tailoring(server, min_score=1, limit=1, target_url=urls[0])
        assert first["status"] == "queued"
        assert first_started.wait(timeout=2)

        second = start_tailoring(server, min_score=1, limit=1, target_url=urls[1])
        third = start_tailoring(server, min_score=1, limit=1, target_url=urls[2])
        duplicate = start_tailoring(server, min_score=1, limit=1, target_url=urls[1])

        assert second["queue_position"] == 1
        assert third["queue_position"] == 2
        assert duplicate["id"] == second["id"]
        assert duplicate["deduplicated"] is True
        state = tailoring_status(server)
        assert state["current"]["target_url"] == urls[0]
        assert [item["target_url"] for item in state["queued"]] == urls[1:]

        release_first.set()
        for _ in range(100):
            state = tailoring_status(server)
            if state["status"] == "idle":
                break
            time.sleep(0.01)

        assert state["status"] == "idle"
        assert calls == urls
        assert max_active == 1
        statuses = {item["target_url"]: item["status"] for item in state["recent"]}
        assert statuses == {
            urls[0]: "complete",
            urls[1]: "error",
            urls[2]: "complete",
        }
    finally:
        release_first.set()
        server.server_close()


def test_tailoring_queue_skips_job_that_becomes_ineligible(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "tailoring-stale.db"
    conn = init_db(db_path)
    urls = ["https://example.com/jobs/active", "https://example.com/jobs/stale"]
    for url in urls:
        conn.execute(
            "INSERT INTO jobs (url, title, full_description, fit_score) "
            "VALUES (?, 'Engineer', 'Complete description', 8)",
            (url,),
        )
    conn.commit()
    monkeypatch.setattr(
        dashboard_server,
        "get_connection",
        lambda: get_connection(db_path),
    )

    first_started = threading.Event()
    release_first = threading.Event()
    original_execute = dashboard_server._run_tailoring_request

    def execute(request):
        if request["target_url"] == urls[0]:
            first_started.set()
            assert release_first.wait(timeout=2)
            return "complete", {"approved": 1, "failed": 0, "errors": 0}, None
        return original_execute(request)

    monkeypatch.setattr(dashboard_server, "_run_tailoring_request", execute)
    server = DashboardHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    try:
        start_tailoring(server, min_score=1, limit=1, target_url=urls[0])
        assert first_started.wait(timeout=2)
        start_tailoring(server, min_score=1, limit=1, target_url=urls[1])
        conn.execute(
            "UPDATE jobs SET applied_at = '2026-08-03T12:00:00+00:00' WHERE url = ?",
            (urls[1],),
        )
        conn.commit()
        release_first.set()

        for _ in range(100):
            state = tailoring_status(server)
            if state["status"] == "idle":
                break
            time.sleep(0.01)

        stale = next(item for item in state["recent"] if item["target_url"] == urls[1])
        assert stale["status"] == "skipped"
        assert stale["result"]["reason"] == "This job is already marked as applied"
    finally:
        release_first.set()
        server.server_close()


def test_tailoring_queue_allows_only_one_outstanding_batch(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def execute(_request):
        started.set()
        assert release.wait(timeout=2)
        return "complete", {"approved": 0, "failed": 0, "errors": 0}, None

    monkeypatch.setattr(dashboard_server, "_run_tailoring_request", execute)
    server = DashboardHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    try:
        start_tailoring(server)
        assert started.wait(timeout=2)
        with pytest.raises(RuntimeError, match="bulk tailoring request"):
            start_tailoring(server)
    finally:
        release.set()
        server.server_close()


def test_cancel_tailoring_marks_running_job_and_worker_finishes_cancelled(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "tailoring-cancel.db"
    conn = init_db(db_path)
    url = "https://example.com/jobs/cancel"
    conn.execute(
        "INSERT INTO jobs (url, title, full_description, fit_score) "
        "VALUES (?, 'Engineer', 'Complete description', 8)",
        (url,),
    )
    conn.commit()
    monkeypatch.setattr(dashboard_server, "get_connection", lambda: get_connection(db_path))

    started = threading.Event()

    def execute(request):
        started.set()
        for _ in range(100):
            if request["cancel_requested"]:
                return "cancelled", None, None
            time.sleep(0.01)
        return "complete", {"approved": 1, "failed": 0, "errors": 0}, None

    monkeypatch.setattr(dashboard_server, "_run_tailoring_request", execute)
    server = DashboardHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    try:
        start_tailoring(server, min_score=1, limit=1, target_url=url)
        assert started.wait(timeout=2)

        result = cancel_tailoring(server, url)
        assert result["status"] == "cancelling"
        assert tailoring_status(server)["current"]["cancel_requested"] is True

        for _ in range(100):
            state = tailoring_status(server)
            if state["status"] == "idle":
                break
            time.sleep(0.01)
        assert state["recent"][0]["status"] == "cancelled"
    finally:
        server.server_close()


def test_cancel_tailoring_removes_queued_job(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "tailoring-cancel-queued.db"
    conn = init_db(db_path)
    urls = ["https://example.com/jobs/running", "https://example.com/jobs/queued"]
    for url in urls:
        conn.execute(
            "INSERT INTO jobs (url, title, full_description, fit_score) "
            "VALUES (?, 'Engineer', 'Complete description', 8)",
            (url,),
        )
    conn.commit()
    monkeypatch.setattr(dashboard_server, "get_connection", lambda: get_connection(db_path))
    started = threading.Event()
    release = threading.Event()

    def execute(_request):
        started.set()
        assert release.wait(timeout=2)
        return "complete", {"approved": 1, "failed": 0, "errors": 0}, None

    monkeypatch.setattr(dashboard_server, "_run_tailoring_request", execute)
    server = DashboardHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    try:
        start_tailoring(server, min_score=1, limit=1, target_url=urls[0])
        assert started.wait(timeout=2)
        start_tailoring(server, min_score=1, limit=1, target_url=urls[1])

        result = cancel_tailoring(server, urls[1])
        assert result["status"] == "cancelled"
        state = tailoring_status(server)
        assert state["queued"] == []
        assert state["recent"][0]["target_url"] == urls[1]
        assert state["recent"][0]["status"] == "cancelled"
    finally:
        release.set()
        server.server_close()


def test_tailoring_queue_can_replace_an_existing_resume(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "tailoring-replace.db"
    conn = init_db(db_path)
    url = "https://example.com/jobs/replace-resume"
    conn.execute(
        "INSERT INTO jobs (url, title, full_description, fit_score, "
        "tailored_resume_path, tailor_attempts) VALUES (?, 'Engineer', ?, 8, ?, 1)",
        (url, "Complete description", str(tmp_path / "old.tex")),
    )
    conn.commit()
    monkeypatch.setattr(
        dashboard_server,
        "get_connection",
        lambda: get_connection(db_path),
    )

    default_server = DashboardHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    try:
        with pytest.raises(ValueError, match="already has a tailored resume"):
            start_tailoring(default_server, target_url=url)
    finally:
        default_server.server_close()

    finished = threading.Event()

    def execute(request):
        assert request["replace_existing"] is True
        finished.set()
        return "complete", {"approved": 1, "failed": 0, "errors": 0}, None

    monkeypatch.setattr(dashboard_server, "_run_tailoring_request", execute)
    server = DashboardHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    try:
        request = start_tailoring(server, target_url=url, replace_existing=True)
        assert request["replace_existing"] is True
        assert finished.wait(timeout=2)
    finally:
        server.server_close()


def test_dashboard_settings_api(settings_files) -> None:
    server = DashboardHTTPServer(
        ("127.0.0.1", 0),
        DashboardRequestHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        with urllib.request.urlopen(f"{base_url}/api/settings") as response:
            settings = json.load(response)
        assert settings["password_configured"] is True
        assert "password" not in settings["profile"]["personal"]

        with urllib.request.urlopen(f"{base_url}/api/resume") as response:
            resume = json.load(response)
        assert resume["content"] == "Existing resume\nExperience\n"
        with urllib.request.urlopen(
            f"{base_url}/api/resume?format=tex"
        ) as response:
            latex_resume = json.load(response)
        assert latex_resume["format"] == "tex"

        latex_request = urllib.request.Request(
            f"{base_url}/api/resume",
            data=json.dumps(
                {
                    "filename": "replacement.tex",
                    "content": (
                        "% upload comment\n"
                        "\\documentclass{article}\n"
                        "\\begin{document}\nHello 10\\% % inline\n\\end{document}\n"
                    ),
                    "remove_comments": True,
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": base_url,
            },
            method="PUT",
        )
        with urllib.request.urlopen(latex_request) as response:
            latex_uploaded = json.load(response)
        assert latex_uploaded["pdf_available"] is True
        assert latex_uploaded["comments_removed"] == 2
        assert "% upload comment" not in latex_uploaded["content"]
        assert r"10\%" in latex_uploaded["content"]

        with urllib.request.urlopen(f"{base_url}/api/resume/pdf") as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "application/pdf"
            assert response.read().startswith(b"%PDF-")

        resume_request = urllib.request.Request(
            f"{base_url}/api/resume",
            data=json.dumps(
                {
                    "filename": "replacement.txt",
                    "content": "Replacement resume",
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": base_url,
            },
            method="PUT",
        )
        with urllib.request.urlopen(resume_request) as response:
            uploaded = json.load(response)
        assert uploaded["content"] == "Replacement resume"

        rebound = urllib.request.Request(
            f"{base_url}/api/settings",
            headers={"Host": "attacker.example"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(rebound)
        assert exc_info.value.code == 403

        settings["profile"]["personal"]["full_name"] = "Updated User"
        request = urllib.request.Request(
            f"{base_url}/api/settings/profile",
            data=json.dumps({"profile": settings["profile"]}).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": base_url,
            },
            method="PUT",
        )
        with urllib.request.urlopen(request) as response:
            updated = json.load(response)
        assert updated["profile"]["personal"]["full_name"] == "Updated User"

        invalid = urllib.request.Request(
            f"{base_url}/api/settings/searches",
            data=json.dumps(
                {"searches": {"queries": [{"query": "Test", "tier": 9}]}}
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": base_url,
            },
            method="PUT",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(invalid)
        assert exc_info.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_has_fit_and_applied_tabs(tmp_path) -> None:
    connection = init_db(tmp_path / "dashboard.db")
    import_external_job("https://example.com/jobs/active", connection)
    tailored_resume = tmp_path / "Active_Engineer_Tailored_Resume.tex"
    tailored_resume.write_text("resume", encoding="utf-8")
    tailored_resume.with_suffix(".pdf").write_bytes(b"%PDF")
    tailored_resume.with_name(
        f"{tailored_resume.stem}_REPORT.json"
    ).write_text("{}", encoding="utf-8")
    connection.execute(
        "UPDATE jobs SET full_description = ?, company = ?, company_logo = ?, "
        "tailored_resume_path = ?, fit_score = ? WHERE url = ?",
        (
            "Complete job description",
            "Example Corp",
            "https://cdn.example.com/logo.png",
            str(tailored_resume),
            8,
            "https://example.com/jobs/active",
        ),
    )
    strong = import_external_job("https://example.com/jobs/strong", connection)
    connection.execute(
        "UPDATE jobs SET fit_score = ? WHERE url = ?",
        (9, strong["url"]),
    )
    applied = import_external_job("https://example.com/jobs/done", connection)
    mark_job_applied(applied["url"], connection)
    connection.execute(
        "UPDATE jobs SET tailored_resume_path = ? WHERE url = ?",
        (
            str(tmp_path / "Example_Engineer_Tailored_Resume.tex"),
            applied["url"],
        ),
    )
    connection.commit()

    jobs = load_dashboard_jobs(connection)
    by_url = {job["url"]: job for job in jobs}
    assert len(jobs) == 3
    assert by_url["https://example.com/jobs/active"]["has_tailored"] is True
    assert by_url["https://example.com/jobs/active"]["has_pdf"] is True
    assert by_url["https://example.com/jobs/active"]["has_tex"] is True
    assert by_url["https://example.com/jobs/active"]["has_report"] is True
    assert by_url["https://example.com/jobs/active"]["can_retailor"] is True
    assert by_url[strong["url"]]["score"] == 9
    assert by_url[applied["url"]]["applied"] is True
    assert by_url[applied["url"]]["applied_tab"] == "needs_drafts"
    assert by_url[applied["url"]]["status"] == "Needs drafts"


def test_applied_tabs_use_draft_ids_and_pre_apollo_dates(tmp_path) -> None:
    connection = init_db(tmp_path / "dashboard.db")
    connection.executemany(
        "INSERT INTO jobs (url, title, company, applied_at) VALUES (?, ?, 'Example', ?)",
        [
            ("https://example.com/old", "Old application", "2026-09-11T03:26:03+00:00"),
            ("https://example.com/pending", "Pending drafts", "2026-09-11T03:26:05+00:00"),
            ("https://example.com/drafted", "Created drafts", "2026-09-11T03:26:05+00:00"),
            ("https://example.com/sent", "Sent outreach", "2026-09-11T03:26:05+00:00"),
        ],
    )
    connection.execute(
        "INSERT INTO outreach_batches (id, job_url, status, created_at, updated_at) "
        "VALUES ('batch-1', 'https://example.com/drafted', 'drafted', '', '')"
    )
    connection.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, gmail_draft_id, status, created_at, updated_at) "
        "VALUES ('recipient-1', 'batch-1', 'person-1', 'draft-1', 'drafted', '', '')"
    )
    connection.execute(
        "INSERT INTO outreach_batches (id, job_url, status, created_at, updated_at) "
        "VALUES ('batch-2', 'https://example.com/sent', 'completed', '', '')"
    )
    connection.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, apollo_message_id, status, created_at, updated_at) "
        "VALUES ('recipient-2', 'batch-2', 'person-2', 'message-1', 'sent', '', '')"
    )
    connection.commit()

    jobs = load_dashboard_jobs(connection)
    buckets = {job["title"]: job["applied_tab"] for job in jobs}
    assert buckets == {
        "Old application": "drafts_done",
        "Pending drafts": "needs_drafts",
        "Created drafts": "drafts_done",
        "Sent outreach": "drafts_done",
    }
    assert applied_view("2026-09-11T03:26:03+00:00") == "drafts_done"
    assert applied_view("2026-09-11T03:26:05+00:00") == "needs_drafts"
    assert applied_view("2026-09-11T03:26:05+00:00", True) == "drafts_done"


def test_dashboard_cards_show_dates_and_sort_newest_within_score(
    tmp_path,
) -> None:
    connection = init_db(tmp_path / "posted-dates.db")
    connection.executemany(
        "INSERT INTO jobs (url, title, company, discovered_at, posted_at, fit_score) "
        "VALUES (?, ?, 'Example Corp', ?, ?, ?)",
        [
            (
                "https://example.com/jobs/older",
                "Older same-score job",
                "2026-08-15T00:00:00+00:00",
                "2026-08-01",
                8,
            ),
            (
                "https://example.com/jobs/newer",
                "Newer same-score job",
                "2026-08-15T00:00:00+00:00",
                "2026-08-14",
                8,
            ),
            (
                "https://example.com/jobs/legacy",
                "Legacy job",
                "2026-08-13T00:00:00+00:00",
                None,
                7,
            ),
        ],
    )
    connection.commit()

    jobs = load_dashboard_jobs(connection)
    assert [job["title"] for job in jobs] == [
        "Newer same-score job",
        "Older same-score job",
        "Legacy job",
    ]
    assert jobs[0]["posted_label"] == "Posted Aug 14, 2026"
    assert jobs[2]["posted_label"] == "Posted Aug 13, 2026"


def test_dashboard_serves_packaged_spa_assets(tmp_path, monkeypatch) -> None:
    web_dist = tmp_path / "web_dist"
    assets = web_dist / "assets"
    assets.mkdir(parents=True)
    (web_dist / "index.html").write_text(
        '<div id="root"></div><script src="/assets/app-abc.js"></script>',
        encoding="utf-8",
    )
    (assets / "app-abc.js").write_text("console.log('app')", encoding="utf-8")
    monkeypatch.setattr(dashboard_server, "WEB_DIST_DIR", web_dist)

    server = DashboardHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base_url}/") as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-cache"
            assert response.headers["Content-Type"].startswith("text/html")
            assert b'id="root"' in response.read()
        with urllib.request.urlopen(f"{base_url}/profile") as response:
            assert response.headers["Cache-Control"] == "no-cache"
        with urllib.request.urlopen(f"{base_url}/assets/app-abc.js") as response:
            assert response.headers["Cache-Control"] == (
                "public, max-age=31536000, immutable"
            )
            assert "javascript" in response.headers["Content-Type"]
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/assets/../index.html")
        assert exc_info.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_jobs_api_returns_empty_and_standardized_errors(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "empty.db"
    init_db(db_path).close()
    monkeypatch.setattr(
        dashboard_server,
        "get_connection",
        lambda: get_connection(db_path),
    )

    server = DashboardHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base_url}/api/jobs") as response:
            assert json.load(response) == {"jobs": []}

        def fail_to_load_jobs(_connection):
            raise sqlite3.OperationalError("simulated database failure")

        monkeypatch.setattr(dashboard_server, "load_dashboard_jobs", fail_to_load_jobs)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/api/jobs")
        assert exc_info.value.code == 500
        assert json.load(exc_info.value) == {
            "error": "Could not load jobs: simulated database failure"
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
