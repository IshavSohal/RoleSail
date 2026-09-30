"""Safe rendering and inspection for the supported LaTeX resume template."""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
import tempfile
import unicodedata
from itertools import pairwise
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

TEMPLATE_PATH = Path(__file__).parents[1] / "templates" / "jake_resume.tex"


def escape_latex(value: object) -> str:
    """Escape untrusted plain text before inserting it into LaTeX."""
    text = str(value or "")
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def _url(value: object) -> str:
    """Escape a URL for hyperref without allowing arbitrary LaTeX."""
    return escape_latex(str(value or "").strip())


def _as_bullet_text(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return str(value or "")


def _render_bullet(value: object) -> str:
    """Escape bullet text and restore validated source-authored bold spans."""
    text = _as_bullet_text(value)
    if not isinstance(value, dict):
        return escape_latex(text)

    raw_spans = value.get("bold_spans", [])
    spans: list[tuple[int, int]] = []
    if isinstance(raw_spans, list):
        for span in raw_spans:
            if (
                isinstance(span, (list, tuple))
                and len(span) == 2
                and all(isinstance(position, int) and not isinstance(position, bool) for position in span)
            ):
                start, end = span
                if 0 <= start < end <= len(text):
                    spans.append((start, end))

    spans.sort()
    if any(
        start < previous_end
        for (_, previous_end), (start, _) in pairwise(spans)
    ):
        return escape_latex(text)

    rendered: list[str] = []
    cursor = 0
    for start, end in spans:
        rendered.append(escape_latex(text[cursor:start]))
        rendered.append(rf"\textbf{{{escape_latex(text[start:end])}}}")
        cursor = end
    rendered.append(escape_latex(text[cursor:]))
    return "".join(rendered)


def _master_header(resume_text: str) -> str | None:
    """Return the master header verbatim when it uses a safe LaTeX subset."""
    if not resume_text or r"\begin{document}" not in resume_text:
        return None
    document = resume_text.split(r"\begin{document}", 1)[1]
    match = re.search(
        r"\\begin\{center\}(.*?)\\end\{center\}",
        document,
        re.DOTALL,
    )
    if not match:
        return None

    body = match.group(1)
    allowed_commands = {
        "textbf", "Huge", "huge", "Large", "large", "normalsize",
        "scshape", "vspace", "href", "underline", "small", "footnotesize",
    }
    allowed_escapes = {"\\", "&", "%", "#", "_", "$"}
    for command in re.findall(r"\\([A-Za-z]+|.)", body):
        if command not in allowed_commands and command not in allowed_escapes:
            return None

    hrefs = re.findall(r"\\href\{([^{}]*)\}", body)
    if len(hrefs) != body.count(r"\href{"):
        return None
    if any(not target.startswith(("https://", "http://", "mailto:")) for target in hrefs):
        return None
    return "\\begin{center}" + body + "\\end{center}"


def _phone_label(value: object) -> str:
    phone = str(value or "").strip()
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return phone


def _header(profile: dict, master_resume_text: str = "") -> str:
    preserved = _master_header(master_resume_text)
    if preserved:
        return preserved

    personal = profile.get("personal", {})
    name = escape_latex(personal.get("full_name", ""))
    contact: list[str] = []
    if personal.get("phone"):
        contact.append(escape_latex(_phone_label(personal["phone"])))
    if personal.get("email"):
        email = _url(personal["email"])
        contact.append(rf"\href{{mailto:{email}}}{{\underline{{{email}}}}}")
    if personal.get("linkedin_url"):
        value = _url(personal["linkedin_url"])
        label = escape_latex(personal.get("full_name") or "LinkedIn")
        contact.append(rf"\href{{{value}}}{{LinkedIn: \underline{{{label}}}}}")
    if personal.get("github_url"):
        raw_value = str(personal["github_url"])
        value = _url(raw_value)
        label = escape_latex(raw_value.rstrip("/").rsplit("/", 1)[-1] or "GitHub")
        contact.append(rf"GitHub: \href{{{value}}}{{\underline{{{label}}}}}")
    seen_urls: set[str] = set()
    for key in ("portfolio_url", "website_url"):
        raw_value = str(personal.get(key) or "").strip()
        normalized = raw_value.rstrip("/")
        if not raw_value or normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        value = _url(raw_value)
        label = re.sub(r"^https?://", "", raw_value).rstrip("/")
        contact.append(rf"\href{{{value}}}{{\underline{{{escape_latex(label)}}}}}")
    contact_line = r" $|$ ".join(contact)
    return (
        "\\begin{center}\n"
        rf"    \textbf{{\Huge \scshape {name}}} \\ \vspace{{1pt}}" "\n"
        rf"    \small {contact_line}" "\n"
        "\\end{center}"
    )


def _education(entries: object) -> str:
    if not isinstance(entries, list) or not entries:
        return ""
    rows: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rows.append(
            "    \\resumeSubheading\n"
            f"      {{{escape_latex(entry.get('institution'))}}}"
            f"{{{escape_latex(entry.get('location'))}}}\n"
            f"      {{{escape_latex(entry.get('degree'))}}}"
            f"{{{escape_latex(entry.get('dates'))}}}"
        )
    if not rows:
        return ""
    return "\\section{Education}\n  \\resumeSubHeadingListStart\n" + "\n".join(rows) + "\n  \\resumeSubHeadingListEnd"


def _experience(entries: object) -> str:
    if not isinstance(entries, list) or not entries:
        return ""
    rows: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        bullets = "\n".join(
            f"        \\resumeItem{{{_render_bullet(b)}}}"
            for b in entry.get("bullets", [])
            if _as_bullet_text(b).strip()
        )
        rows.append(
            "    \\resumeSubheading\n"
            f"      {{{escape_latex(entry.get('role') or entry.get('header'))}}}"
            f"{{{escape_latex(entry.get('dates'))}}}\n"
            f"      {{{escape_latex(entry.get('company') or entry.get('subtitle'))}}}"
            f"{{{escape_latex(entry.get('location'))}}}\n"
            "      \\resumeItemListStart\n"
            f"{bullets}\n"
            "      \\resumeItemListEnd"
        )
    if not rows:
        return ""
    return "\\section{Experience}\n  \\resumeSubHeadingListStart\n\n" + "\n\n".join(rows) + "\n  \\resumeSubHeadingListEnd"


def _projects(entries: object) -> str:
    if not isinstance(entries, list) or not entries:
        return ""
    rows: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = escape_latex(entry.get("name") or entry.get("header"))
        raw_technologies = entry.get("technologies") or entry.get("subtitle") or ""
        if isinstance(raw_technologies, list):
            raw_technologies = ", ".join(str(value) for value in raw_technologies)
        technologies = escape_latex(raw_technologies)
        heading = rf"\textbf{{{name}}}"
        if technologies:
            heading += rf" $|$ \emph{{{technologies}}}"
        bullets = "\n".join(
            f"            \\resumeItem{{{_render_bullet(b)}}}"
            for b in entry.get("bullets", [])
            if _as_bullet_text(b).strip()
        )
        rows.append(
            "      \\resumeProjectHeading\n"
            f"          {{{heading}}}{{{escape_latex(entry.get('dates'))}}}\n"
            "          \\resumeItemListStart\n"
            f"{bullets}\n"
            "          \\resumeItemListEnd"
        )
    if not rows:
        return ""
    return "\\section{Projects}\n    \\resumeSubHeadingListStart\n" + "\n".join(rows) + "\n    \\resumeSubHeadingListEnd"


def _simple_section(title: str, values: object) -> str:
    if not isinstance(values, list) or not values:
        return ""
    items = "\n".join(f"    \\resumeItem{{{escape_latex(v)}}}" for v in values if str(v).strip())
    return rf"\section{{{title}}}" + "\n  \\resumeItemListStart\n" + items + "\n  \\resumeItemListEnd"


def _skills(skills: object) -> str:
    if not isinstance(skills, dict) or not skills:
        return ""
    rows: list[str] = []
    for category, values in skills.items():
        value = ", ".join(str(v) for v in values) if isinstance(values, list) else str(values)
        if value.strip():
            rows.append(
                rf"     \textbf{{{escape_latex(category)}}}{{: {escape_latex(value)}}}"
                + r" \\"
            )
    if rows:
        rows[-1] = rows[-1].removesuffix(r" \\")
    body = "\n".join(rows)
    return (
        "\\section{Technical Skills}\n"
        " \\begin{itemize}[leftmargin=0.15in, label={}]\n"
        "    \\small{\\item{\n"
        f"{body}\n"
        "    }}\n"
        " \\end{itemize}"
    )


def render_resume_tex(
    data: dict,
    profile: dict,
    master_resume_text: str = "",
) -> str:
    """Render validated structured resume data with the trusted template."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "@@HEADER@@": _header(profile, master_resume_text),
        "@@EDUCATION@@": _education(data.get("education")),
        "@@EXPERIENCE@@": _experience(data.get("experience")),
        "@@PROJECTS@@": _projects(data.get("projects")),
        "@@COURSES@@": _simple_section("Relevant Courses", data.get("courses")),
        "@@AWARDS@@": _simple_section("Awards", data.get("awards")),
        "@@SKILLS@@": _skills(data.get("skills")),
    }
    for marker, content in replacements.items():
        template = template.replace(marker, content)
    return template


def _tectonic_executable() -> str | None:
    found = shutil.which("tectonic")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "tectonic"
    return str(local) if local.is_file() and local.stat().st_mode & 0o111 else None


def prepare_for_tectonic(content: str) -> str:
    """Disable the two pdfTeX-only glyph-map commands for XeTeX/Tectonic."""
    prepared: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith((r"\input{glyphtounicode}", r"\pdfgentounicode")):
            prepared.append(f"% {line}  % disabled for Tectonic/XeTeX")
        else:
            prepared.append(line)
    return "\n".join(prepared) + ("\n" if content.endswith("\n") else "")


def compile_latex(content: str) -> bytes:
    """Compile trusted rendered LaTeX and return PDF bytes."""
    executable = _tectonic_executable()
    if not executable:
        raise ValueError("Tectonic is not installed")
    with tempfile.TemporaryDirectory(prefix="rolesail-tailored-tex-") as directory:
        work_dir = Path(directory)
        source = work_dir / "resume.tex"
        source.write_text(prepare_for_tectonic(content), encoding="utf-8")
        result = subprocess.run(
            [executable, "--outdir", str(work_dir), str(source)],
            cwd=work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
        pdf_path = work_dir / "resume.pdf"
        if result.returncode or not pdf_path.exists():
            detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())[-4000:]
            raise ValueError(f"LaTeX compilation failed:\n{detail or 'Tectonic did not produce a PDF'}")
        pdf = pdf_path.read_bytes()
        if not pdf.startswith(b"%PDF-"):
            raise ValueError("Tectonic produced an invalid PDF")
        return pdf


def pdf_page_count(pdf: bytes) -> int:
    """Return page count using PyMuPDF, with a conservative PDF marker fallback."""
    try:
        import fitz

        with fitz.open(stream=pdf, filetype="pdf") as document:
            return document.page_count
    except ImportError:
        executable = shutil.which("pdfinfo")
        if executable:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
                handle.write(pdf)
                handle.flush()
                result = subprocess.run(
                    [executable, handle.name], capture_output=True, text=True, check=False
                )
            match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.MULTILINE)
            if match:
                return int(match.group(1))
        return len(re.findall(rb"/Type\s*/Page\b", pdf))


def _normalized_words(value: str) -> list[str]:
    text = unicodedata.normalize("NFKD", value).lower()
    return re.findall(r"[a-z0-9+#.%]+", text)


def audit_visual_lines(pdf: bytes, data: dict) -> dict[str, Any]:
    """Find bullets ending in a very short continuation line.

    Poppler's bounding-box output is used because it is widely available and
    does not alter the PDF. A multi-line bullet is considered awkward when its
    final line occupies no more than 25% of the page width.
    """
    executable = shutil.which("pdftotext")
    if not executable:
        return {"performed": False, "complete": False, "reason": "pdftotext is not installed", "awkward": []}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
        handle.write(pdf)
        handle.flush()
        result = subprocess.run(
            [executable, "-bbox-layout", handle.name, "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    if result.returncode:
        return {"performed": False, "complete": False, "reason": result.stderr.strip(), "awkward": []}
    try:
        root = ET.fromstring(result.stdout)
    except ET.ParseError as exc:
        return {"performed": False, "complete": False, "reason": f"invalid bbox output: {exc}", "awkward": []}

    lines: list[dict[str, Any]] = []
    page_width = 612.0
    for page in root.iter():
        if page.tag.endswith("page"):
            page_width = float(page.attrib.get("width", page_width))
        if not page.tag.endswith("line"):
            continue
        words = [node.text or "" for node in page.iter() if node.tag.endswith("word")]
        if words:
            lines.append({
                "words": _normalized_words(" ".join(words)),
                "width": float(page.attrib.get("xMax", 0)) - float(page.attrib.get("xMin", 0)),
            })

    awkward: list[dict[str, Any]] = []
    total_bullets = sum(
        len(entry.get("bullets", []))
        for section in ("experience", "projects")
        for entry in data.get(section, [])
    )
    matched_bullets = 0
    search_from = 0
    for section in ("experience", "projects"):
        for entry in data.get(section, []):
            for bullet_index, bullet in enumerate(entry.get("bullets", [])):
                target = _normalized_words(_as_bullet_text(bullet))
                if not target:
                    continue
                match: tuple[int, int] | None = None
                for start in range(search_from, len(lines)):
                    collected: list[str] = []
                    for end in range(start, min(start + 8, len(lines))):
                        collected.extend(lines[end]["words"])
                        if collected == target:
                            match = (start, end)
                            break
                        if len(collected) > len(target) or collected != target[:len(collected)]:
                            break
                    if match:
                        break
                if not match:
                    continue
                matched_bullets += 1
                start, end = match
                search_from = end + 1
                ratio = lines[end]["width"] / page_width if page_width else 1.0
                if end > start and ratio <= 0.25:
                    awkward.append({
                        "section": section,
                        "entity_id": entry.get("entity_id", ""),
                        "bullet_index": bullet_index,
                        "line_count": end - start + 1,
                        "last_line_width_ratio": round(ratio, 3),
                    })
    return {
        "performed": True,
        "complete": matched_bullets == total_bullets,
        "matched_bullets": matched_bullets,
        "total_bullets": total_bullets,
        "threshold": 0.25,
        "awkward": awkward,
    }


def fit_one_page(
    data: dict,
    profile: dict,
    master_resume_text: str = "",
) -> tuple[dict, str, bytes, list[str], dict]:
    """Fit the PDF by omitting low-priority content, never rewriting bullets."""
    fitted = copy.deepcopy(data)
    changes: list[str] = []

    def compile_current() -> tuple[str, bytes]:
        source = render_resume_tex(fitted, profile, master_resume_text)
        return source, compile_latex(source)

    def checked_audit(current_pdf: bytes) -> dict:
        result = audit_visual_lines(current_pdf, fitted)
        # The audit is diagnostic only. Source bullets belong to the user and
        # tailoring must not rewrite them to satisfy a cosmetic heuristic.
        result["policy"] = "informational_only_source_bullets_are_never_rewritten"
        return result

    source, pdf = compile_current()
    if pdf_page_count(pdf) == 1:
        audit = checked_audit(pdf)
        return fitted, source, pdf, changes, audit
    for field, label in (("courses", "Relevant Courses"), ("awards", "Awards")):
        if fitted.get(field):
            fitted[field] = []
            changes.append(f"Removed {label} to reduce the resume to one page")
            source, pdf = compile_current()
            if pdf_page_count(pdf) == 1:
                audit = checked_audit(pdf)
                return fitted, source, pdf, changes, audit

    # Remove one lowest-priority selected bullet per compile, retaining at
    # least two bullets on every selected entity and never removing an entity.
    entries = list(reversed(fitted.get("projects", []))) + list(reversed(fitted.get("experience", [])))
    while pdf_page_count(pdf) > 1:
        candidate = next((entry for entry in entries if len(entry.get("bullets", [])) > 2), None)
        if candidate is None:
            raise ValueError("Tailored resume cannot fit one page without removing a selected entity")
        candidate["bullets"].pop()
        changes.append(f"Removed the lowest-priority bullet from {candidate.get('entity_id', 'an entity')} as a last resort")
        source, pdf = compile_current()
    return fitted, source, pdf, changes, checked_audit(pdf)
