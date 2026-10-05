"""Shared test fixtures."""

import json

import pytest
import yaml

from rolesail import dashboard_server
from rolesail.dashboard import settings as dashboard_settings


@pytest.fixture
def settings_files(tmp_path, monkeypatch):
    profile_path = tmp_path / "profile.json"
    search_path = tmp_path / "searches.yaml"
    resume_path = tmp_path / "resume.txt"
    resume_tex_path = tmp_path / "resume.tex"
    resume_pdf_path = tmp_path / "resume.pdf"
    profile_path.write_text(
        json.dumps(
            {
                "personal": {
                    "full_name": "Example User",
                    "email": "user@example.com",
                    "password": "stored-secret",
                },
                "experience": {"target_role": "Software Engineer"},
                "skills_boundary": {
                    "programming_languages": ["Python", "Custom Language"],
                    "frameworks": ["FastAPI"],
                    "tools": ["Custom Platform"],
                },
                "resume_facts": {
                    "preserved_companies": ["Example Corp"],
                    "preserved_projects": ["RoleSail"],
                    "preserved_school": "Example University",
                    "real_metrics": ["50% faster"],
                },
                "custom_section": {"keep": True},
            }
        ),
        encoding="utf-8",
    )
    search_path.write_text(
        yaml.safe_dump(
            {
                "defaults": {
                    "location": "Canada",
                    "distance": 25,
                    "hours_old": 72,
                    "results_per_site": 50,
                },
                "queries": [{"query": "Software Engineer", "tier": 1}],
                "locations": [{"location": "Canada", "remote": True}],
                "allowed_countries": ["Canada", "United States"],
                "location_accept": ["Toronto"],
                "location_reject_non_remote": ["India"],
                "priority_titles": ["Backend Engineer", "Custom Title"],
                "exclude_titles": ["Senior Director"],
                "custom_search_key": "keep",
                "custom_null": None,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    resume_path.write_text("Existing resume\nExperience\n", encoding="utf-8")
    resume_tex_path.write_text(
        "\\documentclass{article}\n\\begin{document}\nResume\n\\end{document}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server.config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(dashboard_server.config, "SEARCH_CONFIG_PATH", search_path)
    monkeypatch.setattr(dashboard_server.config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(
        dashboard_server.config,
        "RESUME_TEX_PATH",
        resume_tex_path,
    )
    monkeypatch.setattr(dashboard_server.config, "RESUME_PDF_PATH", resume_pdf_path)
    monkeypatch.setattr(
        dashboard_settings,
        "_compile_latex_resume",
        lambda content: b"%PDF-1.4\n% fake tectonic output\n",
    )
    return profile_path, search_path


