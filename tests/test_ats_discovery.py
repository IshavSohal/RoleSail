import json
import sqlite3

from rolesail.discovery import ats


def _jobs_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs ("
        "url TEXT PRIMARY KEY, title TEXT, company TEXT, salary TEXT, description TEXT, "
        "location TEXT, site TEXT, strategy TEXT, discovered_at TEXT, posted_at TEXT, "
        "full_description TEXT, application_url TEXT, detail_scraped_at TEXT)"
    )
    return conn


def test_fetch_ashby_jobs_normalizes_listed_postings(monkeypatch):
    payload = {
        "jobs": [
            {
                "id": "one",
                "title": "Software Engineer",
                "location": "Toronto",
                "secondaryLocations": [{"location": "Remote - Canada"}],
                "isListed": True,
                "jobUrl": "https://jobs.ashbyhq.com/example/one",
                "applyUrl": "https://jobs.ashbyhq.com/example/one/application",
                "descriptionPlain": "Build reliable services. " * 20,
                "publishedAt": "2026-08-01T12:00:00Z",
                "compensation": {"compensationTierSummary": "CAD 140K - 180K"},
            },
            {
                "id": "hidden",
                "title": "Hidden role",
                "isListed": False,
            },
        ]
    }
    calls = []

    def fake_request(url, **_kwargs):
        calls.append(url)
        return json.dumps(payload).encode()

    monkeypatch.setattr(ats, "_http_request", fake_request)

    jobs = ats.fetch_ashby_jobs({"board": "example"})

    assert "posting-api/job-board/example?includeCompensation=true" in calls[0]
    assert jobs == [{
        "title": "Software Engineer",
        "location": "Toronto; Remote - Canada",
        "url": "https://jobs.ashbyhq.com/example/one",
        "application_url": "https://jobs.ashbyhq.com/example/one/application",
        "content": "Build reliable services. " * 20,
        "salary": "CAD 140K - 180K",
        "posted_at": "2026-08-01T12:00:00Z",
    }]


def test_fetch_lever_jobs_paginates_and_supports_eu(monkeypatch):
    calls = []

    def fake_request(url, **_kwargs):
        calls.append(url)
        if "skip=0" in url:
            postings = [
                {
                    "id": "one",
                    "text": "Software Engineer",
                    "categories": {"allLocations": ["Paris", "Remote - EU"]},
                    "hostedUrl": "https://jobs.eu.lever.co/example/one",
                    "applyUrl": "https://jobs.eu.lever.co/example/one/apply",
                    "descriptionPlain": "Build the product. " * 20,
                    "lists": [
                        {
                            "text": "Requirements",
                            "content": "<li>Write reliable code</li><li>Review changes</li>",
                        },
                        {
                            "text": "Compensation",
                            "content": "<li>EUR 100,000 - 130,000</li>",
                        },
                    ],
                    "additionalPlain": "Benefits and equal opportunity information.",
                    "createdAt": 1_722_470_400_000,
                    "salaryRange": {
                        "currency": "EUR", "min": 100000, "max": 130000,
                        "interval": "per-year-salary",
                    },
                },
                {"id": "duplicate", "text": "Other role", "hostedUrl": "https://x/2"},
            ]
        else:
            postings = [{
                "id": "two", "text": "Backend Engineer",
                "categories": {"location": "Berlin"}, "hostedUrl": "https://x/3",
            }]
        return json.dumps(postings).encode()

    monkeypatch.setattr(ats, "_http_request", fake_request)

    jobs = ats.fetch_lever_jobs({"site": "example", "region": "eu", "page_size": 2})

    assert len(calls) == 2
    assert calls[0].startswith("https://api.eu.lever.co/v0/postings/example?")
    assert jobs[0]["location"] == "Paris; Remote - EU"
    assert jobs[0]["salary"] == "EUR 100,000 - 130,000 year"
    assert jobs[0]["posted_at"] == "2024-08-01T00:00:00+00:00"
    assert "Requirements" in jobs[0]["content"]
    assert "Write reliable code" in jobs[0]["content"]
    assert "Review changes" in jobs[0]["content"]
    assert "Compensation" in jobs[0]["content"]
    assert "EUR 100,000 - 130,000" in jobs[0]["content"]
    assert "Benefits and equal opportunity" in jobs[0]["content"]


def test_process_ats_company_filters_and_persists_full_description(monkeypatch):
    conn = _jobs_connection()
    monkeypatch.setattr(ats, "get_connection", lambda: conn)
    monkeypatch.setitem(
        ats.ATS_FETCHERS,
        "ashby",
        lambda _company: [
            {
                "title": "Software Engineer",
                "location": "Toronto, Canada",
                "url": "https://jobs.ashbyhq.com/example/one",
                "application_url": "https://jobs.ashbyhq.com/example/one/application",
                "content": "A complete engineering job description. " * 20,
                "salary": "CAD 140K - 180K",
                "posted_at": "Today",
            },
            {
                "title": "Sales Director",
                "location": "Toronto, Canada",
                "url": "https://jobs.ashbyhq.com/example/two",
                "content": "Not relevant. " * 20,
            },
        ],
    )
    search_cfg = {
        "include_titles": ["software engineer"],
        "allowed_countries": ["Canada"],
        "accept_unknown_locations": False,
    }

    result = ats._process_company(
        "example", {"name": "Example"}, "ashby", search_cfg, True,
    )

    assert result == {
        "company": "Example", "found": 2, "kept": 1, "title_rejected": 1,
        "location_rejected": 0, "new": 1, "existing": 0, "error": None,
    }
    row = conn.execute(
        "SELECT strategy, salary, application_url, full_description, detail_scraped_at "
        "FROM jobs"
    ).fetchone()
    assert row[0:3] == (
        "ashby_api", "CAD 140K - 180K",
        "https://jobs.ashbyhq.com/example/one/application",
    )
    assert row[3]
    assert row[4]


def test_process_ats_company_refreshes_existing_job(monkeypatch):
    conn = _jobs_connection()
    url = "https://jobs.ashbyhq.com/example/one"
    conn.execute(
        "INSERT INTO jobs (url, title, company) VALUES (?, ?, ?)",
        (url, "Software Engineer", "Example"),
    )
    monkeypatch.setattr(ats, "get_connection", lambda: conn)
    monkeypatch.setitem(
        ats.ATS_FETCHERS,
        "ashby",
        lambda _company: [{
            "title": "Software Engineer",
            "location": "Toronto, Canada",
            "url": url,
            "application_url": f"{url}/apply",
            "content": "A newly complete description. " * 20,
            "posted_at": "Today",
        }],
    )

    result = ats._process_company(
        "example",
        {"name": "Example"},
        "ashby",
        {"include_titles": ["software engineer"]},
        False,
    )

    assert result["existing"] == 1
    row = conn.execute(
        "SELECT posted_at, full_description, application_url FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    assert row[0] == "Today"
    assert row[1] == ("A newly complete description. " * 20).strip()
    assert row[2] == f"{url}/apply"


def test_process_ats_company_ignores_stale_posting(monkeypatch):
    conn = _jobs_connection()
    monkeypatch.setattr(ats, "get_connection", lambda: conn)
    monkeypatch.setitem(
        ats.ATS_FETCHERS,
        "ashby",
        lambda _company: [{
            "title": "Software Engineer",
            "location": "Toronto, Canada",
            "url": "https://jobs.ashbyhq.com/example/stale",
            "content": "An old engineering job description. " * 20,
            "posted_at": "2020-01-01",
        }],
    )

    result = ats._process_company(
        "example",
        {"name": "Example"},
        "ashby",
        {"include_titles": ["software engineer"]},
        False,
    )

    assert result["kept"] == 0
    assert result["new"] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_default_ats_registries_have_required_slugs():
    assert all(company.get("board") for company in ats.load_ashby_companies().values())
    lever_companies = ats.load_lever_companies()
    assert all(company.get("site") for company in lever_companies.values())
    assert lever_companies["veeva"] == {"name": "Veeva Systems", "site": "veeva"}
