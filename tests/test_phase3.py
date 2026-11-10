from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from PIL import Image

from config import load_settings
from db import connect
from images import HEIGHT, WIDTH, create_featured_image
from interlinker import add_internal_links
from main import generate_drafts
from wp_client import DraftResult, WordPressClient


def _article(keyword: str) -> dict:
    paragraphs = []
    for index in range(45):
        paragraphs.append(
            f"<p>{keyword.title()} planning helps business owners make informed choices. "
            f"Review project needs and compare suitable options carefully in section {index}.</p>"
        )
    faq = "".join(
        f"<h3>How should a business consider {keyword} question {index}?</h3>"
        f"<p>Start by reviewing the business requirements for this question number {index}.</p>"
        for index in range(1, 5)
    )
    steps = (
        "<ol><li>Step 1: list the requirements.</li>"
        "<li>Step 2: compare suitable options.</li>"
        "<li>Step 3: confirm the next action.</li></ol>"
    )
    checklist = (
        "<h2>Checklist</h2><ul><li>Confirm the goal.</li>"
        "<li>Review available options.</li><li>Record the next step.</li></ul>"
    )
    body = (
        f"<p>{keyword.title()} planning gives business owners a starting point. "
        "Choose a useful approach based on requirements.</p>"
        + "".join(paragraphs)
        + steps + checklist + f"<h2>Frequently Asked Questions</h2>{faq}"
    )
    return {
        "title": keyword.title()[:60],
        "slug": keyword.replace(" ", "-") + "-guide",
        "meta_description": f"A practical educational guide to {keyword}.",
        "html": body,
        "tags": ["business guide"],
        "category": "web-development",
        "image_prompt": "A calm, clean planning illustration.",
        "verify_claims": ["Confirm any local requirements", "Review current platform options", "Check technical details"],
    }


class FakeWriter:
    def __init__(self):
        self.calls = []

    def generate(self, keyword, cluster, *, quality_feedback=None):
        self.calls.append((keyword, cluster, quality_feedback))
        return _article(keyword)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_AUTH_MODE", "file")
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "engine.sqlite3"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "engine.log"))
    monkeypatch.setenv("POSTS_PER_DAY", "2")
    return load_settings(env_file=tmp_path / "missing.env")


def test_interlinker_adds_relevant_unique_internal_and_service_links():
    posts = [
        {"title": {"rendered": "Website speed optimization"}, "link": "https://israinsols.com/blog/website-speed/"},
        {"title": "Ecommerce website planning", "link": "https://israinsols.com/blog/ecommerce/"},
        {"title": "Website speed optimization duplicate", "link": "https://israinsols.com/blog/website-speed/"},
        {"title": "Relevant external guide", "link": "https://other.example/website-guide/"},
        {"title": "Unrelated recipe", "link": "https://israinsols.com/blog/recipe/"},
    ]
    result = add_internal_links(
        '<p>Draft.</p><a href="https://israinsols.com/blog/website-speed/">Existing link</a>',
        keyword="website speed",
        cluster="web-development",
        existing_posts=posts,
    )
    assert len(result.related_links) == 1
    assert result.related_links[0] == "https://israinsols.com/blog/ecommerce/"
    assert result.service_link == "https://israinsols.com/web-development/"
    assert result.html.count("https://israinsols.com/blog/website-speed/") == 1
    assert result.html.count(result.service_link) == 1


def test_interlinker_does_not_duplicate_service_url_or_external_service():
    service = "https://israinsols.com/seo/"
    result = add_internal_links(
        f'<p><a href="{service}">SEO</a></p>',
        keyword="technical seo",
        cluster="seo",
        service_pages={"seo": "https://unrelated.example/"},
    )
    assert result.service_link is None
    assert result.html.count(service) == 1


def test_featured_image_dimensions_branding_and_alt(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAND_PRIMARY", "123456")
    image = create_featured_image("A useful guide for business owners", tmp_path / "featured.png")
    with Image.open(image.path) as rendered:
        assert rendered.size == (WIDTH, HEIGHT)
        assert rendered.getpixel((100, 100)) == (18, 52, 86)
    assert image.alt_text == "Featured image: A useful guide for business owners"


def test_featured_image_rejects_empty_title_and_invalid_brand_color(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="title"):
        create_featured_image("  ", tmp_path / "empty.png")
    monkeypatch.setenv("BRAND_ACCENT", "blue")
    with pytest.raises(ValueError, match="BRAND_ACCENT"):
        create_featured_image("An article", tmp_path / "invalid.png")


def test_generate_end_to_end_file_mode_draft_image_and_database(settings, caplog):
    caplog.set_level(logging.INFO)
    connection = connect(":memory:")
    writer = FakeWriter()
    client = WordPressClient(settings)
    results = generate_drafts(
        settings,
        connection,
        writer=writer,
        client=client,
        existing_posts=[
            {"title": "Website speed", "link": "https://israinsols.com/blog/website-speed/"}
        ],
        limit=1,
    )
    assert len(results) == 1
    result = results[0]
    html_path = Path(result["file_path"])
    json_path = html_path.with_suffix(".json")
    image_path = Path(result["image_path"])
    assert html_path.exists() and json_path.exists() and image_path.exists()
    metadata = json.loads(json_path.read_text(encoding="utf-8"))
    assert metadata["verify_claims"] == _article("website cost")["verify_claims"]
    assert metadata["featured_image"] == image_path.name
    assert "web-development/" in html_path.read_text(encoding="utf-8")
    with Image.open(image_path) as image:
        assert image.size == (1200, 630)
    article_row = connection.execute(
        "SELECT status, wp_post_id, word_count FROM articles WHERE status='draft'"
    ).fetchone()
    assert article_row["status"] == "draft"
    assert article_row["wp_post_id"] is None
    assert article_row["word_count"] >= settings.min_words
    assert connection.execute("SELECT COUNT(*) FROM topics WHERE status = 'used'").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM runs WHERE status = 'success'").fetchone()[0] >= 1
    assert f"{len(results)} draft ready:" in caplog.text
    assert "N drafts ready" not in caplog.text


def test_generate_daily_limit_and_dry_run_do_not_create_output(settings):
    from dataclasses import replace

    settings = replace(settings, posts_per_day=1)
    connection = connect(":memory:")
    writer = FakeWriter()
    dry_run = generate_drafts(settings, connection, writer=writer, dry_run=True, limit=1)
    assert dry_run == []
    assert writer.calls == []
    assert not settings.drafts_dir.exists()
    assert connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0
    client = WordPressClient(settings)
    ready = generate_drafts(settings, connection, writer=writer, client=client, limit=1)
    assert len(ready) == 1
    second = generate_drafts(settings, connection, writer=writer, client=client, limit=1)
    assert second == []
    third = generate_drafts(settings, connection, writer=writer, client=client, limit=1)
    assert third == []
    assert len(writer.calls) == 1


def test_generate_supports_wordpress_draft_client(settings):
    jwt_settings = settings.__class__(**{**settings.__dict__, "wp_auth_mode": "jwt"})
    class FakeWordPress:
        mode = "jwt"

        def create_draft(self, article):
            assert article["featured_image_path"]
            return DraftResult(post_id=42, status="draft")

    ready = generate_drafts(
        jwt_settings,
        connect(":memory:"),
        writer=FakeWriter(),
        client=FakeWordPress(),
        existing_posts=[],
        limit=1,
    )
    assert len(ready) == 1


def test_generate_records_wordpress_live_publication(settings):
    from dataclasses import replace

    publishing_settings = replace(settings, auto_publish=True)

    class FakePublisher:
        mode = "jwt"

        def create_draft(self, article):
            return DraftResult(post_id=77, status="publish")

    connection = connect(":memory:")
    ready = generate_drafts(
        publishing_settings,
        connection,
        writer=FakeWriter(),
        client=FakePublisher(),
        existing_posts=[],
        limit=1,
    )
    assert len(ready) == 1
    assert ready[0]["status"] == "publish"
    article = connection.execute(
        "SELECT status, wp_post_id, published_at FROM articles WHERE wp_post_id=77"
    ).fetchone()
    assert article["status"] == "published"
    assert article["wp_post_id"] == 77
    assert article["published_at"]
