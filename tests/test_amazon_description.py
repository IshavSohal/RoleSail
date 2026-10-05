import json
import sqlite3

from rolesail.discovery import bigtech, greenhouse
from rolesail.enrichment.detail import reset_incomplete_amazon_descriptions


def _amazon_payload() -> dict:
    return {
        "jobs": [
            {
                "id_icims": "1234567",
                "title": "Software Development Engineer",
                "normalized_location": "Seattle, Washington, USA",
                "job_path": "/en/jobs/1234567/software-development-engineer",
                "description": (
                    "Build the future of software development.<br/><br/>"
                    "Key job responsibilities<br/>Ship reliable systems.<br/><br/>"
                    "About the team<br/>We learn from one another."
                ),
                "basic_qualifications": (
                    "- 5+ years of software development experience<br/>"
                    "- Experience leading system design"
                ),
                "preferred_qualifications": (
                    "- Bachelor's degree in computer science<br/><br/>"
                    "Amazon is an equal opportunity employer.<br/><br/>"
                    "The base salary range for this position is listed below.<br/><br/>"
                    "USA, WA, Seattle - 168,100.00 - 227,400.00 USD annually"
                ),
                "url_next_step": "https://account.amazon.com/jobs/1234567/apply",
                "posted_date": "August 1, 2026",
            }
        ]
    }


def test_amazon_fetch_assembles_qualifications_and_salary(monkeypatch):
    monkeypatch.setattr(
        bigtech,
        "_http_request",
        lambda *_args, **_kwargs: json.dumps(_amazon_payload()).encode(),
    )

    jobs = bigtech._fetch_amazon_jobs(
        {"page_size": 100, "max_pages": 1}, ["software engineer"]
    )

    assert len(jobs) == 1
    job = jobs[0]
    description = greenhouse._normalize_description(job["content"])
    assert "Basic Qualifications" in description
    assert "5+ years of software development experience" in description
    assert "Preferred Qualifications" in description
    assert "Bachelor's degree in computer science" in description
    assert "168,100.00 - 227,400.00 USD annually" in description
    assert job["salary"] == "USA, WA, Seattle - 168,100.00 - 227,400.00 USD annually"
    assert job["content_is_full"] is True
    assert job["application_url"] == (
        "https://www.amazon.jobs/applicant/jobs/1234567/apply"
    )


def test_fetch_amazon_job_requires_an_exact_job_id(monkeypatch):
    payload = _amazon_payload()
    payload["jobs"].insert(
        0,
        {
            **payload["jobs"][0],
            "id_icims": "7654321",
            "job_path": "/en/jobs/7654321/different-job",
        },
    )
    requested_urls = []

    def fake_request(url, **_kwargs):
        requested_urls.append(url)
        return json.dumps(payload).encode()

    monkeypatch.setattr(bigtech, "_http_request", fake_request)

    job = bigtech.fetch_amazon_job("1234567")

    assert job is not None
    assert job["id"] == "1234567"
    assert job["company"] == "Amazon"
    assert job["content_is_full"] is True
    assert "base_query=1234567" in requested_urls[0]


def test_existing_amazon_row_is_refreshed_with_complete_api_description(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs ("
        "url TEXT PRIMARY KEY, title TEXT, company TEXT, salary TEXT, description TEXT, "
        "location TEXT, site TEXT, strategy TEXT, discovered_at TEXT, posted_at TEXT, "
        "full_description TEXT, application_url TEXT, detail_scraped_at TEXT)"
    )
    url = "https://www.amazon.jobs/en/jobs/1234567/software-development-engineer"
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            url,
            "Software Development Engineer",
            "Amazon",
            None,
            "Old summary",
            "Seattle, Washington, USA",
            "Amazon",
            "amazon_careers",
            "2026-07-01",
            None,
            "About this role only",
            url,
            "2026-07-01",
        ),
    )
    monkeypatch.setitem(
        greenhouse.BIGTECH_FETCHERS,
        "amazon",
        lambda _company, _terms: [
            {
                "title": "Software Development Engineer",
                "location": "Seattle, Washington, USA",
                "url": url,
                "content": "<br/><br/>".join(
                    [
                        _amazon_payload()["jobs"][0]["description"],
                        "Basic Qualifications<br/><br/>"
                        + _amazon_payload()["jobs"][0]["basic_qualifications"],
                        "Preferred Qualifications<br/><br/>"
                        + _amazon_payload()["jobs"][0]["preferred_qualifications"],
                    ]
                ),
                "content_is_full": True,
                "salary": "USA, WA, Seattle - 168,100.00 - 227,400.00 USD annually",
                "application_url": "https://account.amazon.jobs/jobs/1234567/apply",
            }
        ],
    )
    monkeypatch.setattr(greenhouse, "get_connection", lambda: conn)

    result = greenhouse._process_bigtech_company(
        "amazon",
        {"name": "Amazon", "provider": "amazon"},
        ["software engineer"],
        [],
        [],
        [],
        {},
        False,
    )

    row = conn.execute(
        "SELECT salary, full_description, application_url FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    assert result["existing"] == 1
    assert "Basic Qualifications" in row[1]
    assert "Preferred Qualifications" in row[1]
    assert "168,100.00 - 227,400.00 USD annually" in row[0]
    assert row[2] == "https://account.amazon.jobs/jobs/1234567/apply"


def test_reset_incomplete_amazon_descriptions_requeues_old_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs (site TEXT, strategy TEXT, full_description TEXT, "
        "detail_scraped_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO jobs VALUES (?, ?, ?, ?)",
        [
            ("Amazon", "amazon_careers", "About this role only", "2026-08-01"),
            (
                "Amazon",
                "amazon_careers",
                "Basic Qualifications\nExperience\n\nPreferred Qualifications\nDegree",
                "2026-08-01",
            ),
        ],
    )

    assert reset_incomplete_amazon_descriptions(conn) == 1
    rows = conn.execute(
        "SELECT full_description, detail_scraped_at FROM jobs ORDER BY rowid"
    ).fetchall()
    assert rows[0] == (None, None)
    assert rows[1][0] is not None
    assert rows[1][1] == "2026-08-01"
