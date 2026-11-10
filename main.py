"""SEO Traffic Engine command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import db
from config import KNOWN_EXISTING_PAGES, NICHE_CLUSTERS, PRIORITY_TOPICS, Settings, load_settings
from cleanup_report import classify_url, create_cleanup_report
from gsc_client import GSCClient
from images import create_featured_image
from notifier import TelegramNotifier
from interlinker import add_internal_links
from quality import evaluate_article, word_count
from refresh import queue_refreshes
from report import build_report
from social import share_published
from topic_finder import find_overlapping_page, find_topics, queue_overlap_refresh
from writer import GeminiWriter, WriterError, list_generate_content_models, load_business_facts
from wp_client import WordPressClient, WordPressError


def configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        force=True,
    )


def check_wp(client: WordPressClient) -> bool:
    """Test WordPress authentication and list five posts with actionable hints."""
    try:
        posts = client.check_connection()
    except WordPressError as exc:
        logging.error("WordPress check failed: %s", exc)
        if client.fallback_reason:
            logging.error(
                "Falling back to file mode. Fix hint: verify WP_URL, JWT plugin/header "
                "configuration, and the dedicated Editor credentials; or set WP_AUTH_MODE=file."
            )
        elif client.mode == "file":
            logging.info("File mode is configured; WordPress was not contacted.")
        elif exc.status_code == 401:
            logging.error(
                "Fix hint: verify the username/password and JWT plugin; ensure the account has Editor access."
            )
        elif exc.status_code in {403, 404}:
            logging.error(
                "Fix hint: verify the REST API is accessible, the JWT plugin route is enabled, "
                "and the Authorization header reaches WordPress."
            )
        else:
            logging.error(
                "Fix hint: verify WP_URL and network access, then check WordPress and JWT plugin logs."
            )
        return False
    logging.info("WordPress connection succeeded. Recent posts:")
    for post in posts[:5]:
        title = post.get("title", {})
        rendered = title.get("rendered", "") if isinstance(title, dict) else str(title)
        logging.info("- %s (ID %s)", rendered, post.get("id", "unknown"))
    return True


def check_gemini(settings: Settings, *, session=None) -> bool:
    """List Gemini models supporting generation without revealing the API key."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    try:
        models = list_generate_content_models(
            api_key,
            session=session,
            timeout=settings.request_timeout,
        )
    except WriterError as exc:
        logging.error("Gemini check failed: %s", exc)
        logging.error(
            "Fix hint: add a valid GEMINI_API_KEY to .env and confirm the key has "
            "Generative Language API access; never paste it into chat or logs."
        )
        return False
    if not models:
        logging.error("Gemini check found no models that support generateContent.")
        logging.error("Fix hint: verify API access and check the API's model availability.")
        return False
    logging.info("Gemini models supporting generateContent:")
    for model in models:
        logging.info("- %s", model)
    return True


def seed_topics(connection) -> None:
    """Insert the requested Pakistan-focused priority topics in their configured order."""
    timestamp = date.today().isoformat()
    for priority, (keyword, cluster) in enumerate(PRIORITY_TOPICS):
        score = 1000.0 - priority
        connection.execute(
            """INSERT OR IGNORE INTO topics
               (keyword, cluster, source, score, status, created_at)
               VALUES (?, ?, 'priority_seed', ?, 'queued', ?)""",
            (keyword, cluster, score, timestamp),
        )
        connection.execute(
            """UPDATE topics SET cluster = ?, score = MAX(score, ?),
               status = CASE WHEN status IN ('new', 'queued') THEN 'queued' ELSE status END
               WHERE lower(keyword) = lower(?)""",
            (cluster, score, keyword),
        )
    connection.commit()


def _article_slug(article: dict[str, Any]) -> str:
    value = str(article.get("slug") or article.get("title") or "article")
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "article"


def _focus_keyword(topic: str) -> str:
    value = re.sub(r"\([^)]*\)", "", topic)
    value = re.split(r"[?:]", value, maxsplit=1)[0].strip()
    value = re.sub(r"^(?:how to|do you need|what is|why does)\s+", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if len(value) <= 52:
        return value
    words = [word for word in value.split() if word.casefold() not in {
        "a", "an", "the", "to", "for", "in", "of", "and", "what", "it", "includes",
    }]
    return " ".join(words)[:52].rsplit(" ", 1)[0]


def _existing_posts(connection, site_url: str) -> list[dict[str, str]]:
    return [
        {
            "title": row["title"],
            "slug": row["slug"],
            "link": f"{site_url.rstrip('/')}/{row['slug'].strip('/')}/",
            "status": row["status"],
        }
        for row in connection.execute("SELECT title, slug, status FROM articles ORDER BY id DESC")
    ]


def generate_drafts(
    settings: Settings,
    connection,
    *,
    writer=None,
    client: WordPressClient | None = None,
    existing_posts: list[dict] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Generate, quality-check, interlink, and create drafts independently."""
    wp = client or WordPressClient(settings)
    if not dry_run:
        seed_topics(connection)
    today_count = connection.execute(
        "SELECT COUNT(*) FROM articles WHERE substr(created_at, 1, 10) = ?",
        (date.today().isoformat(),),
    ).fetchone()[0]
    available = max(0, settings.posts_per_day - int(today_count))
    requested = available if limit is None else min(available, max(0, limit))
    if requested == 0:
        logging.info("Daily draft limit reached or no drafts requested; nothing to generate.")
        return []

    pending = connection.execute(
        """SELECT t.id, t.keyword, t.cluster FROM topics t
           WHERE t.status IN ('new', 'queued')
             AND NOT EXISTS (SELECT 1 FROM articles a WHERE a.topic_id = t.id)
             AND NOT EXISTS (
                 SELECT 1 FROM refresh_queue rq
                 WHERE rq.topic_id = t.id AND rq.status IN ('queued', 'brief_created')
             )
           ORDER BY t.score DESC, t.id ASC""",
    ).fetchall()
    if dry_run and not pending:
        pending = [
            {"id": None, "keyword": keyword, "cluster": cluster}
            for keyword, cluster in PRIORITY_TOPICS
        ][:requested]
    if dry_run:
        logging.info("Dry run: would generate %s draft(s); no API calls or files are created.", len(pending))
        for topic in pending:
            logging.info("Would draft: %s [%s]", topic["keyword"], topic["cluster"])
        return []
    if writer is None:
        writer = GeminiWriter(settings)
    known_pages = list(KNOWN_EXISTING_PAGES)
    known_pages.extend(existing_posts or [])
    known_pages.extend(_existing_posts(connection, settings.site_url))
    if existing_posts is None and wp.mode != "file":
        try:
            known_pages.extend(wp.list_posts(100))
        except WordPressError as exc:
            logging.warning("Could not load WordPress posts for interlinking: %s", exc)
    known_posts = _existing_posts(connection, settings.site_url) + known_pages
    facts = load_business_facts(settings.business_facts_path)
    ready: list[dict[str, Any]] = []

    for topic in pending:
        if len(ready) >= requested:
            break
        title = str(topic["keyword"])
        step = f"generate:{topic['id']}"
        try:
            overlap = find_overlapping_page(topic["keyword"], known_pages)
            if overlap:
                queue_overlap_refresh(
                    connection,
                    keyword=topic["keyword"],
                    cluster=topic["cluster"],
                    page=overlap,
                )
                logging.info(
                    "Skipped duplicate topic %r; queued existing page for refresh: %s",
                    topic["keyword"],
                    overlap.get("link") or overlap.get("url") or overlap.get("slug"),
                )
                db.record_run(
                    connection,
                    step,
                    "success",
                    "Overlapping existing page queued for refresh; no duplicate draft created.",
                )
                continue
            focus_keyword = _focus_keyword(topic["keyword"])
            article = None
            result = None
            quality_feedback: list[str] = []
            for attempt in range(3):
                article = writer.generate(
                    focus_keyword,
                    topic["cluster"],
                    quality_feedback=quality_feedback,
                )
                result = evaluate_article(
                    article,
                    focus_keyword,
                    min_words=settings.min_words,
                    max_words=settings.max_words,
                    existing_posts=known_posts,
                    business_facts=facts,
                )
                if result.passed:
                    break
                quality_feedback = list(result.reasons)
                if attempt < 2:
                    logging.warning(
                        "Quality gate retry %s for %r: %s",
                        attempt + 1,
                        topic["keyword"],
                        "; ".join(result.reasons),
                    )
            if article is None or result is None:
                raise RuntimeError("Article writer returned no result.")
            if not result.passed:
                connection.execute(
                    "UPDATE topics SET status = 'rejected' WHERE id = ?",
                    (topic["id"],),
                )
                connection.commit()
                raise ValueError("Quality gate rejected draft: " + "; ".join(result.reasons))

            title = str(article["title"])
            article["focus_keyword"] = focus_keyword
            interlinked = add_internal_links(
                str(article["html"]),
                keyword=topic["keyword"],
                cluster=topic["cluster"],
                existing_posts=known_posts,
                site_url=settings.site_url,
            )
            article["html"] = interlinked.html
            slug = _article_slug(article)
            base = f"{date.today().isoformat()}_{slug}"
            image_path = settings.drafts_dir / f"{base}.png"
            suffix = 2
            while image_path.exists():
                image_path = settings.drafts_dir / f"{base}_{suffix}.png"
                suffix += 1
            image = create_featured_image(title, image_path)
            article["featured_image"] = image.path.name
            article["featured_image_path"] = str(image.path)
            article["image_alt"] = image.alt_text
            draft = wp.create_draft(article)
            if wp.mode == "file" and wp.fallback_reason:
                logging.error("WordPress rejected a request; saved draft in file mode. Reason: %s", wp.fallback_reason)
            connection.execute(
                """INSERT INTO articles
                   (topic_id, wp_post_id, title, slug, status, word_count, created_at)
                   VALUES (?, ?, ?, ?, 'draft', ?, ?)""",
                (
                    topic["id"],
                    draft.post_id,
                    title,
                    slug,
                    word_count(article["html"]),
                    date.today().isoformat(),
                ),
            )
            connection.execute("UPDATE topics SET status = 'used' WHERE id = ?", (topic["id"],))
            connection.commit()
            item = {
                "title": title,
                "verify_claims": article.get("verify_claims", []),
                "file_path": str(draft.file_path) if draft.file_path else None,
                "image_path": str(image.path),
                "word_count": result.word_count,
            }
            ready.append(item)
            logging.info("Draft ready: %s | verify claims: %s", title, item["verify_claims"])
            db.record_run(connection, step, "success", f"Draft created: {title}")
        except Exception as exc:
            logging.exception("Draft generation failed for topic %r", topic["keyword"])
            db.record_run(connection, step, "failed", str(exc))

    if ready:
        label = "draft" if len(ready) == 1 else "drafts"
        logging.info("%s %s ready:", len(ready), label)
        for item in ready:
            logging.info("- %s | verify_claims: %s", item["title"], item["verify_claims"])
        if settings.telegram_enabled:
            lines = [f"{len(ready)} draft(s) ready for human review:"]
            for item in ready:
                claims = "; ".join(item["verify_claims"])
                lines.append(f"- {item['title']} | verify claims: {claims}")
            message = "\n".join(lines)
            if len(message) > 3900:
                message = message[:3880].rsplit("\n", 1)[0] + "\n... (message shortened)"
            delivered = TelegramNotifier(settings).notify(message)
            db.record_run(
                connection,
                "telegram-notifier",
                "success" if delivered else "failed",
                "Draft summary delivered." if delivered else "Draft summary notification failed; see log.",
            )
    return ready


def run_research(settings: Settings, connection, *, dry_run: bool = False) -> int:
    if dry_run:
        logging.info(
            "Dry run: would research %s priority seed topics; no external requests or database changes.",
            len(PRIORITY_TOPICS),
        )
        for keyword, cluster in PRIORITY_TOPICS[:10]:
            logging.info("Would research: %s [%s]", keyword, cluster)
        return len(PRIORITY_TOPICS)
    seed_topics(connection)
    gsc = GSCClient.from_settings(settings)
    queries = gsc.top_queries(56) if gsc else None
    pages: list[dict] = list(KNOWN_EXISTING_PAGES)
    pages.extend(_existing_posts(connection, settings.site_url))
    gsc_pages = gsc.top_pages(56) if gsc else []
    wp = WordPressClient(settings)
    if wp.mode != "file":
        try:
            pages.extend(wp.list_posts(100))
        except WordPressError as exc:
            logging.warning("WordPress posts unavailable for topic dedupe: %s", exc)
    topics = find_topics(
        settings,
        connection,
        existing_posts=pages,
        gsc_queries=queries,
        persist=True,
        gsc_pages=gsc_pages,
    )
    logging.info("Topic research found %s new candidates.", len(topics))
    for topic in topics[:10]:
        logging.info("- %s (%.2f)", topic.keyword, topic.score)
    return len(topics)


def _refresh_step(settings: Settings, connection, *, dry_run: bool = False) -> int:
    gsc = GSCClient.from_settings(settings) if not dry_run else None
    if gsc:
        count = gsc.sync_metrics(connection, days=56)
        logging.info("Stored %s daily Search Console metric row(s).", count)
    elif not connection.execute("SELECT 1 FROM metrics LIMIT 1").fetchone():
        logging.info("Refresh skipped: no GSC access or stored Search Console metrics.")
        return 0
    briefs_dir = settings.drafts_dir.parent / "refresh_briefs"
    briefs = queue_refreshes(
        connection,
        site_url=settings.site_url,
        briefs_dir=briefs_dir,
        dry_run=dry_run,
    )
    logging.info("%s refresh brief(s) %s.", len(briefs), "would be created" if dry_run else "created")
    return len(briefs)


def _report_step(settings: Settings, connection, *, dry_run: bool = False) -> dict:
    gsc = GSCClient.from_settings(settings) if not dry_run else None
    if gsc:
        gsc.sync_metrics(connection, days=56)
    elif not connection.execute("SELECT 1 FROM metrics LIMIT 1").fetchone():
        logging.info("Report skipped: no GSC access or stored Search Console metrics.")
        return {}
    report = build_report(
        connection,
        output_dir=settings.drafts_dir.parent / "reports",
        dry_run=dry_run,
    )
    logging.info("%s", report["summary"])
    if report["csv_path"]:
        logging.info("CSV report: %s", report["csv_path"])
        logging.info("Topics prioritized: %s", report["topics_prioritized"])
    return report


def _share_step(settings: Settings, connection, *, dry_run: bool = False) -> list[dict]:
    if dry_run:
        logging.info("Dry run: social sharing preview skipped; WordPress and social APIs were not contacted.")
        return []
    outcomes = share_published(
        connection,
        settings=settings,
        client=WordPressClient(settings),
        dry_run=dry_run,
    )
    logging.info("Social share outcomes: %s", len(outcomes))
    return outcomes


def _cleanup_step(source: str, *, dry_run: bool = False) -> str:
    if dry_run:
        import csv

        with Path(source).open("r", newline="", encoding="utf-8-sig") as file:
            rows = list(csv.DictReader(file))
        for row in rows:
            action, rationale = classify_url(row)
            logging.info("Cleanup preview: %s | %s | %s", row, action, rationale)
        logging.info("Dry run: cleanup_plan.csv was not written.")
        return ""
    output = create_cleanup_report(source)
    logging.info("Human-review cleanup plan written to %s; no live URLs were changed.", output)
    return str(output)


def _run_isolated(connection, step: str, action) -> bool:
    """Run one CLI stage and record errors without preventing later stages."""
    try:
        result = action()
        db.record_run(connection, step, "success", f"Completed {step}.")
        return result is not False
    except Exception as exc:
        logging.exception("Step %s failed; continuing with other requested steps.", step)
        db.record_run(connection, step, "failed", f"{type(exc).__name__}: {exc}")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Organic SEO research and draft engine.")
    parser.add_argument("--check-wp", action="store_true", help="Check WordPress auth and list 5 posts.")
    parser.add_argument("--check-gemini", action="store_true", help="List Gemini models supporting generation.")
    parser.add_argument("--research", action="store_true", help="Research and score new topics.")
    parser.add_argument("--generate", action="store_true", help="Generate quality-checked drafts.")
    parser.add_argument("--refresh", action="store_true", help="Create briefs for published posts needing updates.")
    parser.add_argument("--report", action="store_true", help="Write a Search Console CSV and text report.")
    parser.add_argument("--share", action="store_true", help="Share eligible published posts on enabled platforms.")
    parser.add_argument("--cleanup-report", metavar="CSV", help="Classify a Search Console not-indexed CSV.")
    parser.add_argument("--all", action="store_true", help="Run research, draft, refresh, report, and share steps.")
    parser.add_argument("--dry-run", action="store_true", help="Preview selected topics without API calls or files.")
    parser.add_argument("--limit", type=int, help="Limit drafts generated in this run (daily cap still applies).")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit cannot be negative.")
    if not any((args.check_wp, args.check_gemini, args.research, args.generate,
                args.refresh, args.report, args.share, args.cleanup_report, args.all)):
        parser.print_help()
        return 0

    settings = load_settings()
    configure_logging(settings.log_path)
    connection = db.connect(settings.database_path)
    try:
        if args.check_wp:
            succeeded = check_wp(WordPressClient(settings))
            db.record_run(
                connection,
                "--check-wp",
                "success" if succeeded else "failed",
                "WordPress connection check succeeded." if succeeded else "See logs for exact error and fix hint.",
            )
            return 0 if succeeded else 1
        if args.check_gemini:
            succeeded = check_gemini(settings)
            db.record_run(
                connection,
                "--check-gemini",
                "success" if succeeded else "failed",
                "Gemini model check succeeded." if succeeded else "Gemini model check failed; see logs for fix hint.",
            )
            return 0 if succeeded else 1
        steps = []
        if args.all:
            steps = ["research", "generate", "refresh", "report", "share"]
        else:
            steps = [
                name for enabled, name in (
                    (args.research, "research"),
                    (args.generate, "generate"),
                    (args.refresh, "refresh"),
                    (args.report, "report"),
                    (args.share, "share"),
                ) if enabled
            ]
        if args.cleanup_report:
            steps.append("cleanup-report")
        success = True
        for step in steps:
            if step == "research":
                action = lambda: run_research(settings, connection, dry_run=args.dry_run)
            elif step == "generate":
                action = lambda: generate_drafts(
                    settings, connection, limit=args.limit, dry_run=args.dry_run
                )
            elif step == "refresh":
                action = lambda: _refresh_step(settings, connection, dry_run=args.dry_run)
            elif step == "report":
                action = lambda: _report_step(settings, connection, dry_run=args.dry_run)
            elif step == "cleanup-report":
                action = lambda: _cleanup_step(args.cleanup_report, dry_run=args.dry_run)
            else:
                action = lambda: _share_step(settings, connection, dry_run=args.dry_run)
            success = _run_isolated(connection, f"--{step}", action) and success
        return 0 if success else 1
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
