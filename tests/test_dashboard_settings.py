"""Dashboard settings and resume-management tests."""

from __future__ import annotations

import json

import pytest
import yaml

from rolesail import dashboard_server
from rolesail.apply.prompt import _build_salary_section
from rolesail.dashboard import settings as dashboard_settings
from rolesail.dashboard_server import (
    load_dashboard_resume,
    load_dashboard_settings,
    save_dashboard_profile,
    save_dashboard_resume,
    save_dashboard_searches,
)


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

    monkeypatch.setattr(dashboard_settings, "_compile_latex_resume", fail_compile)

    with pytest.raises(ValueError, match="compilation failed"):
        save_dashboard_resume(
            "broken.tex",
            "\\documentclass{article}\\begin{document}x\\end{document}",
        )

    assert tex_path.read_text(encoding="utf-8") == original_tex
    assert pdf_path.read_bytes() == b"%PDF-1.4\n% previous\n"

