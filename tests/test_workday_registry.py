from rolesail.database import init_db
from rolesail.discovery import workday


def test_capital_one_is_in_default_workday_registry() -> None:
    assert workday.load_employers()["capital_one"] == {
        "name": "Capital One",
        "tenant": "capitalone",
        "site_id": "Capital_One",
        "base_url": "https://capitalone.wd12.myworkdayjobs.com",
    }


def test_disney_is_in_default_workday_registry() -> None:
    assert workday.load_employers()["disney"] == {
        "name": "The Walt Disney Company",
        "tenant": "disney",
        "site_id": "disneycareer",
        "base_url": "https://disney.wd5.myworkdayjobs.com",
    }


def test_autodesk_is_in_default_workday_registry() -> None:
    assert workday.load_employers()["autodesk"] == {
        "name": "Autodesk",
        "tenant": "autodesk",
        "site_id": "Ext",
        "base_url": "https://autodesk.wd1.myworkdayjobs.com",
    }


def test_workday_discovery_repairs_existing_external_job(tmp_path) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = (
        "https://disney.wd5.myworkdayjobs.com/disneycareer/job/Glendale-CA-USA/"
        "Software-Engineer-I_10158076"
    )
    conn.execute(
        "INSERT INTO jobs (url, title, company, location, site, strategy) "
        "VALUES (?, ?, ?, ?, ?, 'external_upload')",
        (
            url, "Software Engineer I", "5014 Disney Entertainment &amp; Sports LLC",
            "Remote; USA - CA - 1200 Grand Central Ave",
            "disney.wd5.myworkdayjobs.com",
        ),
    )
    conn.commit()

    description = "A complete and current Disney job description. " * 10
    result = workday.store_results(
        conn,
        [{
            "apply_url": url,
            "title": "Software Engineer I",
            "location": "Glendale, CA, USA",
            "posted": "Today",
            "full_description": description,
            "employer_name": "The Walt Disney Company",
        }],
        {},
    )

    assert result == (0, 1)
    row = conn.execute(
        "SELECT company, location, site, strategy, full_description FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    assert tuple(row) == (
        "The Walt Disney Company",
        "Glendale, CA, USA",
        "The Walt Disney Company",
        "workday_api",
        description,
    )
