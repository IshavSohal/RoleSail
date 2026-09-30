"""Small, defensive client for the Apollo REST API."""

from __future__ import annotations

import os
import random
import time
from typing import Any

import httpx


class ApolloError(RuntimeError):
    """Apollo returned an error that is safe to show to the user."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class ApolloClient:
    BASE_URL = "https://api.apollo.io/api/v1"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        max_retries: int = 3,
    ) -> None:
        self.api_key = (api_key or os.environ.get("APOLLO_API_KEY", "")).strip()
        if not self.api_key:
            raise ApolloError("APOLLO_API_KEY is not configured")
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.max_retries = max_retries

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        for attempt in range(self.max_retries):
            try:
                response = self.client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers={
                        "x-api-key": self.api_key,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Cache-Control": "no-cache",
                    },
                    json=json,
                    params=params,
                )
            except httpx.TimeoutException as exc:
                if attempt + 1 == self.max_retries:
                    raise ApolloError("Apollo request timed out") from exc
                time.sleep(2**attempt)
                continue

            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < self.max_retries:
                raw_wait = response.headers.get("Retry-After", "")
                try:
                    wait = min(float(raw_wait), 30) if raw_wait else 2**attempt
                except ValueError:
                    wait = 2**attempt
                time.sleep(wait + random.uniform(0, 0.25))
                continue

            if response.is_error:
                detail = ""
                try:
                    payload = response.json()
                    detail = str(payload.get("error") or payload.get("message") or "")
                except (ValueError, TypeError):
                    pass
                labels = {
                    401: "Apollo authentication failed",
                    403: "Apollo plan or API-key permissions do not allow this action",
                    422: "Apollo rejected the request",
                    429: "Apollo rate limit was exceeded",
                }
                message = labels.get(response.status_code, f"Apollo returned HTTP {response.status_code}")
                if detail:
                    message = f"{message}: {detail[:300]}"
                raise ApolloError(message, status_code=response.status_code)
            try:
                payload = response.json()
            except ValueError as exc:
                raise ApolloError("Apollo returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise ApolloError("Apollo returned an unexpected response")
            return payload
        raise ApolloError("Apollo request failed")

    def health(self) -> dict:
        return self._request("GET", "/auth/health")

    def email_accounts(self) -> list[dict]:
        data = self._request("GET", "/email_accounts")
        return list(data.get("email_accounts") or [])

    def search_organizations(self, name: str, *, per_page: int = 10) -> list[dict]:
        data = self._request(
            "POST",
            "/mixed_companies/search",
            json={"q_organization_name": name, "page": 1, "per_page": per_page},
        )
        return list(data.get("organizations") or data.get("accounts") or [])

    def search_people(
        self,
        *,
        organization_id: str | None = None,
        domain: str | None = None,
        titles: list[str] | None = None,
        locations: list[str] | None = None,
        per_page: int = 25,
    ) -> list[dict]:
        body: dict[str, Any] = {
            "page": 1,
            "per_page": min(max(per_page, 1), 100),
            "person_seniorities": ["c_suite", "vp", "head", "director", "manager", "senior"],
            "contact_email_status": ["verified"],
        }
        if organization_id:
            body["organization_ids"] = [organization_id]
        if domain:
            body["q_organization_domains_list"] = [domain]
        if titles:
            body["person_titles"] = titles
            body["include_similar_titles"] = True
        if locations:
            body["person_locations"] = locations
        data = self._request("POST", "/mixed_people/api_search", json=body)
        return list(data.get("people") or [])

    def enrich_person(self, person_id: str) -> dict | None:
        data = self._request(
            "POST",
            "/people/match",
            json={"id": person_id, "reveal_personal_emails": False, "reveal_phone_number": False},
        )
        person = data.get("person")
        return person if isinstance(person, dict) else None

    def create_contact(self, recipient: dict) -> dict:
        data = self._request(
            "POST",
            "/contacts",
            json={
                "first_name": recipient.get("first_name"),
                "last_name": recipient.get("last_name"),
                "email": recipient.get("email"),
                "title": recipient.get("title"),
                "website_url": recipient.get("linkedin_url"),
                "run_dedupe": True,
            },
        )
        contact = data.get("contact")
        if not isinstance(contact, dict) or not contact.get("id"):
            raise ApolloError("Apollo did not return a contact ID")
        return contact

    def create_email_draft(
        self,
        *,
        contact_id: str,
        subject: str,
        body_html: str,
        email_account_id: str,
    ) -> dict:
        # Apollo's one-off draft endpoint sends from the authenticated user's
        # linked/default mailbox. The configured ID is validated separately
        # and retained here as an explicit guard against accidental setup gaps.
        if not email_account_id:
            raise ApolloError("A linked Apollo email account is required")
        data = self._request(
            "POST",
            "/emailer_messages",
            json={
                "contact_id": contact_id,
                "subject": subject,
                "body_html": body_html,
            },
        )
        message = data.get("emailer_message")
        if not isinstance(message, dict) or not message.get("id"):
            raise ApolloError("Apollo did not return an email draft ID")
        return message

    def send_email(self, message_id: str) -> dict:
        return self._request("POST", f"/emailer_messages/{message_id}/send_now", json={"surface": "emails"})

    def email_status(self, message_id: str) -> dict:
        return self._request("POST", "/emailer_messages/email_send_status", json={"id": message_id})
