"""HTTP transport and runtime for the interactive RoleSail dashboard."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import sqlite3
import threading
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from rolesail import config
from rolesail.dashboard_data import load_dashboard_jobs
from rolesail.database import get_connection

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 32768
MAX_SETTINGS_BYTES = 262144
MAX_RESUME_BYTES = 1_000_000
# JSON may expand control-heavy text to six bytes per source byte.
MAX_RESUME_REQUEST_BYTES = 7_000_000
MAX_URL_LENGTH = 2048
MAX_TAILORING_QUEUE_SIZE = 100
TAILORING_HISTORY_SIZE = 20
WEB_DIST_DIR = Path(__file__).parent.parent / "web_dist"
EXTERNAL_EMPLOYER_DOMAINS = {
    "konrad.com": {
        "name": "Konrad",
        "greenhouse_board": "konradgroup",
    },
    "salesforce.com": {
        "name": "Salesforce",
        "workday_employer": "salesforce",
    },
}
_settings_write_lock = threading.Lock()
_DELETE_SETTING = {"__rolesail_delete__": True}


from rolesail.dashboard.jobs import (
    clear_tailored_resume,
    delete_job,
    enrich_external_job,
    import_external_job,
    job_import_status,
    load_dashboard_company_logo,
    load_tailored_artifact,
    mark_job_applied,
    unmark_job_applied,
)
from rolesail.dashboard.settings import (
    load_dashboard_resume,
    load_dashboard_settings,
    save_dashboard_profile,
    save_dashboard_resume,
    save_dashboard_searches,
)
from rolesail.dashboard.tasks import (
    TailoringQueueFullError,
    cancel_tailoring,
    run_outreach_dispatcher,
    start_dashboard_pipeline,
    start_discovery,
    start_tailoring,
    tailoring_status,
)


class DashboardHTTPServer(ThreadingHTTPServer):
    """Threaded localhost server with a bounded enrichment pool."""

    daemon_threads = True

    def __init__(self, server_address, handler_class):
        super().__init__(server_address, handler_class)
        from rolesail.usage import recover_interrupted_runs
        recover_interrupted_runs()
        self.enrichment_pool = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="rolesail-enrich",
        )
        self.discovery_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="rolesail-discovery",
        )
        self.tailoring_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="rolesail-tailoring",
        )
        self.pipeline_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="rolesail-pipeline",
        )
        self.outreach_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="rolesail-outreach",
        )
        self.pipeline_lock = threading.Lock()
        self.discovery_lock = threading.Lock()
        self.discovery_state = {
            "status": "idle",
            "workers": None,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        self.tailoring_lock = threading.Lock()
        self.tailoring_queue: deque[dict] = deque()
        self.tailoring_current: dict | None = None
        self.tailoring_recent: deque[dict] = deque(maxlen=TAILORING_HISTORY_SIZE)
        self.tailoring_processor_running = False
        self.tailoring_stopping = False
        self.render_lock = threading.Lock()
        self.outreach_dispatcher_stop = threading.Event()
        self.outreach_dispatcher_wake = threading.Event()
        self.outreach_dispatcher_thread: threading.Thread | None = None
        if os.environ.get("OUTREACH_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
            from rolesail.outreach.service import prepare_batch, recover_reapplied_batches
            recover_reapplied_batches(get_connection())
            rows = get_connection().execute(
                "SELECT id FROM outreach_batches WHERE status IN ('queued', 'preparing')"
            ).fetchall()
            get_connection().execute(
                "UPDATE outreach_batches SET status = 'queued' WHERE status = 'preparing'"
            )
            get_connection().commit()
            for row in rows:
                self.outreach_pool.submit(prepare_batch, row["id"])
            self.outreach_dispatcher_thread = threading.Thread(
                target=run_outreach_dispatcher,
                args=(self.outreach_dispatcher_stop, self.outreach_dispatcher_wake),
                name="rolesail-outreach-dispatcher",
                daemon=True,
            )
            self.outreach_dispatcher_thread.start()

    def server_close(self) -> None:
        self.outreach_dispatcher_stop.set()
        self.outreach_dispatcher_wake.set()
        if self.outreach_dispatcher_thread:
            self.outreach_dispatcher_thread.join(timeout=2)
        self.enrichment_pool.shutdown(wait=False, cancel_futures=True)
        self.discovery_pool.shutdown(wait=False, cancel_futures=True)
        with self.tailoring_lock:
            self.tailoring_stopping = True
            self.tailoring_queue.clear()
        self.tailoring_pool.shutdown(wait=False, cancel_futures=True)
        self.pipeline_pool.shutdown(wait=False, cancel_futures=True)
        self.outreach_pool.shutdown(wait=False, cancel_futures=True)
        super().server_close()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve the dashboard and its external-job API."""

    server: DashboardHTTPServer

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _read_json(self, max_bytes: int = MAX_REQUEST_BYTES) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > max_bytes:
            raise ValueError("Invalid request size")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("Content-Type must be application/json")
        payload = json.loads(self.rfile.read(content_length))
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        return payload

    def _validate_origin(self) -> None:
        self._validate_local_host()
        origin = self.headers.get("Origin")
        if not origin:
            return
        origin_host = urlparse(origin).netloc.lower()
        request_host = self.headers.get("Host", "").lower()
        if not origin_host or origin_host != request_host:
            raise PermissionError("Cross-origin settings updates are not allowed")

    def _validate_local_host(self) -> None:
        request_host = self.headers.get("Host", "")
        hostname = urlparse(f"//{request_host}").hostname
        bound_host = str(self.server.server_address[0]).lower()
        allowed = {"127.0.0.1", "localhost", "::1", bound_host}
        if not hostname or hostname.lower() not in allowed:
            raise PermissionError("Settings are only available from localhost")

    def _send_web_asset(self, relative_path: str, *, shell: bool = False) -> None:
        """Serve a file from the packaged Vite build without path traversal."""
        try:
            relative = Path(relative_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(relative_path)
            root = WEB_DIST_DIR.resolve(strict=True)
            target = (root / relative).resolve(strict=True)
            target.relative_to(root)
            if not target.is_file():
                raise FileNotFoundError(relative_path)
        except (FileNotFoundError, RuntimeError, ValueError):
            self._send_json(404, {"error": "Dashboard asset not found"})
            return

        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
            "image/svg+xml",
        }:
            content_type += "; charset=utf-8"
        cache_control = "no-cache" if shell else "public, max-age=31536000, immutable"
        self._send_bytes(200, target.read_bytes(), content_type, cache_control)

    def _send_spa_shell(self) -> None:
        self._send_web_asset("index.html", shell=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_spa_shell()
            return

        if parsed.path.startswith("/assets/"):
            self._send_web_asset(parsed.path.removeprefix("/"))
            return

        if parsed.path == "/api/jobs":
            try:
                self._validate_local_host()
                self._send_json(200, {"jobs": load_dashboard_jobs(get_connection())})
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except sqlite3.Error as exc:
                log.exception("Could not load dashboard jobs")
                self._send_json(500, {"error": f"Could not load jobs: {exc}"})
            return

        if parsed.path == "/api/jobs/company-logo":
            raw_url = parse_qs(parsed.query).get("url", [""])[0]
            try:
                logo = load_dashboard_company_logo(raw_url)
                if not logo:
                    self._send_json(404, {"error": "Company logo unavailable"})
                    return
                body, content_type = logo
                self._send_bytes(200, body, content_type)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/jobs/status":
            raw_url = parse_qs(parsed.query).get("url", [""])[0]
            try:
                self._send_json(200, job_import_status(raw_url))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/outreach":
            try:
                self._validate_local_host()
                from rolesail.outreach.gmail import connected_account
                from rolesail.outreach.service import get_batch, refresh_delivery_statuses
                query = parse_qs(parsed.query)
                identifier = query.get("batch_id", query.get("job_url", [""]))[0]
                batch = get_batch(identifier)
                if not batch:
                    self._send_json(404, {"error": "Outreach batch not found"})
                    return
                if batch["status"] == "sending":
                    batch = refresh_delivery_statuses(identifier)
                self._send_json(200, {"batch": batch, "gmail_account": connected_account()})
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except (ValueError, RuntimeError) as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/discovery/status":
            with self.server.discovery_lock:
                state = dict(self.server.discovery_state)
            self._send_json(200, state)
            return

        if parsed.path == "/api/tailoring/status":
            self._send_json(200, tailoring_status(self.server))
            return

        if parsed.path == "/api/pipeline/status":
            from rolesail.usage import get_run
            run_id = parse_qs(parsed.query).get("run_id", [None])[0]
            self._send_json(200, {"run": get_run(run_id)})
            return

        if parsed.path == "/api/usage/summary":
            from rolesail.usage import usage_summary
            query = parse_qs(parsed.query)
            self._send_json(200, usage_summary(
                query.get("run_id", [None])[0],
                stage=query.get("stage", [None])[0],
                provider=query.get("provider", [None])[0],
                model=query.get("model", [None])[0],
            ))
            return

        if parsed.path == "/api/usage/history":
            try:
                from rolesail.usage import usage_history
                query = parse_qs(parsed.query)
                self._send_json(200, {"entries": usage_history(
                    run_id=query.get("run_id", [None])[0],
                    stage=query.get("stage", [None])[0],
                    provider=query.get("provider", [None])[0],
                    model=query.get("model", [None])[0],
                    limit=int(query.get("limit", ["100"])[0]),
                )})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/settings/pricing":
            try:
                self._validate_local_host()
                from rolesail.usage import load_pricing
                self._send_json(200, load_pricing())
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            return

        if parsed.path == "/api/settings":
            try:
                self._validate_local_host()
                self._send_json(200, load_dashboard_settings())
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except (FileNotFoundError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/resume/pdf":
            try:
                self._validate_local_host()
                if (
                    not config.RESUME_TEX_PATH.exists()
                    or not config.RESUME_PDF_PATH.exists()
                ):
                    self._send_json(404, {"error": "Compiled LaTeX resume not found"})
                    return
                self._send_bytes(
                    200,
                    config.RESUME_PDF_PATH.read_bytes(),
                    "application/pdf",
                )
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except OSError as exc:
                self._send_json(500, {"error": f"Could not read resume PDF: {exc}"})
            return

        if parsed.path == "/api/resume":
            try:
                self._validate_local_host()
                resume_format = parse_qs(parsed.query).get("format", ["txt"])[0]
                self._send_json(200, load_dashboard_resume(resume_format))
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except (OSError, UnicodeError) as exc:
                self._send_json(500, {"error": f"Could not read resume: {exc}"})
            return

        if parsed.path == "/api/jobs/artifact":
            try:
                self._validate_local_host()
                query = parse_qs(parsed.query)
                path, body, content_type = load_tailored_artifact(
                    query.get("url", [""])[0],
                    query.get("kind", [""])[0],
                )
                disposition = query.get("disposition", ["download"])[0]
                if disposition not in {"inline", "download"}:
                    raise ValueError("Disposition must be inline or download")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                header_disposition = "inline" if disposition == "inline" else "attachment"
                self.send_header("Content-Disposition", f'{header_disposition}; filename="{path.name}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except (FileNotFoundError, OSError) as exc:
                self._send_json(404, {"error": str(exc)})
            return

        # BrowserRouter routes are handled by the SPA. API typos must remain
        # JSON 404s rather than receiving HTML.
        if not parsed.path.startswith("/api/") and parsed.path in {"/profile"}:
            self._send_spa_shell()
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {
            "/api/jobs",
            "/api/jobs/applied",
            "/api/jobs/delete",
            "/api/jobs/tailored/clear",
            "/api/discovery",
            "/api/tailoring",
            "/api/tailoring/job",
            "/api/tailoring/cancel",
            "/api/pipeline",
            "/api/outreach/prepare",
            "/api/outreach/preview",
            "/api/outreach/approve",
            "/api/outreach/gmail-drafts",
            "/api/outreach/reset-gmail-draft",
            "/api/outreach/redraft",
            "/api/outreach/retry",
            "/api/outreach/cancel",
            "/api/outreach/cancel-pending",
            "/api/outreach/clear",
            "/api/outreach/suppress",
            "/api/outreach/restore-suppressed",
        }:
            self._send_json(404, {"error": "Not found"})
            return

        try:
            if path.startswith("/api/outreach/"):
                self._validate_origin()
            payload = self._read_json()
            if path == "/api/discovery":
                result = start_discovery(self.server, payload.get("workers", 3))
            elif path == "/api/pipeline":
                result = start_dashboard_pipeline(self.server, payload.get("workers", 3))
            elif path == "/api/tailoring":
                result = start_tailoring(
                    self.server,
                    payload.get("min_score", 7),
                    payload.get("limit", 20),
                    payload.get("validation_mode", "normal"),
                )
            elif path == "/api/tailoring/job":
                result = start_tailoring(
                    self.server,
                    min_score=1,
                    limit=1,
                    validation_mode=payload.get("validation_mode", "normal"),
                    target_url=payload.get("url"),
                    replace_existing=payload.get("replace_existing", False),
                )
            elif path == "/api/tailoring/cancel":
                result = cancel_tailoring(self.server, payload.get("url", ""))
            elif path == "/api/jobs/applied":
                applied = payload.get("applied", True)
                if not isinstance(applied, bool):
                    raise ValueError("Applied must be a boolean")
                result = (
                    mark_job_applied(payload.get("url", ""))
                    if applied
                    else unmark_job_applied(payload.get("url", ""))
                )
            elif path == "/api/jobs/delete":
                result = delete_job(payload.get("url", ""))
            elif path == "/api/jobs/tailored/clear":
                result = clear_tailored_resume(payload.get("url", ""))
            elif path.startswith("/api/outreach/"):
                from rolesail.outreach.service import (
                    approve_batch,
                    cancel_batch,
                    cancel_pending,
                    clear_cancelled_batch,
                    create_gmail_drafts,
                    enqueue_for_job,
                    prepare_batch,
                    preview_batch_schedule,
                    redraft_batch,
                    reset_uncertain_gmail_draft,
                    restore_suppressed_recipient,
                    retry_batch,
                    suppress_recipient,
                )
                from rolesail.outreach.service import enabled as outreach_enabled

                if path == "/api/outreach/prepare":
                    identifier = payload.get("batch_id") or ""
                    if not identifier:
                        job_url = payload.get("job_url") or ""
                        if not outreach_enabled():
                            raise ValueError(
                                "Employee outreach is disabled. Set OUTREACH_ENABLED=true and restart RoleSail."
                            )
                        batch = enqueue_for_job(job_url)
                        if not batch:
                            raise ValueError(
                                "Outreach can only be prepared for a job marked as applied"
                            )
                        identifier = batch["id"]
                    self.server.outreach_pool.submit(prepare_batch, identifier)
                    result = {"status": "queued", "id": identifier}
                elif path == "/api/outreach/preview":
                    result = {
                        "schedule": preview_batch_schedule(
                            payload.get("batch_id", ""), payload.get("recipient_ids", [])
                        )
                    }
                elif path == "/api/outreach/approve":
                    result = approve_batch(
                        payload.get("batch_id", ""),
                        payload.get("recipients", []),
                        confirmed=payload.get("confirmed") is True,
                    )
                elif path == "/api/outreach/gmail-drafts":
                    result = create_gmail_drafts(
                        payload.get("batch_id", ""),
                        payload.get("recipients", []),
                        confirmed_account=payload.get("confirmed_account", ""),
                    )
                elif path == "/api/outreach/reset-gmail-draft":
                    result = reset_uncertain_gmail_draft(
                        payload.get("recipient_id", ""),
                        confirmed_no_draft=payload.get("confirmed_no_draft") is True,
                    )
                elif path == "/api/outreach/redraft":
                    result = redraft_batch(
                        payload.get("batch_id", ""), feedback=payload.get("feedback", "")
                    )
                elif path == "/api/outreach/retry":
                    result = retry_batch(payload.get("batch_id", ""))
                elif path == "/api/outreach/cancel":
                    result = cancel_batch(payload.get("batch_id", ""))
                elif path == "/api/outreach/cancel-pending":
                    result = cancel_pending(payload.get("batch_id", ""))
                elif path == "/api/outreach/clear":
                    result = clear_cancelled_batch(payload.get("batch_id", ""))
                elif path == "/api/outreach/suppress":
                    result = suppress_recipient(
                        payload.get("recipient_id", ""), payload.get("reason", "user")
                    )
                else:
                    result = restore_suppressed_recipient(payload.get("recipient_id", ""))
                if path in {
                    "/api/outreach/approve",
                    "/api/outreach/retry",
                    "/api/outreach/cancel-pending",
                }:
                    self.server.outreach_dispatcher_wake.set()
            else:
                result = import_external_job(payload.get("url", ""))
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except TailoringQueueFullError as exc:
            self._send_json(429, {"error": str(exc)})
            return
        except RuntimeError as exc:
            self._send_json(409, {"error": str(exc)})
            return
        except sqlite3.Error as exc:
            log.exception("Could not import external job")
            self._send_json(500, {"error": f"Database error: {exc}"})
            return

        if path in {"/api/discovery", "/api/tailoring", "/api/tailoring/job", "/api/pipeline"}:
            self._send_json(202, result)
            return

        if path.startswith("/api/outreach/"):
            if path == "/api/outreach/preview":
                self._send_json(200, result)
            else:
                self._send_json(202 if path == "/api/outreach/prepare" else 200, {"batch": result})
            return

        if path == "/api/tailoring/cancel":
            self._send_json(200, result)
            return

        if path == "/api/jobs/applied":
            if result["updated"]:
                batch = result.get("outreach")
                if batch and batch.get("status") in {"queued", "failed"}:
                    from rolesail.outreach.service import prepare_batch
                    self.server.outreach_pool.submit(prepare_batch, batch["id"])
                self._send_json(200, result)
            else:
                self._send_json(404, {"error": "Job not found"})
            return

        if path == "/api/jobs/delete":
            if result["deleted"]:
                self._send_json(200, result)
            else:
                self._send_json(404, {"error": "Job not found"})
            return

        if path == "/api/jobs/tailored/clear":
            if result["cleared"]:
                self._send_json(200, result)
            elif result["status"] == "missing":
                self._send_json(404, {"error": "Job not found"})
            else:
                self._send_json(404, {"error": "Tailored resume not found"})
            return

        if result.get("enrichment_pending"):
            self.server.enrichment_pool.submit(enrich_external_job, result["url"])
            if result["created"]:
                self._send_json(201, result)
            else:
                result["message"] = "This job is already in the dashboard; enrichment was retried"
                self._send_json(202, result)
        else:
            result["message"] = "This job is already in the dashboard"
            self._send_json(200, result)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if path not in {
            "/api/settings/profile",
            "/api/settings/searches",
            "/api/resume",
            "/api/settings/pricing",
        }:
            self._send_json(404, {"error": "Not found"})
            return

        try:
            self._validate_origin()
            max_bytes = (
                MAX_RESUME_REQUEST_BYTES
                if path == "/api/resume"
                else MAX_SETTINGS_BYTES
            )
            payload = self._read_json(max_bytes)
            if path == "/api/settings/profile":
                result = save_dashboard_profile(payload.get("profile"))
            elif path == "/api/resume":
                result = save_dashboard_resume(
                    payload.get("filename"),
                    payload.get("content"),
                    payload.get("remove_comments", False),
                )
            elif path == "/api/settings/pricing":
                from rolesail.usage import save_pricing
                result = save_pricing(payload.get("overrides"))
            else:
                result = save_dashboard_searches(payload.get("searches"))
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        except (
            FileNotFoundError,
            ValueError,
            json.JSONDecodeError,
            yaml.YAMLError,
        ) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except OSError as exc:
            log.exception("Could not save dashboard settings")
            self._send_json(500, {"error": f"Could not save settings: {exc}"})
            return

        self._send_json(200, result)

    def log_message(self, format: str, *args) -> None:
        log.debug("Dashboard: " + format, *args)


def serve_dashboard(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Run the interactive dashboard until interrupted."""
    server = DashboardHTTPServer((host, port), DashboardRequestHandler)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"RoleSail dashboard: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
