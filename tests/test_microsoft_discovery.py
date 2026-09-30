import json
import sqlite3
import urllib.parse

from rolesail.discovery import greenhouse
from rolesail.enrichment.detail import (
    extract_from_microsoft_details,
    reset_incomplete_microsoft_descriptions,
)


def test_fetch_microsoft_jobs_normalizes_and_paginates(monkeypatch):
    calls = []

    def fake_request(url, **kwargs):
        calls.append(url)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path.endswith("/position_details"):
            job_id = query["position_id"][0]
            assert kwargs["headers"]["Referer"].endswith(f"/careers/job/{job_id}")
            return json.dumps(
                {
                    "data": {
                        "jobDescription": (
                            "<b>Overview</b><br><p>Build reliable data systems.</p>"
                            "<br><b>Responsibilities</b><br><ul>"
                            "<li>Ship production code.</li></ul>"
                            "<br><b>Qualifications</b><br><ul>"
                            "<li>Experience with Python.</li></ul>"
                        )
                    }
                }
            ).encode()

        start = int(query["start"][0])
        positions = []
        if start == 0:
            positions = [
                {
                    "id": 123,
                    "name": "Software Engineer",
                    "locations": ["Canada, Ontario, Toronto"],
                    "postedTs": 1_722_470_400,
                    "positionUrl": "/careers/job/123",
                }
            ] * 10
        elif start == 10:
            positions = [
                {
                    "id": 456,
                    "name": "Senior Software Engineer",
                    "standardizedLocations": ["Vancouver, BC, CA"],
                    "positionUrl": "/careers/job/456",
                }
            ]
        return json.dumps({"data": {"count": 11, "positions": positions}}).encode()

    monkeypatch.setattr(greenhouse, "_http_request", fake_request)

    jobs = greenhouse._fetch_microsoft_jobs({"max_pages": 5}, ["software engineer"])

    assert len(calls) == 2
    assert len(jobs) == 2

    greenhouse._fetch_microsoft_job_details(jobs[0])
    assert len(calls) == 3
    assert jobs[0] == {
        "title": "Software Engineer",
        "location": "Canada, Ontario, Toronto",
        "url": "https://apply.careers.microsoft.com/careers/job/123",
        "content": (
            "<b>Overview</b><br><p>Build reliable data systems.</p>"
            "<br><b>Responsibilities</b><br><ul>"
            "<li>Ship production code.</li></ul>"
            "<br><b>Qualifications</b><br><ul>"
            "<li>Experience with Python.</li></ul>"
        ),
        "content_is_full": True,
        "posted_at": "2024-08-01",
        "application_url": "https://apply.careers.microsoft.com/careers/job/123",
    }
    assert jobs[1]["location"] == "Vancouver, BC, CA"

    normalized = greenhouse._normalize_description(jobs[0]["content"])
    assert normalized == (
        "Overview\n\nBuild reliable data systems.\n\n"
        "Responsibilities\n\n- Ship production code.\n\n"
        "Qualifications\n\n- Experience with Python."
    )


def test_extract_microsoft_details_keeps_overview_headings_and_bullets():
    result = extract_from_microsoft_details(
        {
            "final_url": "https://apply.careers.microsoft.com/careers/job/123",
            "microsoft_details": {
                "name": "Software Development Engineer",
                "location": "Canada, Ontario, Toronto",
                "postedTs": 1_722_470_400,
                "jobDescription": (
                    "<b>Overview</b><br><p>Build the data platform for AI.</p>"
                    "<br><br><b>Responsibilities</b><br><ul>"
                    "<li>Ship reliable database features.</li>"
                    "<li>Contribute to PostgreSQL.</li></ul>"
                    "<br><br><b>Qualifications</b><br>"
                    "<p><strong>Required Qualifications:</strong></p><ul>"
                    "<li>Experience coding in Python.</li></ul>"
                ),
            },
        }
    )

    assert result is not None
    description = result["full_description"]
    assert description.startswith("Overview\nBuild the data platform for AI.")
    assert "Responsibilities\n- Ship reliable database features." in description
    assert "- Contribute to PostgreSQL." in description
    assert "Qualifications\nRequired Qualifications:\n- Experience coding in Python." in description
    assert result["posted_at"] == "2024-08-01"


def test_reset_incomplete_microsoft_description_requeues_only_partial_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs (site TEXT, strategy TEXT, full_description TEXT, "
        "detail_scraped_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO jobs VALUES (?, ?, ?, ?)",
        [
            (
                "Microsoft",
                "microsoft_careers",
                "Responsibilities\n- Ship code\nQualifications\n- Python",
                "2026-08-01",
            ),
            (
                "Microsoft",
                "microsoft_careers",
                "Overview\nAbout the team\nResponsibilities\n- Ship code",
                "2026-08-01",
            ),
        ],
    )

    assert reset_incomplete_microsoft_descriptions(conn) == 1
    rows = conn.execute(
        "SELECT full_description, detail_scraped_at FROM jobs ORDER BY rowid"
    ).fetchall()
    assert rows[0] == (None, None)
    assert rows[1][0].startswith("Overview")
    assert rows[1][1] == "2026-08-01"


def test_bigtech_config_includes_supported_microsoft_provider():
    microsoft = greenhouse.load_bigtech_companies()["microsoft"]

    assert microsoft["name"] == "Microsoft"
    assert microsoft["provider"] in greenhouse.BIGTECH_FETCHERS
