"""IndexNow submission and sitemap availability checks for published pages only."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from config import SITE_URL

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
INDEXNOW_KEY_FILE_INSTRUCTIONS = (
    "Set INDEXNOW_KEY in .env, then publish a UTF-8 text file named "
    "<INDEXNOW_KEY>.txt at the website root containing only the same key. "
    "Confirm https://<your-host>/<INDEXNOW_KEY>.txt is publicly reachable."
)


@dataclass(frozen=True)
class IndexingResult:
    url: str
    status: str
    indexnow_status: int | None
    sitemap_status: int | None
    message: str


def _validate_site_url(url: str, site_url: str) -> None:
    parsed = urlparse(url)
    site_host = urlparse(site_url).netloc.casefold()
    if parsed.scheme not in {"https", "http"} or parsed.netloc.casefold() != site_host:
        raise ValueError("Indexing is restricted to absolute URLs on the configured site.")


def _sitemap_url(site_url: str) -> str:
    return f"{site_url.rstrip('/')}/sitemap_index.xml"


def ping_sitemap(
    *,
    site_url: str = SITE_URL,
    session: requests.Session | None = None,
    timeout: float = 15,
) -> tuple[int | None, str]:
    """Fetch Rank Math's sitemap URL to verify it is available after a publication."""
    url = _sitemap_url(site_url)
    try:
        response = (session or requests.Session()).get(url, timeout=timeout)
    except requests.RequestException as exc:
        logging.warning("Sitemap fetch failed (%s).", type(exc).__name__)
        return None, f"Sitemap fetch failed ({type(exc).__name__})."
    if response.status_code >= 400:
        return response.status_code, f"Sitemap returned HTTP {response.status_code}."
    return response.status_code, "Rank Math sitemap is reachable."


def notify_published_url(
    url: str,
    *,
    post_status: str,
    indexnow_key: str,
    site_url: str = SITE_URL,
    session: requests.Session | None = None,
    timeout: float = 15,
) -> IndexingResult:
    """Submit one confirmed published URL to IndexNow and verify the sitemap separately."""
    if post_status != "publish":
        return IndexingResult(url, "skipped", None, None, "WordPress post status is not publish.")
    _validate_site_url(url, site_url)
    if not indexnow_key.strip():
        return IndexingResult(
            url,
            "skipped",
            None,
            None,
            "INDEXNOW_KEY is missing. " + INDEXNOW_KEY_FILE_INSTRUCTIONS,
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", indexnow_key.strip()):
        raise ValueError("INDEXNOW_KEY must contain only letters, numbers, underscores, or hyphens.")
    http = session or requests.Session()
    parsed_site = urlparse(site_url)
    key_location = f"{site_url.rstrip('/')}/{indexnow_key.strip()}.txt"
    index_status: int | None = None
    messages: list[str] = []
    try:
        response = http.post(
            INDEXNOW_ENDPOINT,
            json={
                "host": parsed_site.netloc,
                "key": indexnow_key.strip(),
                "keyLocation": key_location,
                "urlList": [url],
            },
            timeout=timeout,
        )
        index_status = response.status_code
        if response.status_code in {200, 202}:
            messages.append(f"IndexNow accepted the published URL (HTTP {response.status_code}).")
        else:
            messages.append(f"IndexNow returned HTTP {response.status_code}.")
    except requests.RequestException as exc:
        messages.append(f"IndexNow request failed ({type(exc).__name__}).")
        logging.warning("IndexNow submission failed (%s).", type(exc).__name__)

    sitemap_status, sitemap_message = ping_sitemap(
        site_url=site_url,
        session=http,
        timeout=timeout,
    )
    messages.append(sitemap_message)
    status = (
        "success"
        if index_status in {200, 202} and sitemap_status is not None and sitemap_status < 400
        else "partial_failure"
    )
    return IndexingResult(url, status, index_status, sitemap_status, " ".join(messages))
