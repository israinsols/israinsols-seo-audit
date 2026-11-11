from __future__ import annotations

import csv
import logging
import sqlite3
from datetime import date, timedelta

import pytest
import requests

from config import load_settings
from db import connect
from gsc_client import GSCClient, _parse_rows, upsert_metrics
import main as cli
from refresh import queue_refreshes, refresh_candidates
from report import build_report
from social import share_published


class FakeRequest:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeAnalytics:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def query(self, *, siteUrl, body):
        self.calls.append((siteUrl, body))
        return FakeRequest(self.response)


class FakeService:
    def __init__(self, response):
        self.analytics_client = FakeAnalytics(response)

    def searchanalytics(self):
        return self.analytics_client


class FakeWP:
    def __init__(self, posts):
        self.posts = posts
        self.calls = []

    def get_post(self, post_id):
        self.calls.append(post_id)
        return self.posts[post_id]


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "engine.sqlite3"))
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "engine.log"))
    monkeypatch.setenv("GSC_SERVICE_ACCOUNT_FILE", str(tmp_path / "missing-service.json"))
    monkeypatch.setenv("GSC_CLIENT_SECRET_FILE", str(tmp_path / "missing-client.json"))
    monkeypatch.setenv("GSC_TOKEN_FILE", str(tmp_path / "token.pickle"))
    return load_settings(env_file=tmp_path / "missing.env")


def _insert_metric(connection, url, query, day, *, clicks, impressions=100, position=12):
    connection.execute(
        "INSERT INTO metrics (url, query, clicks, impressions, ctr, position, date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (url, query, clicks, impressions, clicks / impressions if impressions else 0, position, day),
    )


def test_gsc_parses_daily_query_page_rows_and_sends_valid_request():
    response = {
        "rows": [{
            "keys": ["website cost", "https://israinsols.com/website-cost/", "2026-11-08"],
            "clicks": 8,
            "impressions": 120,
            "ctr": 8 / 120,
            "position": 9.5,
        }]
    }
    service = FakeService(response)
    client = GSCClient(service, "https://israinsols.com")
    rows = client.query_page_rows(7)
    assert rows == [{
        "query": "website cost",
        "date": "2026-11-08",
        "url": "https://israinsols.com/website-cost/",
        "clicks": 8,
        "impressions": 120,
        "ctr": 8 / 120,
        "position": 9.5,
    }]
    site_url, body = service.analytics_client.calls[0]
    assert site_url == "https://israinsols.com"
    assert body["dimensions"] == ["query", "page", "date"]
    assert body["rowLimit"] == 25000


def test_gsc_metrics_upsert_uses_date_url_query_key():
    connection = connect(":memory:")
    rows = [{
        "url": "https://israinsols.com/guide/",
        "query": "website cost",
        "date": "2026-11-01",
        "clicks": 3,
        "impressions": 30,
        "ctr": 0.1,
        "position": 12,
    }]
    assert upsert_metrics(connection, rows) == 1
    assert upsert_metrics(connection, [{**rows[0], "clicks": 9, "position": 8}]) == 1
    assert connection.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 1
    row = connection.execute("SELECT clicks, position FROM metrics").fetchone()
    assert tuple(row) == (9, 8.0)


def test_gsc_parser_skips_malformed_row_and_rejects_bad_days():
    assert _parse_rows({"rows": [{"keys": ["only-one"]}]}, ("query", "date")) == []
    with pytest.raises(ValueError, match="days"):
        from gsc_client import _date_range
        _date_range(0)


def test_gsc_missing_credentials_skips_with_clear_log(settings, caplog):
    with caplog.at_level(logging.INFO):
        assert GSCClient.from_settings(settings) is None
    assert "client_secret.json" in caplog.text
    assert "GSC_SERVICE_ACCOUNT_FILE" in caplog.text


def test_gsc_defaults_to_installed_app_oauth(settings, tmp_path, monkeypatch):
    import google.auth.credentials
    import google_auth_oauthlib.flow
    import googleapiclient.discovery

    client_secret = tmp_path / "client_secret.json"
    client_secret.write_text("{}", encoding="utf-8")
    token_file = tmp_path / "token.pickle"
    settings = settings.__class__(**{
        **settings.__dict__,
        "gsc_service_account_file": tmp_path / "not-configured.json",
        "gsc_client_secret_file": client_secret,
        "gsc_token_file": token_file,
    })
    monkeypatch.setenv("GSC_SERVICE_ACCOUNT_FILE", str(tmp_path / "not-configured.json"))
    monkeypatch.setenv("GSC_CLIENT_SECRET_FILE", str(client_secret))
    monkeypatch.setenv("GSC_TOKEN_FILE", str(token_file))
    calls = []

    class FakeFlow:
        def run_local_server(self, port):
            calls.append(("oauth", port))
            return google.auth.credentials.AnonymousCredentials()

    monkeypatch.setattr(
        google_auth_oauthlib.flow.InstalledAppFlow,
        "from_client_secrets_file",
        lambda path, scopes: (
            calls.append(("client_secret", path, scopes)) or FakeFlow()
        ),
    )
    monkeypatch.setattr(
        googleapiclient.discovery,
        "build",
        lambda *args, **kwargs: calls.append(("build", args, kwargs)) or object(),
    )
    client = GSCClient.from_settings(settings)
    assert client is not None
    assert calls[0][0] == "client_secret"
    assert calls[0][2] == ["https://www.googleapis.com/auth/webmasters.readonly"]
    assert calls[1][0] == "oauth"
    assert token_file.exists()


def test_refresh_queues_only_published_matching_rules_and_writes_brief(tmp_path):
    connection = connect(":memory:")
    connection.execute(
        "INSERT INTO articles (id, title, slug, status, word_count, created_at) "
        "VALUES (1, 'Website cost guide', 'website-cost-guide', 'published', 1000, '2026-01-01')"
    )
    connection.execute(
        "INSERT INTO articles (id, title, slug, status, word_count, created_at) "
        "VALUES (2, 'Draft guide', 'draft-guide', 'draft', 1000, '2026-01-01')"
    )
    end = date(2026, 11, 10)
    for offset in range(56):
        day = (end - timedelta(days=offset)).isoformat()
        if offset < 28:
            _insert_metric(connection, "https://israinsols.com/blog/website-cost-guide/", "website cost", day,
                           clicks=2, position=11)
        else:
            _insert_metric(connection, "https://israinsols.com/blog/website-cost-guide/", "website cost", day,
                           clicks=5, position=11)
    connection.commit()
    candidates = refresh_candidates(connection, site_url="https://israinsols.com", as_of=end)
    assert len(candidates) == 1
    assert candidates[0]["reason"] == "average position 8-20"
    assert candidates[0]["clicks_recent"] < candidates[0]["clicks_previous"]
    briefs = queue_refreshes(
        connection,
        site_url="https://israinsols.com",
        briefs_dir=tmp_path / "briefs",
        as_of=end,
    )
    assert len(briefs) == 1
    content = (tmp_path / "briefs" / "2026-11-10_website-cost-guide_refresh.txt").read_text()
    assert "Sections to add" in content
    assert "Facts to update" in content
    assert "Internal links" in content
    assert "New FAQs" in content
    assert "Do not edit or overwrite the live" in content
    assert connection.execute("SELECT status FROM articles WHERE id = 1").fetchone()[0] == "published"
    assert queue_refreshes(
        connection,
        site_url="https://israinsols.com",
        briefs_dir=tmp_path / "briefs",
        as_of=end,
    ) == []


def test_refresh_dry_run_does_not_create_queue_or_brief(tmp_path):
    connection = connect(":memory:")
    connection.execute(
        "INSERT INTO articles (id, title, slug, status, word_count, created_at) "
        "VALUES (1, 'Guide', 'guide', 'published', 900, '2026-01-01')"
    )
    _insert_metric(connection, "https://israinsols.com/guide/", "seo for small business",
                   "2026-11-10", clicks=1, position=9)
    connection.commit()
    result = queue_refreshes(
        connection,
        site_url="https://israinsols.com",
        briefs_dir=tmp_path / "briefs",
        as_of=date(2026, 11, 10),
        dry_run=True,
    )
    assert len(result) == 1
    assert not (tmp_path / "briefs").exists()
    assert connection.execute("SELECT COUNT(*) FROM refresh_queue").fetchone()[0] == 0


def test_report_writes_csv_text_buckets_and_prioritizes_topics(tmp_path):
    connection = connect(":memory:")
    today = date(2026, 11, 10)
    samples = [
        ("https://israinsols.com/a/", "technical seo", 10, 100, 2),
        ("https://israinsols.com/b/", "seo for small business", 7, 100, 8),
        ("https://israinsols.com/c/", "website cost", 2, 100, 15),
        ("https://israinsols.com/d/", "logo design", 1, 100, 25),
    ]
    for index, (url, query, clicks, impressions, position) in enumerate(samples):
        _insert_metric(connection, "", query, today.isoformat(), clicks=clicks,
                       impressions=impressions, position=position)
        _insert_metric(connection, url, "", today.isoformat(), clicks=clicks,
                       impressions=impressions, position=position)
    _insert_metric(connection, "", "Israin Solutions", today.isoformat(),
                   clicks=3, impressions=60, position=2)
    _insert_metric(connection, "", "IsrainSols", today.isoformat(),
                   clicks=2, impressions=40, position=3)
    _insert_metric(connection, "", "israinsols.com", today.isoformat(),
                   clicks=1, impressions=20, position=4)
    _insert_metric(connection, "", "IsrainSolutions", today.isoformat(),
                   clicks=1, impressions=10, position=5)
    connection.commit()
    report = build_report(connection, output_dir=tmp_path / "reports", as_of=today)
    assert report["buckets"] == {"1-3": 1, "4-10": 1, "11-20": 1, "21+": 1}
    assert len(report["quick_wins"]) == 2
    assert report["topics_prioritized"] == 2
    assert "ISRAINSOLS | ORGANIC SEARCH PERFORMANCE" in report["summary"]
    assert "TOP SEARCH QUERIES" in report["summary"]
    assert "TOP LANDING PAGES" in report["summary"]
    assert "CONTENT OPPORTUNITIES" in report["summary"]
    assert {item["query"] for item in report["brand_queries"]} == {
        "Israin Solutions",
        "IsrainSols",
        "israinsols.com",
        "IsrainSolutions",
    }
    assert report["brand_totals"] == {
        "clicks": 7,
        "impressions": 130,
        "ctr": 7 / 130,
        "position": 370 / 130,
    }
    assert (
        "Brand performance: 7 clicks | 130 impressions | 5.4% CTR | "
        "average position 2.8"
    ) in report["summary"]
    assert any(
        "Search query" in line and "Clicks" in line and "Avg. position" in line
        for line in report["summary"].splitlines()
    )
    assert any(
        "Israin Solutions" in line and "60" in line and "5.0%" in line
        for line in report["summary"].splitlines()
    )
    with open(report["csv_path"], newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))
    assert {row["type"] for row in rows} == {"query", "page"}
    topics = connection.execute(
        "SELECT keyword, score, status FROM topics ORDER BY keyword"
    ).fetchall()
    assert all(row["status"] == "queued" and row["score"] >= 80 for row in topics)


def test_report_dry_run_does_not_write_files_or_prioritize(tmp_path):
    connection = connect(":memory:")
    _insert_metric(connection, "https://israinsols.com/seo/", "technical seo",
                   "2026-11-10", clicks=1, position=8)
    connection.commit()
    report = build_report(
        connection,
        output_dir=tmp_path / "reports",
        as_of=date(2026, 11, 10),
        dry_run=True,
    )
    assert report["csv_path"] is None
    assert not (tmp_path / "reports").exists()
    assert connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0
    assert report["brand_queries"] == []
    assert report["brand_totals"] == {
        "clicks": 0,
        "impressions": 0,
        "ctr": 0,
        "position": 0,
    }
    assert "No branded queries were recorded in this reporting period." in report["summary"]


def test_report_professional_summary_handles_no_metrics(tmp_path):
    connection = connect(":memory:")
    report = build_report(
        connection,
        output_dir=tmp_path / "reports",
        as_of=date(2026, 11, 10),
        dry_run=True,
    )
    assert "Search queries tracked: 0 | Landing pages tracked: 0" in report["summary"]
    assert "No query data was available for this period." in report["summary"]
    assert "No landing-page data was available for this period." in report["summary"]
    assert "No non-brand queries in positions 4-20 were found." in report["summary"]


def test_social_sends_only_published_and_deduplicates(settings):
    settings = settings.__class__(**{
        **settings.__dict__,
        "social_telegram_enabled": True,
        "social_telegram_channel": "@israinsols",
        "telegram_bot_token": "bot-secret",
    })
    connection = connect(":memory:")
    connection.execute(
        "INSERT INTO articles (id, wp_post_id, title, slug, status, word_count, created_at) "
        "VALUES (1, 101, 'Published guide', 'guide', 'published', 950, '2026-11-01')"
    )
    connection.execute(
        "INSERT INTO articles (id, wp_post_id, title, slug, status, word_count, created_at) "
        "VALUES (2, 102, 'Draft guide', 'draft-guide', 'draft', 950, '2026-11-01')"
    )
    connection.commit()
    wp = FakeWP({
        101: {"id": 101, "status": "publish", "link": "https://israinsols.com/guide/",
              "title": {"rendered": "Published guide"}},
        102: {"id": 102, "status": "draft", "link": "https://israinsols.com/draft-guide/"},
    })
    class HttpResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "result": {"message_id": 88}}

    http = FakeHttp([HttpResponse()])
    first = share_published(connection, settings=settings, client=wp, session=http)
    second = share_published(connection, settings=settings, client=wp, session=http)
    assert first == [{"article_id": 1, "platform": "telegram", "status": "sent"}]
    assert second == []
    assert len(http.calls) == 1
    assert wp.calls == [101, 102, 101, 102]
    assert connection.execute("SELECT status FROM social_posts").fetchone()[0] == "sent"


def test_social_dry_run_and_disabled_platforms_do_not_send(settings):
    connection = connect(":memory:")
    connection.execute(
        "INSERT INTO articles (id, wp_post_id, title, slug, status, word_count, created_at) "
        "VALUES (1, 101, 'Published', 'guide', 'published', 900, '2026-11-01')"
    )
    connection.commit()
    wp = FakeWP({101: {
        "id": 101, "status": "publish", "link": "https://israinsols.com/guide/",
        "title": {"rendered": "Published"},
    }})
    http = FakeHttp([])
    assert share_published(connection, settings=settings, client=wp, session=http) == []
    enabled = settings.__class__(**{
        **settings.__dict__,
        "social_telegram_enabled": True,
        "social_telegram_channel": "@channel",
        "telegram_bot_token": "bot",
    })
    planned = share_published(
        connection, settings=enabled, client=wp, session=http, dry_run=True
    )
    assert planned[0]["status"] == "dry_run"
    assert http.calls == []
    assert connection.execute("SELECT COUNT(*) FROM social_posts").fetchone()[0] == 0


def test_social_detects_manual_wordpress_publication_before_sharing(settings):
    settings = settings.__class__(**{
        **settings.__dict__,
        "social_telegram_enabled": True,
        "social_telegram_channel": "@channel",
        "telegram_bot_token": "bot",
    })
    connection = connect(":memory:")
    connection.execute(
        "INSERT INTO articles (id, wp_post_id, title, slug, status, word_count, created_at) "
        "VALUES (1, 101, 'Draft title', 'guide', 'draft', 900, '2026-11-01')"
    )
    connection.commit()
    wp = FakeWP({101: {
        "id": 101, "status": "publish", "link": "https://israinsols.com/guide/",
        "title": {"rendered": "Published"},
    }})
    class HttpResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "result": {"message_id": 1}}

    result = share_published(
        connection,
        settings=settings,
        client=wp,
        session=FakeHttp([HttpResponse()]),
    )
    assert result[0]["status"] == "sent"
    article = connection.execute(
        "SELECT status, published_at FROM articles WHERE id = 1"
    ).fetchone()
    assert article["status"] == "published"
    assert article["published_at"]


def test_cli_all_runs_each_stage_in_isolation_and_records_outcomes(settings, monkeypatch, tmp_path):
    database_path = tmp_path / "cli.sqlite3"
    connection = connect(database_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _: None)
    monkeypatch.setattr(cli.db, "connect", lambda _: connection)
    calls = []

    def stage(name, raises=False):
        def action(*args, **kwargs):
            calls.append(name)
            if raises:
                raise RuntimeError("controlled failure")
            return True
        return action

    monkeypatch.setattr(cli, "run_research", stage("research", raises=True))
    monkeypatch.setattr(cli, "generate_drafts", stage("generate"))
    monkeypatch.setattr(cli, "_refresh_step", stage("refresh"))
    monkeypatch.setattr(cli, "_report_step", stage("report"))
    monkeypatch.setattr(cli, "_share_step", stage("share"))
    result = cli.main(["--all", "--dry-run"])
    assert result == 1
    assert calls == ["research", "generate", "refresh", "report", "share"]
    connection = connect(database_path)
    statuses = connection.execute(
        "SELECT step, status FROM runs ORDER BY id"
    ).fetchall()
    assert [(row["step"], row["status"]) for row in statuses] == [
        ("--research", "failed"),
        ("--generate", "success"),
        ("--refresh", "success"),
        ("--report", "success"),
        ("--share", "success"),
    ]
