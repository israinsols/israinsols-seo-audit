"""Published-only social sharing with platform flags and database deduplication."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from wp_client import WordPressClient, WordPressError


def share_pinterest(post: dict[str, Any]) -> bool:
    """TODO: add a permitted Pinterest API integration when an approved free API is available."""
    logging.info("Pinterest sharing is a stub and remains disabled.")
    return False


def share_facebook(post: dict[str, Any]) -> bool:
    """TODO: implement through the official Meta Graph API only, if free access is available."""
    logging.info("Facebook sharing is a stub and remains disabled.")
    return False


def share_x(post: dict[str, Any]) -> bool:
    """TODO: implement only through an authorized official X API plan."""
    logging.info("X sharing is a stub and remains disabled.")
    return False


def _send_telegram(
    *,
    token: str,
    channel: str,
    text: str,
    session: requests.Session,
    timeout: float,
) -> str:
    if not token or not channel:
        raise ValueError("Telegram channel sharing needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID.")
    response = session.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": channel, "text": text, "disable_web_page_preview": False},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("Telegram did not accept the channel post.")
    result = payload.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    return str(message_id or "")


def share_published(
    connection,
    *,
    settings,
    client: WordPressClient | None = None,
    session: requests.Session | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Share eligible published posts once; drafts and unknown WP statuses are skipped."""
    platforms = {
        "telegram": bool(settings.social_telegram_enabled),
        "pinterest": bool(settings.pinterest_enabled),
        "facebook": bool(settings.facebook_enabled),
        "x": bool(settings.x_enabled),
    }
    active = [platform for platform, enabled in platforms.items() if enabled]
    if not active:
        logging.info("Social sharing skipped: all platform flags are OFF.")
        return []
    wp = client or WordPressClient(settings)
    http = session or requests.Session()
    articles = connection.execute(
        """SELECT id, wp_post_id, title FROM articles
           WHERE status IN ('draft', 'reviewed', 'published') AND wp_post_id IS NOT NULL
           ORDER BY id"""
    ).fetchall()
    outcomes: list[dict[str, Any]] = []
    for article in articles:
        try:
            post = wp.get_post(int(article["wp_post_id"]))
        except WordPressError as exc:
            logging.error("Could not verify published status for article %s: %s", article["id"], exc)
            continue
        if post.get("status") != "publish":
            continue
        if not dry_run:
            connection.execute(
                """UPDATE articles
                   SET status = 'published',
                       published_at = COALESCE(published_at, ?)
                   WHERE id = ?""",
                (str(post.get("date_gmt") or datetime.now(timezone.utc).isoformat()), article["id"]),
            )
            connection.commit()
        url = str(post.get("link", "")).strip()
        if not url:
            logging.warning("Published WordPress post %s has no link; skipping share.", article["wp_post_id"])
            continue
        title_payload = post.get("title", {})
        title = (
            str(title_payload.get("rendered", ""))
            if isinstance(title_payload, dict) else str(title_payload or article["title"])
        )
        for platform in active:
            exists = connection.execute(
                "SELECT 1 FROM social_posts WHERE article_id = ? AND platform = ?",
                (article["id"], platform),
            ).fetchone()
            if exists:
                continue
            if dry_run:
                outcomes.append({"article_id": article["id"], "platform": platform, "status": "dry_run"})
                logging.info("Would share published post %s to %s.", article["wp_post_id"], platform)
                continue
            timestamp = datetime.now(timezone.utc).isoformat()
            cursor = connection.execute(
                """INSERT INTO social_posts
                   (article_id, platform, status, created_at)
                   VALUES (?, ?, 'queued', ?)""",
                (article["id"], platform, timestamp),
            )
            connection.commit()
            record_id = cursor.lastrowid
            try:
                if platform == "telegram":
                    external_id = _send_telegram(
                        token=settings.telegram_bot_token,
                        channel=settings.social_telegram_channel,
                        text=f"{title}\n{url}",
                        session=http,
                        timeout=settings.request_timeout,
                    )
                else:
                    if platform == "pinterest":
                        share_pinterest(post)
                    elif platform == "facebook":
                        share_facebook(post)
                    else:
                        share_x(post)
                    external_id = None
                status = "sent" if platform == "telegram" else "unsupported"
                connection.execute(
                    "UPDATE social_posts SET status = ?, external_id = ? WHERE id = ?",
                    (status, external_id or None, record_id),
                )
                connection.commit()
                outcomes.append({"article_id": article["id"], "platform": platform, "status": status})
            except Exception as exc:
                logging.error(
                    "Social share failed for article %s on %s (%s).",
                    article["id"],
                    platform,
                    type(exc).__name__,
                )
                connection.execute(
                    "UPDATE social_posts SET status = 'failed' WHERE id = ?",
                    (record_id,),
                )
                connection.commit()
                outcomes.append({"article_id": article["id"], "platform": platform, "status": "failed"})
    return outcomes
