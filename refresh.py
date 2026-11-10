"""Find underperforming published articles and create review briefs, never edits."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse


def _metric_period(as_of: date) -> tuple[str, str, str]:
    latest = as_of
    recent_start = latest - timedelta(days=27)
    previous_start = latest - timedelta(days=55)
    previous_end = latest - timedelta(days=28)
    return recent_start.isoformat(), previous_start.isoformat(), previous_end.isoformat()


def _page_matches(url: str, slug: str, site_url: str) -> bool:
    parsed = urlparse(url)
    expected_host = urlparse(site_url).netloc.casefold()
    path = parsed.path.rstrip("/").casefold()
    return parsed.netloc.casefold() == expected_host and path.endswith("/" + slug.casefold())


def refresh_candidates(
    connection,
    *,
    site_url: str,
    as_of: date | None = None,
) -> list[dict]:
    """Return published articles ranked 8-20 or with falling 28-day clicks."""
    today = as_of or date.today()
    recent_start, previous_start, previous_end = _metric_period(today)
    articles = connection.execute(
        "SELECT id, title, slug FROM articles WHERE status = 'published'"
    ).fetchall()
    metrics = connection.execute(
        """SELECT url, query, clicks, impressions, position, date FROM metrics
           WHERE date >= ? AND date <= ?""",
        (previous_start, today.isoformat()),
    ).fetchall()
    candidates: list[dict] = []
    for article in articles:
        article_rows = [
            row for row in metrics
            if _page_matches(row["url"], article["slug"], site_url)
        ]
        page_totals = [row for row in article_rows if not row["query"]]
        rows = page_totals or article_rows
        if not rows:
            continue
        recent = [row for row in rows if row["date"] >= recent_start]
        previous = [
            row for row in rows
            if previous_start <= row["date"] <= previous_end
        ]
        if not recent:
            continue
        clicks_recent = sum(int(row["clicks"]) for row in recent)
        clicks_previous = sum(int(row["clicks"]) for row in previous)
        impressions = sum(int(row["impressions"]) for row in recent)
        position = (
            sum(float(row["position"]) * max(int(row["impressions"]), 1) for row in recent)
            / sum(max(int(row["impressions"]), 1) for row in recent)
        )
        position_opportunity = 8 <= position <= 20
        falling_clicks = clicks_previous > 0 and clicks_recent < clicks_previous
        if position_opportunity or falling_clicks:
            queries = sorted({
                row["query"] for row in article_rows
                if row["query"] and row["date"] >= recent_start
            })
            candidates.append({
                "article_id": article["id"],
                "title": article["title"],
                "slug": article["slug"],
                "url": next((row["url"] for row in rows if row["url"]), ""),
                "position": round(position, 2),
                "clicks_recent": clicks_recent,
                "clicks_previous": clicks_previous,
                "impressions_recent": impressions,
                "reason": (
                    "average position 8-20"
                    if position_opportunity else "clicks fell versus the prior 28 days"
                ),
                "queries": queries,
            })
    return sorted(candidates, key=lambda item: (item["position"], -item["impressions_recent"]))


def _brief_text(candidate: dict) -> str:
    query_lines = "\n".join(f"- {query}" for query in candidate["queries"]) or "- Review Search Console queries."
    return f"""Refresh brief for human review
Title: {candidate['title']}
URL: {candidate['url']}
Reason: {candidate['reason']}
Average recent position: {candidate['position']}
Clicks, recent 28 days: {candidate['clicks_recent']}
Clicks, previous 28 days: {candidate['clicks_previous']}

Sections to add
- Add a practical section addressing the reader's next decision related to the current topic.
- Explain relevant implementation or evaluation steps without inventing outcomes.

Facts to update
- Verify current platform, policy, and technical details against primary sources.
- Remove or qualify dated statements until checked by a human.

Internal links
- Find 3-4 currently relevant IsrainSols articles and link to the matching service page.

New FAQs
- Add concise questions based on the Search Console queries below.
- Do not add claims or answers that cannot be verified.

Search queries
{query_lines}

This is a brief only. Do not edit or overwrite the live WordPress post automatically.
"""


def queue_refreshes(
    connection,
    *,
    site_url: str,
    briefs_dir: Path | str,
    as_of: date | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Queue each qualifying post once and write a human-only refresh brief."""
    candidates = refresh_candidates(connection, site_url=site_url, as_of=as_of)
    ready: list[dict] = []
    for candidate in candidates:
        exists = connection.execute(
            """SELECT 1 FROM refresh_queue
               WHERE article_id = ? AND status IN ('queued', 'brief_created') LIMIT 1""",
            (candidate["article_id"],),
        ).fetchone()
        if exists:
            continue
        if dry_run:
            ready.append(candidate)
            continue
        folder = Path(briefs_dir)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = (as_of or date.today()).isoformat()
        path = folder / f"{stamp}_{candidate['slug']}_refresh.txt"
        path.write_text(_brief_text(candidate), encoding="utf-8")
        connection.execute(
            """INSERT INTO refresh_queue (article_id, reason, status, created_at)
               VALUES (?, ?, 'brief_created', ?)""",
            (candidate["article_id"], candidate["reason"], datetime.now().isoformat()),
        )
        connection.commit()
        candidate["brief_path"] = str(path)
        ready.append(candidate)
        logging.info("Refresh brief created for %s: %s", candidate["title"], path)
    return ready
