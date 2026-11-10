from __future__ import annotations

import json

import pytest

from config import load_settings
from db import connect
from quality import evaluate_article, word_count
from topic_finder import (
    find_topics,
    generate_autosuggest_queries,
    is_gibberish,
    matches_cluster,
    score_topic,
)
from writer import GEMINI_URL, GeminiWriter, WriterError, _extract_json


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *, responses=None, suggestions=None):
        self.responses = list(responses or [])
        self.suggestions = suggestions or {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(["firefox", self.suggestions.get(kwargs["params"]["q"], [])])

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "engine.sqlite3"))
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "engine.log"))
    return load_settings(env_file=tmp_path / "missing.env")


def valid_article(words=90):
    body = "Website cost planning helps business owners make careful choices. "
    body += "Review requirements and compare useful options. " * words
    faq = "".join(
        f"<h3>Question {index}?</h3><p>Consider the answer carefully for topic {index}.</p>"
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
    return {
        "title": "Website cost planning for growing businesses",
        "html": f"<p>{body}</p>{steps}{checklist}<h2>Frequently Asked Questions</h2>{faq}",
    }


def test_topic_score_weights_and_already_covered_penalty():
    score = score_topic(
        "website cost", "web-development-pakistan", trend=50, gsc_gap=40,
        commercial_intent=100, freshness=80,
    )
    assert score == 68.0
    assert score_topic("website cost", "web-development-pakistan", already_covered=True) < score


def test_topic_filter_rejects_gibberish_and_out_of_cluster_keywords():
    assert matches_cluster("how much does a website development cost", "web-development-pakistan")
    assert not matches_cluster("celebrity gossip today", "web-development-pakistan")
    assert not matches_cluster("website cost", "unlisted-cluster")
    assert is_gibberish("!!!@@@###")
    assert is_gibberish("zzzzzzzzzz website")
    assert not is_gibberish("website cost for a small business")


def test_autosuggest_query_expansion_and_deduplication():
    queries = generate_autosuggest_queries(["website cost"])
    assert "website cost a" in queries and "website cost z" in queries
    assert "how website cost" in queries and "website cost near me" in queries
    assert len(queries) == len(set(queries))


def test_finder_deduplicates_and_persists_relevant_suggestions(settings):
    connection = connect(":memory:")
    connection.execute(
        "INSERT INTO topics (keyword, cluster, source, score, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("Google Business Profile verification for startups", "google-business-profile", "seed", 1, "2026-01-01"),
    )
    session = FakeSession(suggestions={
        "google business profile verification": [
            "Google Business Profile verification for startups",
            "Google Business Profile verification for small businesses",
            "Celebrity gossip website",
            "!!!",
        ]
    })
    class ZeroTrends:
        def score(self, keyword, region):
            return 0

    result = find_topics(
        settings,
        connection,
        existing_posts=[{"slug": "landing-page-design"}],
        trend_provider=ZeroTrends(),
        session=session,
        sleep=lambda _: None,
        max_requests=1,
        rate_limit_seconds=0,
    )
    assert [topic.keyword for topic in result] == ["Google Business Profile verification for small businesses"]
    assert connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 2


def test_finder_uses_region_trends_and_gsc_gap(settings):
    class Trends:
        def score(self, keyword, region):
            assert region == settings.region
            return 100

    session = FakeSession(suggestions={
        "google business profile verification": ["google business profile verification guide"]
    })
    found = find_topics(
        settings,
        connect(":memory:"),
        gsc_queries=[{"query": "google business profile verification guide", "impressions": 500, "position": 12}],
        trend_provider=Trends(),
        session=session,
        sleep=lambda _: None,
        max_requests=1,
    )
    assert found[0].score > 50


def test_finder_spreads_limited_requests_across_clusters(settings):
    session = FakeSession()
    find_topics(
        settings,
        connect(":memory:"),
        session=session,
        sleep=lambda _: None,
        max_requests=6,
        rate_limit_seconds=0,
    )
    queried = {call[2]["params"]["q"] for call in session.calls}
    assert queried == {
        "google business profile verification",
        "software house in hyderabad sindh",
        "website development cost pakistan",
        "mobile app development cost pakistan",
        "online reputation management small business",
        "google business profile verification a",
    }


def test_quality_passes_valid_article():
    article = valid_article()
    assert word_count(article["html"]) > 150
    result = evaluate_article(article, "website cost", min_words=150, max_words=700)
    assert result.passed
    assert not result.reasons


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"html": "<p>Too short.</p>"}, "Word count"),
        ({"html": "<p>Website cost is useful.</p>"}, "FAQ section"),
        ({"title": "A general guide"}, "missing from the title"),
        ({"html": "<p>Business choices matter.</p>"}, "first 100 words"),
        ({"html": "<p>[insert statistic] Website cost</p>"}, "Placeholder"),
    ],
)
def test_quality_rejects_invalid_article(change, reason):
    article = valid_article()
    article.update(change)
    result = evaluate_article(article, "website cost", min_words=10, max_words=10000)
    assert not result.passed
    assert any(reason in item for item in result.reasons)


def test_quality_duplicate_similarity_and_number_warnings():
    article = valid_article()
    article["html"] += "<p>Duplicate paragraph.</p><p>Duplicate paragraph.</p>"
    duplicate = evaluate_article(article, "website cost", min_words=1, max_words=10000)
    assert any("Duplicate paragraphs" in item for item in duplicate.reasons)
    original = valid_article()
    similar = evaluate_article(
        original, "website cost", min_words=1, max_words=10000,
        existing_posts=[{"title": original["title"], "content": original["html"]}],
    )
    assert any("too similar" in item for item in similar.reasons)

    numeric = valid_article()
    numeric["html"] = numeric["html"].replace(
        "Review requirements", "A 47% improvement is guaranteed. Review requirements"
    )
    result = evaluate_article(numeric, "website cost", min_words=1, max_words=10000)
    assert result.passed
    assert any("47%" in warning for warning in result.warnings)


def test_quality_rejects_banned_titles_unverified_currency_and_slug_families():
    article = valid_article()
    article["title"] = "Understanding website cost"
    article["slug"] = "website-cost-guide-3"
    article["html"] += "<p>A package costs PKR 25,000.</p>"
    result = evaluate_article(
        article,
        "website cost",
        min_words=1,
        max_words=10000,
        existing_posts=[{"title": "Another article", "slug": "website-cost-guide-2"}],
    )
    assert any("banned generic phrase" in reason for reason in result.reasons)
    assert any("Currency amounts" in reason for reason in result.reasons)
    assert any("slug family" in reason for reason in result.reasons)

    permitted = evaluate_article(
        article,
        "website cost",
        min_words=1,
        max_words=10000,
        business_facts="Price ranges: PKR 25,000",
    )
    assert not any("Currency amounts" in reason for reason in permitted.reasons)


def test_writer_strict_json_parser_checks_required_fields_and_claims():
    payload = {
        "title": "A website cost guide",
        "slug": "website-cost-guide",
        "meta_description": "Plan a website project.",
        "html": "<p>Article body</p>",
        "tags": ["web development"],
        "category": "web-development",
        "image_prompt": "Planning desk.",
        "verify_claims": ["Claim one", "Claim two", "Claim three"],
    }
    assert _extract_json(f"```json\n{json.dumps(payload)}\n```") == payload
    with pytest.raises(ValueError, match="missing fields"):
        _extract_json('{"title":"incomplete"}')
    with pytest.raises(ValueError, match="exactly three"):
        _extract_json(json.dumps({**payload, "verify_claims": []}))


def test_writer_retries_invalid_json_and_returns_valid_article(settings):
    article = {
        "title": "Website cost guide",
        "slug": "website-cost-guide",
        "meta_description": "Understand website cost planning.",
        "html": "<p>Educational content.</p>",
        "tags": ["website"],
        "category": "web-development",
        "image_prompt": "A practical planning illustration.",
        "verify_claims": ["Verify one", "Verify two", "Verify three"],
    }
    session = FakeSession(responses=[
        FakeResponse({"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}),
        FakeResponse({"candidates": [{"content": {"parts": [{"text": json.dumps(article)}]}}]}),
    ])
    result = GeminiWriter(settings, api_key="test-key", session=session).generate(
        "website cost", "web-development"
    )
    assert result["focus_keyword"] == "website cost"
    assert len(session.calls) == 2
    assert all(call[1] == GEMINI_URL for call in session.calls)


def test_writer_stops_after_bounded_retries_and_requires_api_key(settings):
    session = FakeSession(responses=[
        FakeResponse({"candidates": [{"content": {"parts": [{"text": "bad"}]}}]})
        for _ in range(2)
    ])
    writer = GeminiWriter(settings, api_key="test-key", session=session, max_retries=1)
    with pytest.raises(WriterError, match="after 2 attempts"):
        writer.generate("website cost", "web-development")
    assert len(session.calls) == 2
    with pytest.raises(WriterError, match="GEMINI_API_KEY"):
        GeminiWriter(settings, api_key="").generate("website cost", "web-development")
