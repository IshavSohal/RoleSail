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
