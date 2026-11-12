"""Analyze Search Console coverage and performance ZIP exports without API access."""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from urllib.parse import urlparse


def _read_csv(archive: zipfile.ZipFile, filename: str) -> list[dict[str, str]]:
    member = next(
        (name for name in archive.namelist() if Path(name).name.casefold() == filename.casefold()),
        None,
    )
    if member is None:
        raise ValueError(f"Search Console export is missing {filename}.")
    try:
        text = archive.read(member).decode("utf-8-sig")
    except (UnicodeDecodeError, KeyError) as exc:
        raise ValueError(f"Could not read {filename} from the Search Console ZIP.") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError(f"{filename} is empty or has no CSV header.")
    return [
        {str(key or "").strip(): str(value or "").strip() for key, value in row.items()}
        for row in reader
    ]


def _open_archive(path: Path | str) -> zipfile.ZipFile:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Search Console export was not found: {source}")
    try:
        return zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Search Console export is not a valid ZIP file: {source}") from exc


def _column(row: dict[str, str], name: str) -> str:
    return next(
        (value for key, value in row.items() if key.casefold() == name.casefold()),
        "",
    )


def _number(value: str, *, integer: bool = False) -> int | float:
    try:
        return int(float(value.replace(",", ""))) if integer else float(value.replace(",", ""))
    except (ValueError, AttributeError):
        return 0


def _percent(value: str) -> float:
    clean = value.strip().removesuffix("%")
    number = _number(clean)
    return number / 100 if "%" in value else number


def _rows_by_name(rows: list[dict[str, str]], name: str) -> dict[str, dict[str, str]]:
    return {
        _column(row, name): row
        for row in rows
        if _column(row, name)
    }


def analyze_exports(
    coverage_zip: Path | str,
    performance_zip: Path | str,
) -> dict:
    """Return a cautious summary and action plan from two GSC ZIP exports."""
    with _open_archive(coverage_zip) as archive:
        coverage_chart = _read_csv(archive, "Chart.csv")
        coverage_issues = _read_csv(archive, "Critical issues.csv")

    with _open_archive(performance_zip) as archive:
        queries = _read_csv(archive, "Queries.csv")
        pages = _read_csv(archive, "Pages.csv")
        countries = _read_csv(archive, "Countries.csv")
        filters = _read_csv(archive, "Filters.csv")

    coverage_chart.sort(key=lambda row: _column(row, "Date"))
    if not coverage_chart:
        raise ValueError("Coverage Chart.csv contains no dated rows.")

    first, latest = coverage_chart[0], coverage_chart[-1]
    indexed_start = _number(_column(first, "Indexed"), integer=True)
    indexed_end = _number(_column(latest, "Indexed"), integer=True)
    not_indexed_end = _number(_column(latest, "Not indexed"), integer=True)
    indexed_change = indexed_end - indexed_start
    indexed_change_pct = indexed_change / indexed_start if indexed_start else 0
    issue_counts = {
        _column(row, "Reason"): _number(_column(row, "Pages"), integer=True)
        for row in coverage_issues
        if _column(row, "Reason")
    }
    query_rows = [
        {
            "query": _column(row, "Top queries"),
            "clicks": _number(_column(row, "Clicks"), integer=True),
            "impressions": _number(_column(row, "Impressions"), integer=True),
            "ctr": _percent(_column(row, "CTR")),
            "position": _number(_column(row, "Position")),
        }
        for row in queries
        if _column(row, "Top queries")
    ]
    page_rows = [
        {
            "url": _column(row, "Top pages"),
            "clicks": _number(_column(row, "Clicks"), integer=True),
            "impressions": _number(_column(row, "Impressions"), integer=True),
            "ctr": _percent(_column(row, "CTR")),
            "position": _number(_column(row, "Position")),
        }
        for row in pages
        if _column(row, "Top pages")
    ]
    country_rows = [
        {
            "country": _column(row, "Country"),
            "impressions": _number(_column(row, "Impressions"), integer=True),
        }
        for row in countries
        if _column(row, "Country")
    ]
    time_range = next(
        (
            _column(row, "Value")
            for row in filters
            if _column(row, "Filter").casefold() == "time range"
        ),
        "Not specified in export",
    )

    crawled_count = issue_counts.get("Crawled - currently not indexed", 0)
    discovered_count = issue_counts.get("Discovered - currently not indexed", 0)
    actions = []
    if indexed_change < 0 or crawled_count:
        title = (
            f"Audit the {crawled_count} crawled-but-not-indexed URLs before scaling content"
            if crawled_count
            else "Investigate the indexing decline before scaling new content"
        )
        actions.append(
            (
                "P1",
                title,
                f"Indexed URLs changed from {indexed_start:,} to {indexed_end:,}; "
                f"{not_indexed_end:,} are currently reported as not indexed. Export affected "
                "URLs, group by page type, and inspect uniqueness, canonical, internal links, and "
                "usefulness. Improve or consolidate only after URL-level review; do not bulk-delete.",
            )
        )
    zero_click_visible_pages = [
        row for row in page_rows
        if row["impressions"] > 0 and row["clicks"] == 0 and row["position"] <= 10
    ]
    if zero_click_visible_pages:
        visible_examples = "; ".join(
            f"{urlparse(row['url']).path or '/'} ({row['impressions']} impressions, "
            f"position {row['position']:.1f})"
            for row in zero_click_visible_pages[:3]
        )
        snippet_detail = (
            f"Zero-click pages in this export include {visible_examples}. Compare 28- or "
            "90-day query/page data, then check whether each title and description accurately "
            "matches search intent before considering edits."
        )
    else:
        snippet_detail = (
            "Review the homepage and surfaced service pages for clear brand/service intent, "
            "distinct helpful content, accurate titles, and internal links. Use a 28- or 90-day "
            "window before making snippet changes."
        )
    actions.append(("P1", "Improve search-result relevance for visible pages", snippet_detail))
    if discovered_count:
        actions.append(
            (
                "P2",
                f"Review the {discovered_count} discovered-but-not-indexed URLs",
                "Check sitemap inclusion, internal discovery paths, and whether each URL "
                "deserves indexing. Avoid adding more thin or overlapping pages.",
            )
        )
    technical_counts = {
        reason: count
        for reason, count in issue_counts.items()
        if any(term in reason.casefold() for term in ("404", "noindex", "robots.txt", "duplicate"))
        and count
    }
    if technical_counts:
        actions.append(
            (
                "P2",
                "Resolve URL-specific technical and canonical issues",
                "Review individual 404, noindex, robots, and duplicate-canonical URL lists. "
                "Keep intentional noindex/robots rules; repair or redirect only URLs with "
                "a verified destination.",
            )
        )
    actions.append(
        (
            "P3",
            "Build brand visibility using genuine brand signals",
            "Keep IsrainSols / Israin Solutions consistent on the homepage and official "
            "profiles. Track branded queries separately; do not create pages just to target "
            "the domain spelling or manufacture searches/clicks.",
        )
    )

    return {
        "coverage": {
            "start_date": _column(first, "Date"),
            "end_date": _column(latest, "Date"),
            "indexed_start": indexed_start,
            "indexed_end": indexed_end,
            "indexed_change": indexed_change,
            "indexed_change_pct": indexed_change_pct,
            "not_indexed_end": not_indexed_end,
            "issues": issue_counts,
        },
        "performance": {
            "time_range": time_range,
            "queries": query_rows,
            "pages": page_rows,
            "countries": country_rows,
        },
        "actions": actions,
        "caveats": [
            "Performance export is a short snapshot; use Search Console's 28- or 90-day view for decisions.",
            "Query, page, and country tables are separate dimensions and must not be added together.",
            "Coverage issue counts identify groups, not affected URLs; export URL examples before changing pages.",
        ],
    }


def format_audit(audit: dict) -> str:
    """Render the audit as a concise, readable operator report."""
    coverage = audit["coverage"]
    performance = audit["performance"]
    change = coverage["indexed_change"]
    change_pct = coverage["indexed_change_pct"]
    direction = "+" if change > 0 else ""
    lines = [
        "ISRAINSOLS | SEARCH CONSOLE AUDIT",
        f"Coverage history: {coverage['start_date']} to {coverage['end_date']}",
        "",
        "INDEXING HEALTH",
        (
            f"Indexed URLs: {coverage['indexed_start']:,} -> {coverage['indexed_end']:,} "
            f"({direction}{change:,}; {change_pct:+.1%})"
        ),
        f"Not indexed at latest chart date: {coverage['not_indexed_end']:,}",
        "",
        "CURRENT COVERAGE ISSUES",
    ]
    lines.extend(
        f"- {reason}: {count:,}"
        for reason, count in sorted(audit["coverage"]["issues"].items(), key=lambda item: (-item[1], item[0]))
    )
    lines.extend(["", f"PERFORMANCE SNAPSHOT ({performance['time_range']})"])
    if performance["queries"]:
        lines.append("Queries:")
        lines.extend(
            f"- {row['query']}: {row['clicks']} clicks, {row['impressions']} impressions, "
            f"{row['ctr']:.1%} CTR, position {row['position']:.1f}"
            for row in performance["queries"]
        )
    else:
        lines.append("No query rows in export.")
    if performance["pages"]:
        lines.append("Pages with reported visibility:")
        lines.extend(
            f"- {row['url']}: {row['clicks']} clicks, {row['impressions']} impressions, "
            f"{row['ctr']:.1%} CTR, position {row['position']:.1f}"
            for row in performance["pages"]
        )
    if performance["countries"]:
        lines.append(
            "Countries: "
            + ", ".join(
                f"{row['country']} ({row['impressions']} "
                f"{'impression' if row['impressions'] == 1 else 'impressions'})"
                for row in performance["countries"]
            )
        )
    lines.extend(["", "RECOMMENDED ACTION PLAN"])
    lines.extend(
        f"{priority} | {title}\n  {detail}"
        for priority, title, detail in audit["actions"]
    )
    lines.extend(["", "INTERPRETATION NOTES"])
    lines.extend(f"- {note}" for note in audit["caveats"])
    country_impressions = sum(row["impressions"] for row in performance["countries"])
    pakistan_impressions = next(
        (
            row["impressions"]
            for row in performance["countries"]
            if row["country"].casefold() == "pakistan"
        ),
        0,
    )
    if country_impressions <= 20 and performance["countries"]:
        lines.append(
            f"- Country sample is very small ({pakistan_impressions} impression"
            f"{'' if pakistan_impressions == 1 else 's'} for Pakistan); "
            "use a longer date range before judging regional visibility."
        )
    return "\n".join(lines) + "\n"


def write_audit(
    coverage_zip: Path | str,
    performance_zip: Path | str,
    output_file: Path | str,
) -> Path:
    """Write an audit report without extracting or modifying the source archives."""
    audit = analyze_exports(coverage_zip, performance_zip)
    destination = Path(output_file)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(format_audit(audit), encoding="utf-8")
    return destination
