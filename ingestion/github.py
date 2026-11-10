"""Optional enrichment of commits with pull-request context from the GitHub REST API.

A commit message rarely states *why* a change was made; the pull request that
carried it often does. Enrichment is strictly best-effort: without network access,
without a token on a private repository or once the rate limit is hit, ingestion
simply continues with the information contained in git itself.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


@dataclass(slots=True)
class PullRequest:
    number: int
    title: str
    body: str
    url: str
    author: str | None = None
    merged_at: str | None = None
    labels: tuple[str, ...] = ()


def fetch_pull_requests(
    owner: str,
    repository: str,
    numbers: Iterable[int],
    *,
    token: str | None = None,
    limit: int = 60,
    client: httpx.Client | None = None,
    api_url: str = GITHUB_API,
) -> dict[int, PullRequest]:
    """Fetch up to ``limit`` pull requests; unknown numbers and failures are skipped."""
    wanted = list(dict.fromkeys(numbers))[:limit]
    if not wanted:
        return {}
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    owns_client = client is None
    http = client or httpx.Client(timeout=10.0, headers={"User-Agent": "archaeo-code"})
    found: dict[int, PullRequest] = {}
    try:
        for number in wanted:
            try:
                response = http.get(f"{api_url}/repos/{owner}/{repository}/pulls/{number}", headers=headers)
            except httpx.HTTPError as error:
                logger.info("GitHub enrichment stopped: %s", error.__class__.__name__)
                break
            if response.status_code in (401, 403, 429):
                logger.info(
                    "GitHub enrichment stopped: HTTP %s (credentials or rate limit)", response.status_code
                )
                break
            if response.status_code != 200:
                continue  # an issue number, a PR of another fork, ...
            payload: dict[str, Any] = response.json()
            found[number] = PullRequest(
                number=number,
                title=str(payload.get("title") or ""),
                body=str(payload.get("body") or ""),
                url=str(payload.get("html_url") or ""),
                author=(payload.get("user") or {}).get("login"),
                merged_at=payload.get("merged_at"),
                labels=tuple(
                    str(label.get("name")) for label in payload.get("labels") or () if label.get("name")
                ),
            )
    finally:
        if owns_client:
            http.close()
    return found
