import sqlite3

from applypilot.enrichment.detail import (
    extract_from_meta_details,
    reset_incomplete_meta_descriptions,
)


def _meta_intelligence(rendered_sections=None):
    return {
        "final_url": "https://www.metacareers.com/profile/job_details/123/",
        "page_icon": "https://static.example/meta.ico",
        "page_title": "Meta Careers",
        "meta_sections": rendered_sections,
        "json_ld": [
            {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Software Engineer",
                "description": "Build reliable products used by people around the world.",
                "responsibilities": "Design systems&nbsp;Review code",
                "qualifications": "Programming experience&nbsp;Distributed systems",
                "hiringOrganization": {"name": "Meta"},
            }
        ],
    }


def test_extract_from_meta_details_includes_every_rendered_section():
    intel = _meta_intelligence(
        [
            "Software Engineer Responsibilities\nDesign systems\nReview code",
            "Minimum Qualifications\nProgramming experience",
            "Preferred Qualifications\nDistributed systems experience",
            "About Meta\nMeta builds technology that helps people connect.",
            "United States: $100,000/year to $150,000/year + benefits",
            "Equal Employment Opportunity\nMeta is an equal opportunity employer.",
        ]
    )

    result = extract_from_meta_details(intel)

    assert result is not None
    description = result["full_description"]
    for expected in (
        "Build reliable products used by people around the world.",
        "Software Engineer Responsibilities",
        "Minimum Qualifications",
        "Preferred Qualifications",
        "About Meta",
        "$100,000/year to $150,000/year",
        "Equal Employment Opportunity",
    ):
        assert expected in description
    assert result["application_url"] == intel["final_url"]
    assert result["company"] == "Meta"


def test_extract_from_meta_details_falls_back_to_all_json_ld_fields():
    result = extract_from_meta_details(_meta_intelligence())

    assert result is not None
    assert "Responsibilities\nDesign systems\nReview code" in result["full_description"]
    assert "Qualifications\nProgramming experience\nDistributed systems" in result["full_description"]


def test_reset_incomplete_meta_descriptions_requeues_summary_only_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs (site TEXT, strategy TEXT, full_description TEXT, "
        "detail_scraped_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO jobs VALUES (?, ?, ?, ?)",
        [
            ("Meta", "meta_careers", "Only the introductory summary", "2026-09-01"),
            (
                "Meta",
                "meta_careers",
                (
                    "Responsibilities\nBuild\n\nMinimum Qualifications\nCode\n\n"
                    "Preferred Qualifications\nSystems"
                ),
                "2026-09-01",
            ),
            ("IBM", "ibm_careers", "An unrelated description", "2026-09-01"),
        ],
    )

    assert reset_incomplete_meta_descriptions(conn) == 1
    rows = conn.execute(
        "SELECT full_description, detail_scraped_at FROM jobs ORDER BY rowid"
    ).fetchall()
    assert rows[0] == (None, None)
    assert rows[1][0] is not None
    assert rows[1][1] == "2026-09-01"
    assert rows[2][0] == "An unrelated description"
