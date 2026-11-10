"""Google Search Console read-only client with local OAuth and optional service account."""

from __future__ import annotations

import logging
import os
import pickle
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"


def _date_range(days: int, today: date | None = None) -> tuple[str, str]:
    if days < 1:
        raise ValueError("days must be at least 1.")
    end = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def _parse_rows(response: dict[str, Any], dimensions: tuple[str, ...]) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for row in response.get("rows", []):
        keys = row.get("keys", [])
        if len(keys) != len(dimensions):
            continue
        item: dict[str, Any] = dict(zip(dimensions, keys))
        item.update({
            "clicks": int(row.get("clicks", 0) or 0),
            "impressions": int(row.get("impressions", 0) or 0),
            "ctr": float(row.get("ctr", 0) or 0),
            "position": float(row.get("position", 0) or 0),
        })
        if "page" in item:
            item["url"] = item.pop("page")
        parsed.append(item)
    return parsed


def upsert_metrics(connection, rows: list[dict[str, Any]]) -> int:
    """Upsert GSC records keyed by their reporting date, page URL, and query."""
    count = 0
    for row in rows:
        metric_date = str(row.get("date", ""))
        url = str(row.get("url", ""))
        query = str(row.get("query", ""))
        if not metric_date:
            raise ValueError("GSC metric row is missing its date.")
        connection.execute(
            """INSERT INTO metrics
               (url, query, impressions, clicks, ctr, position, date)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date, url, query) DO UPDATE SET
                   impressions = excluded.impressions,
                   clicks = excluded.clicks,
                   ctr = excluded.ctr,
                   position = excluded.position""",
            (
                url,
                query,
                int(row.get("impressions", 0) or 0),
                int(row.get("clicks", 0) or 0),
                float(row.get("ctr", 0) or 0),
                float(row.get("position", 0) or 0),
                metric_date,
            ),
        )
        count += 1
    connection.commit()
    return count


class GSCClient:
    """Thin adapter for Search Console searchanalytics.query."""

    def __init__(self, service, site_url: str):
        self.service = service
        self.site_url = site_url

    @classmethod
    def from_settings(cls, settings) -> "GSCClient | None":
        """Create a client from service-account or local installed-app OAuth credentials."""
        service_path = Path(
            os.getenv("GSC_SERVICE_ACCOUNT_FILE", str(settings.gsc_service_account_file))
        )
        client_secret_path = Path(
            os.getenv("GSC_CLIENT_SECRET_FILE", str(settings.gsc_client_secret_file))
        )
        token_path = Path(os.getenv("GSC_TOKEN_FILE", str(settings.gsc_token_file)))
        try:
            from googleapiclient.discovery import build
            from google.oauth2.service_account import Credentials as ServiceCredentials
        except ImportError:
            logging.info(
                "Search Console skipped: install google-api-python-client, google-auth, "
                "and google-auth-oauthlib to enable GSC."
            )
            return None

        try:
            if service_path.is_file():
                credentials = ServiceCredentials.from_service_account_file(
                    str(service_path), scopes=[GSC_SCOPE]
                )
            elif client_secret_path.is_file():
                from google.auth.transport.requests import Request
                from google.oauth2.credentials import Credentials
                from google_auth_oauthlib.flow import InstalledAppFlow

                credentials = None
                if token_path.is_file():
                    try:
                        with token_path.open("rb") as token_file:
                            credentials = pickle.load(token_file)
                    except (OSError, pickle.PickleError, EOFError, AttributeError, ImportError) as exc:
                        logging.warning("Ignoring invalid Search Console token file (%s).", type(exc).__name__)
                if credentials and credentials.expired and credentials.refresh_token:
                    credentials.refresh(Request())
                if not credentials or not credentials.valid:
                    credentials = InstalledAppFlow.from_client_secrets_file(
                        str(client_secret_path), [GSC_SCOPE]
                    ).run_local_server(port=0)
                token_path.parent.mkdir(parents=True, exist_ok=True)
                with token_path.open("wb") as token_file:
                    pickle.dump(credentials, token_file)
            else:
                logging.info(
                    "Search Console skipped: add client_secret.json for local OAuth "
                    "or set GSC_SERVICE_ACCOUNT_FILE for service-account access."
                )
                return None
            service = build("searchconsole", "v1", credentials=credentials, cache_discovery=False)
            return cls(service, settings.site_url)
        except Exception as exc:
            logging.warning(
                "Search Console setup failed (%s); GSC-dependent steps will be skipped.",
                type(exc).__name__,
            )
            return None

    def _query(self, days: int, dimensions: tuple[str, ...]) -> list[dict[str, Any]]:
        start_date, end_date = _date_range(days)
        try:
            response = self.service.searchanalytics().query(
                siteUrl=self.site_url,
                body={
                    "startDate": start_date,
                    "endDate": end_date,
                    "dimensions": list(dimensions),
                    "rowLimit": 25000,
                    "dataState": "final",
                },
            ).execute()
        except Exception as exc:
            logging.warning(
                "Search Console query failed for %s (%s).",
                ",".join(dimensions),
                type(exc).__name__,
            )
            return []
        return _parse_rows(response if isinstance(response, dict) else {}, dimensions)

    def top_queries(self, days: int = 28) -> list[dict[str, Any]]:
        return self._query(days, ("query", "date"))

    def top_pages(self, days: int = 28) -> list[dict[str, Any]]:
        return self._query(days, ("page", "date"))

    def query_page_rows(self, days: int = 28) -> list[dict[str, Any]]:
        return self._query(days, ("query", "page", "date"))

    def sync_metrics(self, connection, days: int = 56) -> int:
        """Fetch query/page daily rows and persist them for reports and refresh decisions."""
        rows = self.top_queries(days)
        rows.extend(self.top_pages(days))
        rows.extend(self.query_page_rows(days))
        return upsert_metrics(connection, rows)
