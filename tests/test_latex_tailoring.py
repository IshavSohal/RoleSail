from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rolesail import config
from rolesail.dashboard_server import load_tailored_artifact
from rolesail.database import init_db
from rolesail.scoring import latex, tailor
from rolesail.scoring.tailor import (
    _build_tailor_prompt,
    build_source_bullet_catalog,
    judge_tailored_resume,
    resolve_source_bullets,
    sort_experience_newest_first,
    tailor_resume,
)
from rolesail.scoring.validator import validate_json_fields


def _profile() -> dict:
    return {
        "personal": {
            "full_name": "Ada_Lovelace",
            "email": "ada@example.com",
            "phone": "123-456-7890",
        },
        "skills_boundary": {"programming_languages": ["Python", "SQL"]},
        "resume_facts": {"preserved_school": "Example University"},
    }


def _resume_data() -> dict:
    return {
        "keywords": {
            "must_have_technical": ["Python"],
            "preferred_technical": [],
            "responsibility_action": ["build"],
            "domain_product": [],
        },
        "education": [
            {
                "institution": "Example University",
                "location": "Toronto, ON",
                "degree": "BSc in Computer Science",
                "dates": "2020 -- 2024",
            }
        ],
        "experience": [
            {
                "entity_id": "experience_1",
                "role": "Software Engineer",
                "company": "Acme & Co",
                "location": "Toronto, ON",
                "dates": "2024 -- Present",
                "rationale": "Directly demonstrates production Python work.",
                "bullets": [
                    "Built a Python API reducing latency by 10%",
                    "Created SQL reports for operations",
                    "Tested backend services",
                ],
            }
        ],
        "projects": [
            {
                "entity_id": f"project_{index}",
                "name": f"Project {index}",
                "technologies": ["Python", "SQL"],
                "dates": "2023",
                "rationale": "Demonstrates relevant engineering work.",
                "bullets": ["Built a Python service", "Queried data with SQL", "Added tests"],
            }
            for index in range(1, 5)
        ],
        "skills": {"Languages": ["Python", "SQL"]},
        "courses": ["Algorithms"],
        "awards": ["Engineering Award"],
        "keyword_usage": {"incorporated": ["Python"], "skipped": []},
    }


def _master_source() -> str:
    return """Example University Acme & Co Software Engineer 2024 -- Present
Project 1 Project 2 Project 3 Project 4
Python SQL 10% 2020 2024 2023
Built a Python API reducing latency by 10%
"""


def test_supported_template_preserves_style_and_escapes_content() -> None:
    source = latex.render_resume_tex(_resume_data(), _profile())
    assert r"\documentclass[letterpaper,11pt]{article}" in source
    assert r"\addtolength{\textwidth}{1in}" in source
    assert r"\section{Experience}" in source
    assert r"Acme \& Co" in source
    assert r"10\%" in source
    assert r"Ada\_Lovelace" in source
    assert "@@" not in source


def test_header_is_preserved_from_master_resume() -> None:
    master = r"""
    \begin{document}
    \begin{center}
        \textbf{\Huge \scshape Ada Lovelace} \\ \vspace{1pt}
        \href{https://ada.example}{\underline{ada.example}} $|$
        \small 123-456-7890 $|$ \href{mailto:ada@example.com}{\underline{ada@example.com}} $|$
        \href{https://linkedin.com/in/ada}{LinkedIn: \underline{Ada Lovelace}} $|$
        GitHub: \href{https://github.com/ada}{\underline{ada}}
    \end{center}
    \end{document}
    """

    source = latex.render_resume_tex(_resume_data(), _profile(), master)

    expected = master.split(r"\begin{document}", 1)[1].split(r"\end{document}", 1)[0].strip()
    assert expected in source
    assert "https://linkedin.com/in/ada}" in source
    assert "LinkedIn: \\underline{Ada Lovelace}" in source


def test_generated_header_uses_friendly_unique_link_labels() -> None:
    profile = _profile()
    profile["personal"].update({
        "phone": "1234567890",
        "linkedin_url": "https://linkedin.com/in/ada",
        "github_url": "https://github.com/ada",
        "portfolio_url": "https://ada.example/",
        "website_url": "https://ada.example/",
    })

    source = latex.render_resume_tex(_resume_data(), profile)

    assert "123-456-7890" in source
    assert r"LinkedIn: \underline{Ada\_Lovelace}" in source
    assert r"GitHub: \href{https://github.com/ada}{\underline{ada}}" in source
    assert source.count(r"\href{https://ada.example/}") == 1
    assert "linkedin.com/in/ada}" not in source.split("LinkedIn:", 1)[1]


def test_validator_enforces_exactly_five_entities_and_source_only_skills() -> None:
    data = _resume_data()
    result = validate_json_fields(data, _profile(), original_text=_master_source())
    assert result["passed"], result

    data["projects"].pop()
    data["skills"]["Languages"].append("Rust")
    result = validate_json_fields(data, _profile(), original_text=_master_source())
    assert not result["passed"]
    assert any("exactly 5" in error for error in result["errors"])
    assert any("skill" in error.lower() for error in result["errors"])


def test_validator_rejects_an_invented_metric() -> None:
    data = _resume_data()
    data["experience"][0]["bullets"][0] = "Built a Python API reducing latency by 99%"
    result = validate_json_fields(data, _profile(), original_text=_master_source())
    assert any("99%" in error for error in result["errors"])


@pytest.mark.parametrize("mode", ["strict", "normal", "lenient"])
def test_validator_hard_rejects_duplicate_project_names(mode: str) -> None:
    data = _resume_data()
    data["projects"][1]["name"] = data["projects"][0]["name"]

    result = validate_json_fields(
        data,
        _profile(),
        mode=mode,
        original_text=_master_source(),
    )

    assert not result["passed"]
    assert "Duplicate project name: 'Project 1' is selected more than once" in result["errors"]


def test_validator_normalizes_project_names_before_duplicate_check() -> None:
    data = _resume_data()
    data["projects"][0]["name"] = "Bitly"
    data["projects"][1]["name"] = "  BITLY!!!  "

    result = validate_json_fields(data, _profile())

    assert not result["passed"]
    assert any("Duplicate project name: 'Bitly'" in error for error in result["errors"])


def test_validator_accepts_percent_metric_escaped_in_latex_source() -> None:
    data = _resume_data()
    latex_source = _master_source().replace("10%", r"10\%")

    result = validate_json_fields(data, _profile(), original_text=latex_source)

    assert result["passed"], result


def test_judge_reports_an_empty_model_response(monkeypatch) -> None:
    class EmptyClient:
        def chat(self, messages, max_tokens, temperature):
            assert max_tokens == 2048
            return ""

    monkeypatch.setattr(tailor, "get_client", lambda: EmptyClient())

    result = judge_tailored_resume("source", "tailored", "Engineer", _profile())

    assert not result["passed"]
    assert "empty response" in result["issues"]


def test_prompts_require_newest_experience_first() -> None:
    tailor_prompt = _build_tailor_prompt(_profile())
    prompt = tailor._build_judge_prompt(_profile())

    assert "ALWAYS order professional experience in reverse chronological order" in tailor_prompt
    assert "most recent professional experience must come first" in tailor_prompt
    assert "Never reorder experience by relevance" in tailor_prompt
    assert "FAIL if violated" in prompt
    assert "most recent experience first" in prompt


def test_experience_is_sorted_newest_first_across_common_date_formats() -> None:
    data = {
        "experience": [
            {"role": "Oldest", "dates": "2019 -- 2021"},
            {"role": "Recent completed", "dates": "Sept. 2023 - April 2026"},
            {"role": "Current older start", "dates": "May 2024 - Present"},
            {"role": "Current newer start", "dates": "Summer 2025 to Current"},
            {"role": "Middle", "dates": "2022"},
        ]
    }

    sort_experience_newest_first(data)

    assert [entry["role"] for entry in data["experience"]] == [
        "Current newer start",
        "Current older start",
        "Recent completed",
        "Middle",
        "Oldest",
    ]


def test_tailoring_records_and_logs_attempt_failure(monkeypatch, caplog) -> None:
    class InvalidJsonClient:
        def chat(self, messages, max_tokens, temperature):
            return "not JSON"

    monkeypatch.setattr(tailor, "get_client", lambda: InvalidJsonClient())
    job = {
        "title": "Cloud Engineer",
        "company": "Acme & Co",
        "site": "example",
        "location": "Toronto",
        "full_description": "Build cloud services",
    }

    with caplog.at_level(logging.WARNING):
        _, report = tailor_resume(
            "- Built a Python cloud service",
            job,
            _profile(),
            max_retries=0,
        )

    assert report["attempt_history"] == [{
        "attempt": 1,
        "stage": "json",
        "errors": ["Output was not valid JSON. Return ONLY a JSON object, nothing else."],
        "warnings": [],
    }]
    assert "failed JSON parsing for Cloud Engineer" in caplog.text


def test_source_bullet_catalog_extracts_latex_and_resolves_ids_verbatim() -> None:
    source = r"""
    \newcommand{\resumeItem}[1]{ignored #1}
    \begin{document}
    \resumeItem{Built an API with \textbf{Python} and cut latency by 10\%}
    \resumeItem{Used PostgreSQL \& Redis for storage}
    \end{document}
    """
    catalog = build_source_bullet_catalog(source)
    assert catalog == {
        "bullet_001": {
            "text": "Built an API with Python and cut latency by 10%",
            "bold_spans": [[18, 24]],
        },
        "bullet_002": {
            "text": "Used PostgreSQL & Redis for storage",
            "bold_spans": [],
        },
    }

    data = {
        "experience": [{"entity_id": "experience_1", "bullet_ids": ["bullet_002"]}],
        "projects": [],
    }
    assert resolve_source_bullets(data, catalog) == []
    assert data["experience"][0]["bullets"] == [{
        "text": "Used PostgreSQL & Redis for storage",
        "bold_spans": [],
    }]
    assert "bullet_ids" not in data["experience"][0]


def test_selected_source_bullet_preserves_bold_text_in_rendered_latex() -> None:
    source = r"""
    \begin{document}
    \resumeItem{Built an API with \textbf{Python \& SQL} and cut latency by \textbf{10\%}}
    \end{document}
    """
    catalog = build_source_bullet_catalog(source)
    data = _resume_data()
    data["projects"] = []
    data["experience"][0]["bullet_ids"] = ["bullet_001"]
    data["experience"][0].pop("bullets")

    assert resolve_source_bullets(data, catalog) == []
    rendered = latex.render_resume_tex(data, _profile())

    assert r"\resumeItem{Built an API with \textbf{Python \& SQL} and cut latency by \textbf{10\%}}" in rendered


def test_source_bullet_selection_rejects_free_form_and_duplicate_ids() -> None:
    catalog = {"bullet_001": "Original source bullet"}
    free_form = {
        "experience": [{"entity_id": "experience_1", "bullets": ["Rephrased bullet"]}],
        "projects": [],
    }
    assert "unknown source bullet IDs" in resolve_source_bullets(free_form, catalog)[0]

    duplicate = {
        "experience": [{"entity_id": "experience_1", "bullet_ids": ["bullet_001"]}],
        "projects": [{"entity_id": "project_1", "bullet_ids": ["bullet_001"]}],
    }
    errors = resolve_source_bullets(duplicate, catalog)
    assert any("more than once" in error for error in errors)


def test_tailor_prompt_is_selection_only_and_ignores_bullet_length() -> None:
    prompt = _build_tailor_prompt(_profile())
    assert "Never write, rewrite, shorten" in prompt
    assert "Bullet length, wording, punctuation, and line wrapping are not your concern" in prompt
    assert '"bullet_ids"' in prompt


def test_fit_one_page_removes_courses_then_awards(monkeypatch) -> None:
    def fake_compile(source: str) -> bytes:
        return source.encode()

    def fake_pages(pdf: bytes) -> int:
        source = pdf.decode()
        return 2 if "Relevant Courses" in source or "Awards" in source else 1

    monkeypatch.setattr(latex, "compile_latex", fake_compile)
    monkeypatch.setattr(latex, "pdf_page_count", fake_pages)
    monkeypatch.setattr(
        latex,
        "audit_visual_lines",
        lambda pdf, data: {"performed": True, "complete": True, "threshold": 0.25, "awkward": []},
    )

    fitted, _, _, changes, audit = latex.fit_one_page(_resume_data(), _profile())
    assert fitted["courses"] == []
    assert fitted["awards"] == []
    assert changes == [
        "Removed Relevant Courses to reduce the resume to one page",
        "Removed Awards to reduce the resume to one page",
    ]
    assert audit["performed"] is True


def test_visual_overflow_is_reported_without_rewriting_or_failing(monkeypatch) -> None:
    data = _resume_data()
    original_bullets = list(data["experience"][0]["bullets"])
    monkeypatch.setattr(latex, "compile_latex", lambda source: source.encode())
    monkeypatch.setattr(latex, "pdf_page_count", lambda pdf: 1)
    monkeypatch.setattr(
        latex,
        "audit_visual_lines",
        lambda pdf, resume: {
            "performed": True,
            "complete": True,
            "threshold": 0.25,
            "awkward": [{"entity_id": "experience_1", "bullet_index": 0}],
        },
    )

    fitted, _, _, changes, audit = latex.fit_one_page(data, _profile())
    assert fitted["experience"][0]["bullets"] == original_bullets
    assert changes == []
    assert audit["awkward"]
    assert audit["policy"] == "informational_only_source_bullets_are_never_rewritten"


def test_tailored_artifact_download_is_scoped_to_output_directory(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "tailored"
    output_dir.mkdir()
    tex_path = output_dir / "Acme_Engineer_Tailored_Resume.tex"
    tex_path.write_text("tex", encoding="utf-8")
    tex_path.with_suffix(".pdf").write_bytes(b"%PDF-test")
    tex_path.with_name(f"{tex_path.stem}_REPORT.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "TAILORED_DIR", output_dir)

    conn = init_db(tmp_path / "jobs.db")
    conn.execute(
        "INSERT INTO jobs (url, title, tailored_resume_path) VALUES (?, ?, ?)",
        ("https://example.com/job", "Engineer", str(tex_path)),
    )
    conn.commit()

    path, body, content_type = load_tailored_artifact(
        "https://example.com/job", "pdf", conn
    )
    assert path == tex_path.with_suffix(".pdf")
    assert body.startswith(b"%PDF-")
    assert content_type == "application/pdf"

    outside = tmp_path / "outside.tex"
    outside.write_text("private", encoding="utf-8")
    conn.execute(
        "UPDATE jobs SET tailored_resume_path = ? WHERE url = ?",
        (str(outside), "https://example.com/job"),
    )
    conn.commit()
    with pytest.raises(PermissionError):
        load_tailored_artifact("https://example.com/job", "tex", conn)


def test_retailoring_replaces_artifacts_only_after_success(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "tailored"
    output_dir.mkdir()
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Master resume", encoding="utf-8")
    missing_tex = tmp_path / "resume.tex"
    old_tex = output_dir / "Acme_Engineer_Tailored_Resume.tex"
    old_artifacts = [
        old_tex,
        old_tex.with_suffix(".pdf"),
        old_tex.with_suffix(".txt"),
        old_tex.with_name(f"{old_tex.stem}_REPORT.json"),
        old_tex.with_name(f"{old_tex.stem}_JOB.txt"),
    ]
    for artifact in old_artifacts:
        artifact.write_bytes(b"old")

    conn = init_db(tmp_path / "jobs.db")
    url = "https://example.com/job"
    conn.execute(
        "INSERT INTO jobs (url, title, company, site, full_description, fit_score, "
        "tailored_resume_path, tailor_attempts) VALUES (?, 'Engineer', 'Acme', "
        "'example', 'Complete description', 8, ?, 1)",
        (url, str(old_tex)),
    )
    conn.commit()

    monkeypatch.setattr(tailor, "RESUME_PATH", resume_path)
    monkeypatch.setattr(tailor, "RESUME_TEX_PATH", missing_tex)
    monkeypatch.setattr(tailor, "TAILORED_DIR", output_dir)
    monkeypatch.setattr(tailor, "get_connection", lambda: conn)
    monkeypatch.setattr(tailor, "load_profile", _profile)
    monkeypatch.setattr(
        tailor,
        "tailor_resume",
        lambda *_args, **_kwargs: (
            "Tailored text",
            {"status": "approved", "attempts": 1, "structured_data": _resume_data()},
        ),
    )
    monkeypatch.setattr(
        latex,
        "fit_one_page",
        lambda *_args, **_kwargs: (_resume_data(), "new tex", b"%PDF-new", [], {}),
    )
    monkeypatch.setattr(tailor, "build_decision_summary", lambda *_args: {})

    result = tailor.run_tailoring(target_url=url, replace_existing=True)

    assert result["approved"] == 1
    row = conn.execute(
        "SELECT tailored_resume_path, tailor_attempts FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    replacement = row["tailored_resume_path"]
    assert replacement != str(old_tex)
    assert row["tailor_attempts"] == 2
    assert all(not artifact.exists() for artifact in old_artifacts)
    assert Path(replacement).exists()
    assert Path(replacement).with_suffix(".pdf").exists()

    monkeypatch.setattr(
        tailor,
        "tailor_resume",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("generation failed")),
    )
    failed = tailor.run_tailoring(target_url=url, replace_existing=True)
    preserved = conn.execute(
        "SELECT tailored_resume_path, tailor_attempts FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    assert failed["errors"] == 1
    assert preserved["tailored_resume_path"] == replacement
    assert preserved["tailor_attempts"] == 3
    assert Path(replacement).exists()


def test_cancelled_retailoring_preserves_artifacts_and_attempt_count(
    tmp_path, monkeypatch
) -> None:
    output_dir = tmp_path / "tailored"
    output_dir.mkdir()
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Master resume", encoding="utf-8")
    old_tex = output_dir / "Existing_Tailored_Resume.tex"
    old_tex.write_text("old tex", encoding="utf-8")

    conn = init_db(tmp_path / "jobs.db")
    url = "https://example.com/job/cancel"
    conn.execute(
        "INSERT INTO jobs (url, title, company, site, full_description, fit_score, "
        "tailored_resume_path, tailor_attempts) VALUES (?, 'Engineer', 'Acme', "
        "'example', 'Complete description', 8, ?, 2)",
        (url, str(old_tex)),
    )
    conn.commit()

    cancelled = False

    def generate(*_args, **_kwargs):
        nonlocal cancelled
        cancelled = True
        return (
            "Tailored text",
            {"status": "approved", "attempts": 1, "structured_data": _resume_data()},
        )

    monkeypatch.setattr(tailor, "RESUME_PATH", resume_path)
    monkeypatch.setattr(tailor, "RESUME_TEX_PATH", tmp_path / "missing.tex")
    monkeypatch.setattr(tailor, "TAILORED_DIR", output_dir)
    monkeypatch.setattr(tailor, "get_connection", lambda: conn)
    monkeypatch.setattr(tailor, "load_profile", _profile)
    monkeypatch.setattr(tailor, "tailor_resume", generate)

    with pytest.raises(tailor.TailoringCancelled):
        tailor.run_tailoring(
            target_url=url,
            replace_existing=True,
            cancel_check=lambda: cancelled,
        )

    row = conn.execute(
        "SELECT tailored_resume_path, tailor_attempts FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    assert row["tailored_resume_path"] == str(old_tex)
    assert row["tailor_attempts"] == 2
    assert old_tex.read_text(encoding="utf-8") == "old tex"
    assert sorted(path.name for path in output_dir.iterdir()) == [old_tex.name]
