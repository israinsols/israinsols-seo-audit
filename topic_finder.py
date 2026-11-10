"""Policy-compliant topic discovery using Google suggestions and optional trends."""

from __future__ import annotations

import logging
import re
import string
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Iterable, Protocol

import requests

from config import NICHE_CLUSTERS, Settings

AUTOSUGGEST_URL = "https://suggestqueries.google.com/complete/search"
PREFIXES = ("how", "what", "best", "cost", "vs", "for", "near me")
STOP_WORDS = {
    "a", "an", "and", "are", "for", "how", "in", "is", "me", "of", "on",
    "or", "the", "to", "vs", "what", "with", "near", "best", "does", "can",
}
TOPIC_ALIASES = {
    "gmb": ("google", "business", "profile"),
    "gbp": ("google", "business", "profile"),
    "verification": ("verify",),
    "verified": ("verify",),
    "verifying": ("verify",),
    "suspended": ("suspend",),
    "hyderabad": ("hyderabad",),
}


class TrendProvider(Protocol):
    def score(self, keyword: str, region: str) -> float: ...


@dataclass(frozen=True)
class TopicCandidate:
    keyword: str
    cluster: str
    source: str
    score: float
    status: str = "new"


def _tokens(text: str) -> set[str]:
    tokens = {
        token for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in STOP_WORDS and len(token) > 1
    }
    expanded = set(tokens)
    for token in tokens:
        expanded.update(TOPIC_ALIASES.get(token, ()))
    return expanded


def topic_overlap_score(keyword: str, page: dict) -> float:
    """Estimate topic collision from titles/slugs, with acronym and inflection aliases."""
    candidate = _tokens(keyword)
    existing = _tokens(
        " ".join(
            str(page.get(key, ""))
            for key in ("title", "slug", "url")
        ).replace("-", " ").replace("/", " ")
    )
    overlap = len(candidate & existing)
    if overlap < 3:
        return 0.0
    return overlap / max(len(candidate), 1)


def find_overlapping_page(keyword: str, pages: Iterable[dict]) -> dict | None:
    """Return the strongest page overlap above the conservative collision threshold."""
    best: tuple[float, dict] | None = None
    for page in pages:
        if page.get("status") not in {None, "publish", "published"}:
            continue
        score = topic_overlap_score(keyword, page)
        if score >= 0.30 and (best is None or score > best[0]):
            best = (score, page)
    return best[1] if best else None


def queue_overlap_refresh(
    connection,
    *,
    keyword: str,
    cluster: str,
    page: dict,
) -> int | None:
    """Queue an existing published page for improvement instead of creating a duplicate."""
    connection.execute(
        """INSERT OR IGNORE INTO topics
           (keyword, cluster, source, score, status, created_at)
           VALUES (?, ?, 'overlap_refresh', 100, 'queued', ?)""",
        (keyword, cluster, datetime.now(timezone.utc).isoformat()),
    )
    topic = connection.execute(
        "SELECT id, status FROM topics WHERE lower(keyword) = lower(?)",
        (keyword,),
    ).fetchone()
    if not topic:
        return None
    topic_id = int(topic["id"])
    url = str(page.get("link") or page.get("url") or "").strip()
    title = str(page.get("title", "")).strip()
    if isinstance(page.get("title"), dict):
        title = str(page["title"].get("rendered", "")).strip()
    if not title:
        title = keyword
    slug = str(page.get("slug", "")).strip("/")
    if not slug and url:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    post_id = page.get("id")
    if not isinstance(post_id, int):
        post_id = page.get("wp_post_id")
    article = connection.execute(
        """SELECT id FROM articles
           WHERE (wp_post_id IS NOT NULL AND wp_post_id = ?)
              OR slug = ?
           ORDER BY CASE WHEN wp_post_id = ? THEN 0 ELSE 1 END LIMIT 1""",
        (post_id, slug, post_id),
    ).fetchone()
    if article:
        article_id = int(article["id"])
    else:
        article_cursor = connection.execute(
            """INSERT INTO articles
               (topic_id, wp_post_id, title, slug, status, word_count, created_at, published_at)
               VALUES (?, ?, ?, ?, 'published', 0, ?, ?)""",
            (
                topic_id,
                post_id,
                title,
                slug,
                date.today().isoformat(),
                date.today().isoformat(),
            ),
        )
        article_id = int(article_cursor.lastrowid)
    reason = (
        f"Topic overlaps existing published page {url or slug}; improve or refresh the existing page "
        "instead of creating another post."
    )
    connection.execute(
        """INSERT OR IGNORE INTO refresh_queue (article_id, topic_id, reason, status, created_at)
           VALUES (?, ?, ?, 'queued', ?)""",
        (article_id, topic_id, reason, datetime.now(timezone.utc).isoformat()),
    )
    if topic["status"] not in {"used", "rejected"}:
        connection.execute("UPDATE topics SET status = 'queued', score = MAX(score, 100) WHERE id = ?", (topic_id,))
    connection.commit()
    return article_id


def is_gibberish(keyword: str) -> bool:
    """Reject malformed or noisy autocomplete responses."""
    value = keyword.strip()
    if not value or len(value) > 100 or not re.search(r"[a-z]", value, re.I):
        return True
    if re.search(r"(.)\1{4,}", value.lower()):
        return True
    words = re.findall(r"[a-z]+", value.lower())
    return len(words) > 12 or sum(char.isalpha() for char in value) / len(value) < 0.45


def matches_cluster(keyword: str, cluster: str) -> bool:
    """Only accept terms sharing topical vocabulary with the curated cluster seeds."""
    if cluster not in NICHE_CLUSTERS or is_gibberish(keyword):
        return False
    seed_tokens = set().union(*(_tokens(seed) for seed in NICHE_CLUSTERS[cluster]))
    return len(_tokens(keyword) & seed_tokens) >= 2


def generate_autosuggest_queries(seeds: Iterable[str]) -> list[str]:
    """Generate each seed, alphabet suffixes, and configured intent prefixes."""
    queries: list[str] = []
    seen: set[str] = set()

    def add(query: str) -> None:
        normalized = " ".join(query.split())
        if normalized.casefold() not in seen:
            queries.append(normalized)
            seen.add(normalized.casefold())

    for seed in seeds:
        add(seed)
        for letter in string.ascii_lowercase:
            add(f"{seed} {letter}")
        for prefix in PREFIXES:
            add(f"{seed} {prefix}" if prefix in {"vs", "near me"} else f"{prefix} {seed}")
    return queries


def autosuggest(
    query: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 10,
) -> list[str]:
    """Fetch one public autocomplete response."""
    response = (session or requests.Session()).get(
        AUTOSUGGEST_URL,
        params={"client": "firefox", "q": query},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
        raise ValueError("Google Autosuggest returned an unexpected response.")
    return [item for item in payload[1] if isinstance(item, str)]


def _bounded(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _trend_score(keyword: str, region: str, provider: TrendProvider | None) -> float:
    if provider is not None:
        try:
            return _bounded(provider.score(keyword, region))
        except Exception as exc:
            logging.warning("Trend lookup failed; using zero: %s", exc)
            return 0.0
    try:
        from pytrends.request import TrendReq
    except ImportError:
        logging.warning("pytrends unavailable; trend scores will be zero.")
        return 0.0
    try:
        trends = TrendReq(hl="en-GB", tz=0)
        trends.build_payload([keyword], geo=region)
        values = trends.interest_over_time()
        return _bounded(float(values[keyword].mean())) if not values.empty else 0.0
    except Exception as exc:
        logging.warning("Google Trends lookup failed; using zero: %s", exc)
        return 0.0


def _commercial_intent(keyword: str) -> float:
    return 100.0 if _tokens(keyword) & {
        "cost", "price", "pricing", "hire", "agency", "service", "company", "best"
    } else 0.0


def _gsc_gap(keyword: str, rows: Iterable[dict] | None) -> float:
    if rows is None:
        return 0.0
    matches = [
        row for row in rows
        if keyword.casefold() in str(row.get("query", "")).casefold()
        or str(row.get("query", "")).casefold() in keyword.casefold()
    ]
    if not matches:
        return 0.0
    impressions = max(float(row.get("impressions", 0) or 0) for row in matches)
    position = min(float(row.get("position", 0) or 0) for row in matches)
    return (_bounded(impressions / 10) + _bounded((20 - position) * 5)) / 2


def score_topic(
    keyword: str,
    cluster: str,
    *,
    trend: float = 0,
    gsc_gap: float = 0,
    commercial_intent: float | None = None,
    freshness: float = 100,
    already_covered: bool = False,
) -> float:
    """Score 0-100 trend, Search Console gap, intent, niche, and freshness signals."""
    niche_match = 100.0 if matches_cluster(keyword, cluster) else 0.0
    intent = _commercial_intent(keyword) if commercial_intent is None else _bounded(commercial_intent)
    score = (
        _bounded(trend) * 0.30
        + _bounded(gsc_gap) * 0.25
        + intent * 0.20
        + niche_match * 0.15
        + _bounded(freshness) * 0.10
    )
    if already_covered:
        score -= 20
    return round(max(0.0, score), 2)


def find_topics(
    settings: Settings,
    connection,
    *,
    existing_posts: Iterable[dict] = (),
    gsc_queries: Iterable[dict] | None = None,
    trend_provider: TrendProvider | None = None,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_requests: int = 120,
    rate_limit_seconds: float = 1.0,
    persist: bool = True,
    gsc_pages: Iterable[dict] | None = None,
) -> list[TopicCandidate]:
    """Discover relevant suggestions, dedupe, score, and persist them as new topics."""
    existing_pages = list(existing_posts)
    known = {
        row[0].strip().casefold()
        for row in connection.execute("SELECT keyword FROM topics")
    }
    known.update(
        str(post.get("slug", "")).replace("-", " ").strip().casefold()
        for post in existing_pages
    )
    covered_pages = list(existing_pages)
    if gsc_pages:
        for item in gsc_pages:
            url = str(item.get("url", ""))
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            covered_pages.append({
                "url": url,
                "slug": slug,
                "title": slug.replace("-", " "),
                "status": "publish",
            })
    found: dict[str, TopicCandidate] = {}
    http = session or requests.Session()
    request_count = 0
    timestamp = datetime.now(timezone.utc).isoformat()
    work = deque(
        (cluster, iter(generate_autosuggest_queries(seeds)))
        for cluster, seeds in NICHE_CLUSTERS.items()
    )
    while work and request_count < max_requests:
        cluster, queries = work.popleft()
        try:
            query = next(queries)
            work.append((cluster, queries))
        except StopIteration:
            continue
        if request_count:
            sleep(rate_limit_seconds)
        request_count += 1
        try:
            suggestions = autosuggest(query, session=http, timeout=settings.request_timeout)
        except (requests.RequestException, ValueError) as exc:
            logging.warning("Autosuggest failed for query %r: %s", query, exc)
            continue
        for suggestion in suggestions:
            keyword = " ".join(suggestion.split()).strip()
            key = keyword.casefold()
            overlap = find_overlapping_page(keyword, covered_pages)
            if key in found or key in known or not matches_cluster(keyword, cluster):
                continue
            if overlap:
                if persist:
                    queue_overlap_refresh(
                        connection,
                        keyword=keyword,
                        cluster=cluster,
                        page=overlap,
                    )
                known.add(key)
                continue
            score = score_topic(
                keyword,
                cluster,
                trend=_trend_score(keyword, settings.region, trend_provider),
                gsc_gap=_gsc_gap(keyword, gsc_queries),
            )
            found[key] = TopicCandidate(keyword, cluster, "google_autosuggest", score)
            if persist:
                connection.execute(
                    """INSERT OR IGNORE INTO topics
                       (keyword, cluster, source, score, status, created_at)
                       VALUES (?, ?, ?, ?, 'new', ?)""",
                    (keyword, cluster, "google_autosuggest", score, timestamp),
                )
    if persist:
        connection.commit()
    return sorted(found.values(), key=lambda item: item.score, reverse=True)
