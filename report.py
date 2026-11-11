"""Weekly Search Console CSV/text reports and priority topic feedback."""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from config import NICHE_CLUSTERS
from topic_finder import matches_cluster

BRAND_QUERY_TERMS = (
    "israinsols.com",
    "israinsols",
    "israin sols",
    "israinsolutions",
    "israin solutions",
)


def _aggregate(rows: list, key: str) -> list[dict]:
    groups: dict[str, dict] = defaultdict(
        lambda: {"clicks": 0, "impressions": 0, "position_sum": 0.0, "weight": 0}
    )
    for row in rows:
        label = str(row[key] or "")
        if not label:
            continue
        group = groups[label]
        impressions = int(row["impressions"])
        group["clicks"] += int(row["clicks"])
        group["impressions"] += impressions
        weight = max(impressions, 1)
        group["position_sum"] += float(row["position"]) * weight
        group["weight"] += weight
    return sorted(
        [
            {
                key: label,
                "clicks": values["clicks"],
                "impressions": values["impressions"],
                "ctr": values["clicks"] / values["impressions"] if values["impressions"] else 0,
                "position": values["position_sum"] / values["weight"],
            }
            for label, values in groups.items()
        ],
        key=lambda item: (-item["clicks"], -item["impressions"], item[key]),
    )


def _position_bucket(position: float) -> str:
    if position <= 3:
        return "1-3"
    if position <= 10:
        return "4-10"
    if position <= 20:
        return "11-20"
    return "21+"


def _cluster_for_query(query: str) -> str | None:
    for cluster in NICHE_CLUSTERS:
        if matches_cluster(query, cluster):
            return cluster
    return None


def _is_brand_query(query: str) -> bool:
    normalized = " ".join(query.casefold().split())
    return any(term in normalized for term in BRAND_QUERY_TERMS)


def prioritize_winning_queries(connection, queries: list[dict]) -> int:
    """Raise relevant query topics to priority without reviving used/rejected topics."""
    changed = 0
    for row in queries:
        query = row["query"].strip()
        cluster = _cluster_for_query(query)
        if not cluster:
            continue
        score = 90.0 if 4 <= row["position"] <= 20 else 80.0
        existing = connection.execute(
            "SELECT id, status FROM topics WHERE lower(keyword) = lower(?)",
            (query,),
        ).fetchone()
        if existing:
            if existing["status"] in {"new", "queued"}:
                connection.execute(
                    "UPDATE topics SET score = MAX(score, ?), status = 'queued' WHERE id = ?",
                    (score, existing["id"]),
                )
                changed += 1
            continue
        connection.execute(
            """INSERT INTO topics
               (keyword, cluster, source, score, status, created_at)
               VALUES (?, ?, 'gsc_report', ?, 'queued', ?)""",
            (query, cluster, score, date.today().isoformat()),
        )
        changed += 1
    connection.commit()
    return changed


def build_report(
    connection,
    *,
    output_dir: Path | str,
    as_of: date | None = None,
    dry_run: bool = False,
) -> dict:
    """Build the last-28-day report with query/page rankings and quick wins."""
    today = as_of or date.today()
    start = today - timedelta(days=27)
    rows = connection.execute(
        """SELECT url, query, clicks, impressions, ctr, position, date
           FROM metrics WHERE date >= ? AND date <= ?""",
        (start.isoformat(), today.isoformat()),
    ).fetchall()
    records = [dict(row) for row in rows]
    query_records = [row for row in records if row["query"] and not row["url"]]
    page_records = [row for row in records if row["url"] and not row["query"]]
    if not query_records:
        query_records = [row for row in records if row["query"]]
    if not page_records:
        page_records = [row for row in records if row["url"]]
    query_rows = _aggregate(query_records, "query")
    page_rows = _aggregate(page_records, "url")
    brand_queries = [item for item in query_rows if _is_brand_query(item["query"])]
    buckets = {key: 0 for key in ("1-3", "4-10", "11-20", "21+")}
    for item in page_rows:
        buckets[_position_bucket(item["position"])] += 1
    quick_wins = [
        item for item in query_rows
        if (
            item["impressions"] > 0
            and 4 <= item["position"] <= 20
            and not _is_brand_query(item["query"])
        )
    ]
    quick_wins.sort(key=lambda item: (-item["impressions"], item["position"]))
    summary = [
        f"Search Console weekly report ({start.isoformat()} through {today.isoformat()})",
        "",
        "Top queries:",
    ]
    summary.extend(
        f"- {item['query']}: {item['clicks']} clicks, {item['impressions']} impressions, "
        f"position {item['position']:.1f}"
        for item in query_rows[:10]
    )
    summary.extend(["", "Branded queries:"])
    summary.extend(
        f"- {item['query']}: {item['clicks']} clicks, {item['impressions']} impressions, "
        f"{item['ctr']:.1%} CTR, position {item['position']:.1f}"
        for item in brand_queries
    )
    if not brand_queries:
        summary.append("- No branded queries recorded in this period.")
    summary.extend(["", "Top pages:"])
    summary.extend(
        f"- {item['url']}: {item['clicks']} clicks, {item['impressions']} impressions, "
        f"position {item['position']:.1f}"
        for item in page_rows[:10]
    )
    summary.extend(["", "Page position buckets:"])
    summary.extend(f"- {bucket}: {count} page(s)" for bucket, count in buckets.items())
    summary.extend(["", "Quick wins (positions 4-20 with impressions):"])
    summary.extend(
        f"- {item['query']}: position {item['position']:.1f}, {item['impressions']} impressions"
        for item in quick_wins[:10]
    )
    summary_text = "\n".join(summary) + "\n"
    if not dry_run:
        folder = Path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"gsc_weekly_{today.isoformat()}"
        csv_path = folder / f"{stem}.csv"
        text_path = folder / f"{stem}.txt"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=("type", "query", "url", "clicks", "impressions", "ctr", "position"),
            )
            writer.writeheader()
            for item in query_rows:
                writer.writerow({"type": "query", "query": item["query"], **item})
            for item in page_rows:
                writer.writerow({"type": "page", "url": item["url"], **item})
        text_path.write_text(summary_text, encoding="utf-8")
        summary_result = {"csv_path": str(csv_path), "text_path": str(text_path)}
        priority_count = prioritize_winning_queries(connection, quick_wins)
    else:
        summary_result = {"csv_path": None, "text_path": None}
        priority_count = 0
    return {
        **summary_result,
        "summary": summary_text,
        "queries": query_rows,
        "brand_queries": brand_queries,
        "pages": page_rows,
        "buckets": buckets,
        "quick_wins": quick_wins,
        "topics_prioritized": priority_count,
    }
