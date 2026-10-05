"""Guard historical import paths while implementations move into packages."""

from rolesail import dashboard_server
from rolesail.dashboard import jobs as dashboard_jobs
from rolesail.dashboard import settings as dashboard_settings
from rolesail.dashboard import tasks as dashboard_tasks
from rolesail.enrichment import detail, extractors, urls
from rolesail.outreach import composition, delivery, repository, service


def test_dashboard_server_reexports_owned_services() -> None:
    assert dashboard_server.normalize_job_url is dashboard_jobs.normalize_job_url
    assert dashboard_server.start_tailoring is dashboard_tasks.start_tailoring
    assert dashboard_server._run_outreach_dispatcher is dashboard_tasks.run_outreach_dispatcher
    assert dashboard_server._DELETE_SETTING is dashboard_settings._DELETE_SETTING


def test_outreach_service_reexports_policy_persistence_and_delivery() -> None:
    assert service.rank_people is composition.rank_people
    assert service.get_batch is repository.get_batch
    assert service.dispatch_due_outreach is delivery.dispatch_due_outreach


def test_enrichment_detail_reexports_helpers() -> None:
    assert detail.resolve_url is urls.resolve_url
    assert detail.extract_from_json_ld is extractors.extract_from_json_ld
    assert detail.DETAIL_EXTRACT_PROMPT is extractors.DETAIL_EXTRACT_PROMPT
