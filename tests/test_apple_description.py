import sqlite3

from rolesail.discovery import greenhouse
from rolesail.enrichment.detail import (
    extract_from_apple_hydration,
    reset_incomplete_apple_descriptions,
)


def test_extract_from_apple_hydration_includes_all_description_sections():
    intel = {
        "final_url": "https://jobs.apple.com/en-us/details/200000000/example-role",
        "apple_hydration": {
            "loaderData": {
                "jobDetails": {
                    "jobsData": {
                        "postingTitle": "Example Engineer",
                        "jobSummary": "A sufficiently detailed summary for this example Apple role.",
                        "description": "Build the product and collaborate across several engineering teams.",
                        "responsibilities": "Own delivery, testing, and operational quality.",
                        "minimumQualifications": "Three years of relevant software engineering experience.",
                        "preferredQualifications": "Experience shipping reliable distributed systems.",
                        "postDateInGMT": "2026-08-01T00:00:00Z",
                    }
                }
            }
        },
    }

    result = extract_from_apple_hydration(intel)

    assert result is not None
    description = result["full_description"]
    for section in (
        "Summary",
        "Description",
        "Responsibilities",
        "Minimum Qualifications",
        "Preferred Qualifications",
    ):
        assert section in description
    assert "Own delivery, testing, and operational quality." in description
    assert result["application_url"] == intel["final_url"]


def test_extract_from_apple_hydration_uses_localized_posting_fields():
    intel = {
        "final_url": "https://jobs.apple.com/en-us/details/200000001/example-role",
        "apple_hydration": {
            "loaderData": {
                "jobDetails": {
                    "jobsData": {
                        "selectedLocale": "en_US",
                        "localizations": {
                            "en_US": {
                                "posting": {
                                    "postingTitle": "Localized Engineer",
                                    "jobSummary": "A complete localized summary for the engineering role.",
                                    "description": "Develop and maintain customer-facing software.",
                                    "minimumQualifications": "Professional software development experience.",
                                    "preferredQualifications": "Experience with accessible product design.",
                                }
                            }
                        },
                    }
                }
            }
        },
    }

    result = extract_from_apple_hydration(intel)

    assert result is not None
    assert result["title"] == "Localized Engineer"
    assert "Develop and maintain customer-facing software." in result["full_description"]
    assert "Professional software development experience." in result["full_description"]


def test_apple_search_summary_is_not_marked_as_full_description(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs ("
        "url TEXT PRIMARY KEY, title TEXT, company TEXT, salary TEXT, description TEXT, "
        "location TEXT, site TEXT, strategy TEXT, discovered_at TEXT, posted_at TEXT, "
        "full_description TEXT, application_url TEXT, detail_scraped_at TEXT)"
    )
    monkeypatch.setitem(
        greenhouse.BIGTECH_FETCHERS,
        "apple_test",
        lambda _company, _terms: [
            {
                "title": "Software Engineer",
                "location": "Toronto, Canada",
                "url": "https://jobs.apple.com/en-ca/details/200000002/example",
                "content": "This summary is intentionally longer than two hundred characters. " * 5,
                "content_is_full": False,
                "posted_at": "Today",
            }
        ],
    )
    monkeypatch.setattr(greenhouse, "get_connection", lambda: conn)

    result = greenhouse._process_bigtech_company(
        "apple",
        {"name": "Apple", "provider": "apple_test"},
        ["software engineer"],
        [],
        [],
        [],
        {},
        False,
    )

    row = conn.execute(
        "SELECT description, full_description, detail_scraped_at FROM jobs"
    ).fetchone()
    assert result["new"] == 1
    assert row[0]
    assert row[1] is None
    assert row[2] is None


def test_country_allowlist_cannot_be_disabled_by_bigtech_switch(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs ("
        "url TEXT PRIMARY KEY, title TEXT, company TEXT, salary TEXT, description TEXT, "
        "location TEXT, site TEXT, strategy TEXT, discovered_at TEXT, posted_at TEXT, "
        "full_description TEXT, application_url TEXT, detail_scraped_at TEXT)"
    )
    monkeypatch.setitem(
        greenhouse.BIGTECH_FETCHERS,
        "outside_country_test",
        lambda _company, _terms: [{
            "title": "Software Engineer",
            "location": "London, United Kingdom",
            "url": "https://example.com/jobs/london",
            "content": "Full job description " * 20,
        }],
    )
    monkeypatch.setattr(greenhouse, "get_connection", lambda: conn)

    result = greenhouse._process_bigtech_company(
        "outside",
        {"name": "Outside", "provider": "outside_country_test"},
        ["software engineer"],
        [],
        [],
        [],
        {
            "allowed_countries": ["Canada", "United States"],
            "accept_remote_anywhere": False,
            "accept_unknown_locations": False,
        },
        False,
    )

    assert result["kept"] == 0
    assert result["new"] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_reset_incomplete_apple_descriptions_requeues_summary_only_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs (site TEXT, strategy TEXT, full_description TEXT, "
        "detail_scraped_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO jobs VALUES (?, ?, ?, ?)",
        [
            ("Apple", "apple_careers", "Only the search summary", "2026-08-01"),
            (
                "Apple",
                "apple_careers",
                "Summary\nComplete\n\nMinimum Qualifications\nExperience",
                "2026-08-01",
            ),
        ],
    )

    assert reset_incomplete_apple_descriptions(conn) == 1
    rows = conn.execute(
        "SELECT full_description, detail_scraped_at FROM jobs ORDER BY rowid"
    ).fetchall()
    assert rows[0] == (None, None)
    assert rows[1][0] is not None
    assert rows[1][1] == "2026-08-01"
