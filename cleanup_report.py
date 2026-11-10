"""Classify Search Console non-indexed URL exports for human-led cleanup."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from urllib.parse import urlparse


HIGH_OPPORTUNITY_SLUGS = {"google-business-profile-verification"}
GENERIC_SLUG_TERMS = {
    "blog", "post", "page", "category", "tag", "author", "archive",
    "uncategorized", "sample", "hello", "test",
}


def _row_value(row: dict[str, str], *names: str) -> str:
    lowered = {key.casefold().strip(): value for key, value in row.items() if key}
    for name in names:
        if lowered.get(name.casefold()):
            return lowered[name.casefold()].strip()
    return ""


def classify_url(row: dict[str, str]) -> tuple[str, str]:
    """Choose a review action based on GSC exclusion reason and URL shape."""
    url = _row_value(row, "url", "page", "page url")
    reason = _row_value(row, "reason", "issue", "status", "indexing status", "why not indexed")
    path = urlparse(url).path.strip("/").casefold()
    slug = path.rsplit("/", 1)[-1] if path else ""
    normalized_slug = re.sub(r"-(?:[2-9]|[1-9]\d+)$", "", slug)
    reason_text = reason.casefold()
    if normalized_slug in HIGH_OPPORTUNITY_SLUGS:
        return "improve", "Known high-impression page; improve the existing URL and avoid a duplicate."
    if any(term in reason_text for term in ("duplicate", "alternate page", "canonical")):
        return "merge+redirect", "Duplicate/canonical issue: compare content, choose one canonical page, and review a redirect."
    if (
        re.search(r"/(?:category|tag|author|page|search|attachment)/", f"/{path}/")
        or slug in GENERIC_SLUG_TERMS
        or re.fullmatch(r"(?:category|tag|author|page|attachment)-?.*", slug)
    ):
        return "noindex-delete", "Archive, taxonomy, attachment, or generic utility URL; review for noindex/removal."
    words = [word for word in re.split(r"[-_]+", slug) if word]
    if "crawled" in reason_text and "not indexed" in reason_text and (
        len(words) <= 2 or any(term in words for term in GENERIC_SLUG_TERMS)
    ):
        return "merge+redirect", "Crawled but not indexed with a thin/generic-looking slug; merge useful content or redirect."
    if any(term in reason_text for term in ("soft 404", "not found", "404")):
        return "merge+redirect", "Review whether useful content belongs on a relevant canonical URL before redirecting."
    return "improve", "Review content quality, uniqueness, internal links, and indexing eligibility before changes."


def create_cleanup_report(input_csv: Path | str, output_csv: Path | str | None = None) -> Path:
    """Read an exported non-indexed URL CSV and write a proposed cleanup plan."""
    source = Path(input_csv)
    if not source.is_file():
        raise FileNotFoundError(f"Search Console CSV was not found: {source}")
    destination = Path(output_csv) if output_csv else source.with_name("cleanup_plan.csv")
    with source.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or not any(
            name.casefold().strip() in {"url", "page", "page url"}
            for name in reader.fieldnames if name
        ):
            raise ValueError("CSV must include a URL or Page column.")
        rows = []
        for row in reader:
            action, rationale = classify_url(row)
            rows.append({
                "url": _row_value(row, "url", "page", "page url"),
                "reason": _row_value(row, "reason", "issue", "status", "indexing status", "why not indexed"),
                "action": action,
                "rationale": rationale,
            })
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=("url", "reason", "action", "rationale"))
        writer.writeheader()
        writer.writerows(rows)
    return destination
