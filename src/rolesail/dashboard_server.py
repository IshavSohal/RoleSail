"""Compatibility entrypoint for the dashboard server.

Dashboard implementation lives in :mod:`rolesail.dashboard`. Imports from
this historical module remain supported for callers and extensions.
"""

# Compatibility facade: moved implementation names remain importable here.
# ruff: noqa: F401

from rolesail.dashboard.http import *
from rolesail.dashboard.http import (
    DashboardHTTPServer,
    DashboardRequestHandler,
    clear_tailored_resume,
    delete_job,
    enrich_external_job,
    import_external_job,
    job_import_status,
    load_dashboard_company_logo,
    load_dashboard_resume,
    load_dashboard_settings,
    load_tailored_artifact,
    mark_job_applied,
    save_dashboard_profile,
    save_dashboard_resume,
    save_dashboard_searches,
    serve_dashboard,
    unmark_job_applied,
)
from rolesail.dashboard.jobs import (
    _amazon_job_id,
    _backfill_external_employer_metadata,
    _enrich_external_amazon_job,
    _enrich_external_workday_job,
    _external_employer,
    _external_workday_job_id,
    normalize_job_url,
)
from rolesail.dashboard.settings import (
    _BEGIN_ENVIRONMENT_RE,
    _DELETE_SETTING,
    _END_ENVIRONMENT_RE,
    _VERBATIM_ENVIRONMENTS,
    _atomic_write,
    _atomic_write_bytes,
    _compile_latex_resume,
    _deep_merge,
    _is_nonnegative_finite_number,
    _prepare_tex_for_tectonic,
    _tectonic_executable,
    _validate_profile,
    _validate_searches,
    remove_latex_comments,
)
from rolesail.dashboard.tasks import (
    TailoringQueueFullError,
    _drain_tailoring_queue,
    _execute_dashboard_pipeline,
    _execute_discovery,
    _run_tailoring_request,
    _tailoring_status_locked,
    _tailoring_target_error,
    cancel_tailoring,
    run_outreach_dispatcher,
    start_dashboard_pipeline,
    start_discovery,
    start_tailoring,
    tailoring_status,
)

_run_outreach_dispatcher = run_outreach_dispatcher

__all__ = [
    "DashboardHTTPServer",
    "DashboardRequestHandler",
    "TailoringQueueFullError",
    "cancel_tailoring",
    "clear_tailored_resume",
    "delete_job",
    "enrich_external_job",
    "import_external_job",
    "job_import_status",
    "load_dashboard_company_logo",
    "load_dashboard_resume",
    "load_dashboard_settings",
    "load_tailored_artifact",
    "mark_job_applied",
    "normalize_job_url",
    "remove_latex_comments",
    "save_dashboard_profile",
    "save_dashboard_resume",
    "save_dashboard_searches",
    "serve_dashboard",
    "start_dashboard_pipeline",
    "start_discovery",
    "start_tailoring",
    "tailoring_status",
    "unmark_job_applied",
]
