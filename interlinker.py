"""Add relevant, deduplicated internal links to draft article HTML."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from config import NICHE_CLUSTERS, SITE_URL

SERVICE_PATHS = {
    "google-business-profile": "/seo/",
    "local-software-house": "/web-development/",
    "web-development-pakistan": "/web-development/",
    "mobile-app-development": "/app-development/",
    "online-reputation-and-seo": "/seo/",
    "web-development": "/web-development/",
    "app-development": "/app-development/",
    "ai-automation": "/ai-automation/",
    "seo": "/seo/",
    "social-media-marketing": "/social-media-marketing/",
    "design": "/graphic-design/",
}
SERVICE_LABELS = {
    "google-business-profile": "SEO services",
    "local-software-house": "software development services",
    "web-development-pakistan": "web development services",
    "mobile-app-development": "mobile app development services",
    "online-reputation-and-seo": "SEO services",
    "web-development": "web development services",
    "app-development": "app development services",
    "ai-automation": "AI automation services",
    "seo": "SEO services",
    "social-media-marketing": "social media marketing services",
    "design": "graphic design services",
}


@dataclass(frozen=True)
class InterlinkResult:
    html: str
    related_links: tuple[str, ...]
    service_link: str | None


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def _post_title(post: dict) -> str:
    title = post.get("title", "")
    if isinstance(title, dict):
        title = title.get("rendered", "")
    return html.unescape(_plain(str(title)))


def _post_url(post: dict) -> str:
    url = post.get("link") or post.get("url") or ""
    return str(url).strip()


def _is_internal(url: str, site_url: str) -> bool:
    parsed_url = urlparse(url)
    parsed_site = urlparse(site_url)
    return parsed_url.scheme in {"http", "https"} and parsed_url.netloc.casefold() == parsed_site.netloc.casefold()


def _relevance(title: str, keyword: str, cluster: str) -> int:
    words = set(re.findall(r"[a-z0-9]+", title.casefold()))
    target = set(re.findall(r"[a-z0-9]+", f"{keyword} {cluster.replace('-', ' ')}".casefold()))
    cluster_terms = set().union(
        *(set(re.findall(r"[a-z0-9]+", seed.casefold())) for seed in NICHE_CLUSTERS.get(cluster, []))
    )
    return len(words & target) * 3 + len(words & cluster_terms)


def add_internal_links(
    article_html: str,
    *,
    keyword: str,
    cluster: str,
    existing_posts: list[dict] | tuple[dict, ...] = (),
    site_url: str = SITE_URL,
    service_pages: dict[str, str] | None = None,
    max_related: int = 4,
) -> InterlinkResult:
    """Append up to four relevant existing posts and one service page, once per URL."""
    host = urlparse(site_url).netloc.casefold()
    used = {
        url.casefold()
        for url in re.findall(r"""<a\b[^>]*\bhref\s*=\s*["']([^"']+)["']""", article_html, re.I)
    }
    used = {url for url in used if urlparse(url).netloc.casefold() == host}
    ranked: list[tuple[int, str, str]] = []
    seen: set[str] = set(used)
    for post in existing_posts:
        title = _post_title(post)
        url = _post_url(post)
        key = url.casefold()
        if not title or not url or not _is_internal(url, site_url) or key in seen:
            continue
        seen.add(key)
        score = _relevance(title, keyword, cluster)
        if score:
            ranked.append((score, title, url))
    ranked.sort(key=lambda item: (-item[0], item[1].casefold()))
    selected = ranked[:max(0, min(4, max_related))]
    links: list[str] = []
    if selected:
        links.append("<h2>Related reading</h2><ul>")
        for _, title, url in selected:
            links.append(
                f'<li><a href="{html.escape(url, quote=True)}">{html.escape(title)}</a></li>'
            )
        links.append("</ul>")

    service_link: str | None = None
    service_url = (service_pages or {}).get(cluster)
    if service_url is None and cluster in SERVICE_PATHS:
        service_url = f"{site_url.rstrip('/')}{SERVICE_PATHS[cluster]}"
    if service_url and _is_internal(service_url, site_url) and service_url.casefold() not in seen:
        label = SERVICE_LABELS.get(cluster, f"{cluster.replace('-', ' ')} services")
        links.append(
            f'<p>Need expert support? Explore our '
            f'<a href="{html.escape(service_url, quote=True)}">{html.escape(label)}</a>.</p>'
        )
        service_link = service_url

    result_html = article_html
    if links:
        result_html = f"{article_html.rstrip()}\n{''.join(links)}\n"
    return InterlinkResult(result_html, tuple(url for _, _, url in selected), service_link)
