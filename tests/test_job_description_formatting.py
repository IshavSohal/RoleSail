from rolesail.view import format_job_description_html


def test_job_description_headings_are_bolded_and_body_text_is_not():
    rendered = format_job_description_html(
        "About the team\nWe build useful products.\n\n"
        "Minimum Qualifications:\n- Three years of experience\n\n"
        "Preferred Qualifications\n- Bachelor's degree\n\n"
        "Salary Range\nUSD 100,000 - 150,000 annually"
    )

    for heading in (
        "About the team",
        "Minimum Qualifications:",
        "Preferred Qualifications",
        "Salary Range",
    ):
        assert (
            f'<strong class="description-section-heading">{heading}</strong>'
            in rendered
        )
    assert (
        '<strong class="description-section-heading">We build useful products.</strong>'
        not in rendered
    )


def test_markdown_job_description_heading_is_bolded_and_html_is_escaped():
    rendered = format_job_description_html(
        "## Required experience\n<script>alert('unsafe')</script>"
    )

    assert (
        '<strong class="description-section-heading">Required experience</strong>'
        in rendered
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
