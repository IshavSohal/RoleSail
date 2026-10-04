import sqlite3
from pathlib import Path

from rolesail.config import migrate_legacy_app_dir


def test_migrate_legacy_app_dir_copies_data_and_renames_database(tmp_path: Path) -> None:
    legacy_dir = tmp_path / ".applypilot"
    app_dir = tmp_path / ".rolesail"
    legacy_dir.mkdir()
    (legacy_dir / "applypilot.db").write_bytes(b"legacy database")
    (legacy_dir / "profile.json").write_text('{"name": "Example"}', encoding="utf-8")

    assert migrate_legacy_app_dir(app_dir, legacy_dir) is True
    assert (app_dir / "rolesail.db").read_bytes() == b"legacy database"
    assert (app_dir / "profile.json").read_text(encoding="utf-8") == '{"name": "Example"}'
    assert (legacy_dir / "applypilot.db").exists()


def test_migrate_legacy_app_dir_does_not_overwrite_rolesail_data(tmp_path: Path) -> None:
    legacy_dir = tmp_path / ".applypilot"
    app_dir = tmp_path / ".rolesail"
    legacy_dir.mkdir()
    app_dir.mkdir()
    (legacy_dir / "profile.json").write_text("legacy", encoding="utf-8")
    (app_dir / "profile.json").write_text("current", encoding="utf-8")

    assert migrate_legacy_app_dir(app_dir, legacy_dir) is False
    assert (app_dir / "profile.json").read_text(encoding="utf-8") == "current"


def test_migrate_legacy_app_dir_replaces_initialized_empty_database(tmp_path: Path) -> None:
    legacy_dir = tmp_path / ".applypilot"
    app_dir = tmp_path / ".rolesail"
    legacy_dir.mkdir()
    app_dir.mkdir()

    with sqlite3.connect(legacy_dir / "applypilot.db") as connection:
        connection.execute("CREATE TABLE jobs (title TEXT)")
        connection.execute("INSERT INTO jobs VALUES ('Platform Engineer')")
    with sqlite3.connect(app_dir / "rolesail.db") as connection:
        connection.execute("CREATE TABLE jobs (title TEXT)")

    assert migrate_legacy_app_dir(app_dir, legacy_dir) is True
    with sqlite3.connect(app_dir / "rolesail.db") as connection:
        assert connection.execute("SELECT title FROM jobs").fetchone() == (
            "Platform Engineer",
        )
    assert (app_dir / "rolesail.db.empty-before-legacy-migration").exists()


def test_migrate_legacy_app_dir_rewrites_existing_artifact_paths(tmp_path: Path) -> None:
    legacy_dir = tmp_path / ".applypilot"
    app_dir = tmp_path / ".rolesail"
    old_artifact = legacy_dir / "tailored_resumes" / "example.tex"
    new_artifact = app_dir / "tailored_resumes" / "example.tex"
    old_artifact.parent.mkdir(parents=True)
    new_artifact.parent.mkdir(parents=True)
    old_artifact.write_text("legacy", encoding="utf-8")
    new_artifact.write_text("migrated", encoding="utf-8")

    database_path = app_dir / "rolesail.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE jobs (tailored_resume_path TEXT, cover_letter_path TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, NULL)",
            (str(old_artifact),),
        )

    assert migrate_legacy_app_dir(app_dir, legacy_dir) is True
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT tailored_resume_path FROM jobs"
        ).fetchone() == (str(new_artifact),)
