import json
from datetime import UTC, datetime

from applypilot.discovery import startup_jobs


def test_fetch_startup_jobs_paginates_normalizes_and_deduplicates(monkeypatch):
    calls = []

    def fake_request(url, **kwargs):
        calls.append((url, kwargs))
        second_page = "starting_after=42" in url
        payload = {
            "data": [{
                "id": 7,
                "title": "Software Engineer",
                "url": "https://startup.jobs/software-engineer-example-7",
                "published_at": "2026-09-01T12:00:00Z",
                "workplace_type": "remote",
                "location": {"city": "Toronto", "state": "Ontario", "country": "Canada"},
                "salary": "CAD 140,000 - 180,000 per year",
                "description_html": "<p>Build reliable systems.</p>",
                "company": {"name": "Example", "logo_url": "https://example.com/logo.png"},
            }],
            "has_more": not second_page,
            "next_cursor": None if second_page else 42,
        }
        return json.dumps(payload).encode()

    monkeypatch.setattr(startup_jobs, "_http_request", fake_request)

    jobs = startup_jobs.fetch_startup_jobs(
        "sj_test", ["software engineer"], max_pages_per_query=2,
    )

    assert len(calls) == 2
    assert "q=software+engineer" in calls[0][0]
    assert calls[0][1]["headers"]["Authorization"] == "Bearer sj_test"
    assert jobs == [{
        "title": "Software Engineer",
        "company": "Example",
        "company_logo": "https://example.com/logo.png",
        "location": "Remote, Toronto, Ontario, Canada",
        "url": "https://startup.jobs/software-engineer-example-7",
        "application_url": "https://startup.jobs/software-engineer-example-7",
        "content": "<p>Build reliable systems.</p>",
        "salary": "CAD 140,000 - 180,000 per year",
        "posted_at": "2026-09-01T12:00:00Z",
    }]


def test_run_startup_jobs_skips_without_api_key(monkeypatch):
    monkeypatch.delenv("STARTUP_JOBS_API_KEY", raising=False)
    monkeypatch.setattr(startup_jobs.config, "load_search_config", lambda: {
        "queries": [{"query": "software engineer"}],
        "startup_jobs": {"enabled": True},
    })

    result = startup_jobs.run_startup_jobs_discovery()

    assert result["skipped"] == "STARTUP_JOBS_API_KEY is not configured"
    assert result["errors"] == 0


def test_run_startup_jobs_filters_persists_and_prefers_direct_source(monkeypatch, tmp_path):
    from applypilot.database import init_db

    conn = init_db(tmp_path / "jobs.db")
    conn.execute(
        "INSERT INTO jobs (url, title, company, strategy) VALUES (?, ?, ?, ?)",
        ("https://jobs.ashbyhq.com/direct/1", "Backend Engineer", "Direct Co", "ashby_api"),
    )
    conn.commit()
    monkeypatch.setattr(startup_jobs, "get_connection", lambda: conn)
    monkeypatch.setattr(startup_jobs, "init_db", lambda: conn)
    monkeypatch.setattr(startup_jobs, "reconcile_unscored_jobs", lambda *_args: {})
    monkeypatch.setattr(startup_jobs.config, "load_search_config", lambda: {
        "include_titles": ["software engineer", "backend engineer"],
        "allowed_countries": ["Canada"],
        "accept_unknown_locations": False,
        "startup_jobs": {"enabled": True},
    })
    recent_posted_at = datetime.now(UTC).isoformat()
    monkeypatch.setattr(startup_jobs, "fetch_startup_jobs", lambda *_args, **_kwargs: [
        {
            "title": "Software Engineer", "company": "New Co", "company_logo": None,
            "location": "Remote, Toronto, Canada", "url": "https://startup.jobs/new-1",
            "application_url": "https://startup.jobs/new-1",
            "content": "A complete startup engineering description. " * 20,
                "salary": None, "posted_at": recent_posted_at,
        },
        {
            "title": "Backend Engineer", "company": "Direct Co", "company_logo": None,
            "location": "Toronto, Canada", "url": "https://startup.jobs/duplicate-2",
            "application_url": "https://startup.jobs/duplicate-2",
            "content": "Duplicate role. " * 20, "salary": None,
                "posted_at": recent_posted_at,
        },
        {
            "title": "Sales Director", "company": "Other Co", "company_logo": None,
            "location": "Toronto, Canada", "url": "https://startup.jobs/rejected-3",
            "application_url": "https://startup.jobs/rejected-3",
            "content": "Wrong title. " * 20, "salary": None,
                "posted_at": recent_posted_at,
        },
    ])

    result = startup_jobs.run_startup_jobs_discovery(api_key="sj_test")

    assert result["found"] == 3
    assert result["new"] == 1
    assert result["existing"] == 1
    assert result["title_rejected"] == 1
    row = conn.execute(
        "SELECT company, site, strategy, application_url, full_description FROM jobs "
        "WHERE url = 'https://startup.jobs/new-1'"
    ).fetchone()
    assert tuple(row[:4]) == (
        "New Co", "Startup Jobs", "startup_jobs_api", "https://startup.jobs/new-1",
    )
    assert row[4]
