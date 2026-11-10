from __future__ import annotations

import logging
from dataclasses import replace

import pytest
import requests

from config import load_settings
from indexing import INDEXNOW_ENDPOINT, INDEXNOW_KEY_FILE_INSTRUCTIONS, notify_published_url
from notifier import TelegramNotifier
from wp_client import WordPressClient, WordPressError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.reason = "OK"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_AUTH_MODE", "jwt")
    monkeypatch.setenv("WP_USER", "editor")
    monkeypatch.setenv("WP_PASSWORD", "wp-test-password")
    monkeypatch.setenv("WP_APP_PASSWORD", "app-test-password")
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "engine.sqlite3"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "engine.log"))
    return load_settings(env_file=tmp_path / "missing.env")


def test_jwt_uploads_featured_image_alt_taxonomies_and_creates_draft(settings, tmp_path):
    image = tmp_path / "featured.png"
    image.write_bytes(b"fake png bytes")
    session = FakeSession([
        FakeResponse(payload={"token": "jwt-token"}),
        FakeResponse(payload=[{"id": 3, "name": "web-development"}]),
        FakeResponse(payload=[]),
        FakeResponse(payload={"id": 8, "name": "website guide"}),
        FakeResponse(payload={"id": 12, "source_url": "https://israinsols.com/image.png"}),
        FakeResponse(payload={"id": 12}),
        FakeResponse(payload={"id": 50, "status": "draft"}),
    ])
    client = WordPressClient(settings, session=session)
    result = client.create_draft({
        "title": "Website cost guide",
        "slug": "website-cost-guide",
        "html": "<p>Educational article.</p>",
        "meta_description": "Plan a website project.",
        "focus_keyword": "website cost",
        "category": "web-development",
        "tags": ["website guide"],
        "featured_image_path": str(image),
        "image_alt": "Website planning illustration",
    })
    assert result.post_id == 50
    upload = next(call for call in session.calls if call[1].endswith("/wp/v2/media"))
    assert upload[2]["data"] == b"fake png bytes"
    assert upload[2]["headers"]["Content-Type"] == "image/png"
    assert upload[2]["headers"]["Content-Disposition"] == 'attachment; filename="featured.png"'
    alt_update = next(call for call in session.calls if "/wp/v2/media/12" in call[1])
    assert alt_update[2]["json"] == {"alt_text": "Website planning illustration"}
    post = next(call for call in session.calls if call[1].endswith("/wp/v2/posts"))
    payload = post[2]["json"]
    assert payload["status"] == "draft"
    assert payload["featured_media"] == 12
    assert payload["categories"] == [3]
    assert payload["tags"] == [8]
    assert payload["meta"]["rank_math_focus_keyword"] == "website cost"


def test_auto_publish_sends_live_status_and_requires_wordpress_confirmation(settings):
    publishing_settings = replace(settings, auto_publish=True)
    session = FakeSession([
        FakeResponse(payload={"token": "jwt-token"}),
        FakeResponse(payload={"id": 77, "status": "publish"}),
    ])
    client = WordPressClient(publishing_settings, session=session)
    result = client.create_draft({
        "title": "Website planning",
        "slug": "website-planning",
        "html": "<p>Educational article.</p>",
    })
    post_request = next(call for call in session.calls if call[1].endswith("/wp/v2/posts"))
    assert post_request[2]["json"]["status"] == "publish"
    assert result.post_id == 77
    assert result.status == "publish"


def test_auto_publish_refuses_file_mode_and_unconfirmed_wordpress_status(settings):
    publishing_settings = replace(settings, auto_publish=True, wp_auth_mode="file")
    client = WordPressClient(publishing_settings)
    with pytest.raises(WordPressError, match="file mode cannot publish live posts"):
        client.create_draft({"title": "No fallback publishing"})

    session = FakeSession([
        FakeResponse(payload={"token": "jwt-token"}),
        FakeResponse(payload={"id": 78, "status": "draft"}),
    ])
    client = WordPressClient(replace(settings, auto_publish=True), session=session)
    with pytest.raises(WordPressError, match="expected 'publish'"):
        client.create_draft({"title": "Status must be confirmed"})


def test_upload_auth_failure_falls_back_to_local_draft(settings, tmp_path):
    image = tmp_path / "article.png"
    image.write_bytes(b"png")
    session = FakeSession([
        FakeResponse(payload={"token": "jwt-token"}),
        FakeResponse(status_code=404, text="Media route missing"),
    ])
    client = WordPressClient(settings, session=session)
    result = client.create_draft({
        "title": "Fallback guide",
        "html": "<p>Draft body</p>",
        "featured_image_path": str(image),
        "image_alt": "A guide image",
    })
    assert client.mode == "file"
    assert "Media route missing" in client.fallback_reason
    assert result.file_path and result.file_path.exists()
    metadata = result.file_path.with_suffix(".json").read_text(encoding="utf-8")
    assert "article.png" in metadata
    assert (settings.drafts_dir / "article.png").read_bytes() == b"png"


def test_application_password_auth_can_upload_draft_without_jwt(settings):
    settings = settings.__class__(**{**settings.__dict__, "wp_auth_mode": "app_password"})
    session = FakeSession([FakeResponse(payload={"id": 71})])
    result = WordPressClient(settings, session=session).create_draft({
        "title": "Basic auth draft",
        "html": "<p>Content</p>",
        "category": "",
        "tags": [],
    })
    assert result.post_id == 71
    assert len(session.calls) == 1
    assert session.calls[0][2]["auth"].username == "editor"


def test_telegram_is_off_by_default_and_sends_when_enabled(settings):
    disabled_session = FakeSession([])
    assert not TelegramNotifier(settings, session=disabled_session).notify("Drafts ready")
    assert disabled_session.calls == []

    enabled = settings.__class__(**{
        **settings.__dict__,
        "telegram_enabled": True,
        "telegram_bot_token": "telegram-secret",
        "telegram_chat_id": "chat-1",
    })
    session = FakeSession([FakeResponse(payload={"ok": True})])
    assert TelegramNotifier(enabled, session=session).notify("Drafts ready")
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert "telegram-secret" in url
    assert kwargs["json"]["chat_id"] == "chat-1"


def test_telegram_missing_credentials_and_transport_errors_are_safe(settings, caplog):
    enabled = settings.__class__(**{**settings.__dict__, "telegram_enabled": True})
    session = FakeSession([])
    with caplog.at_level(logging.ERROR):
        assert not TelegramNotifier(enabled, session=session).notify("Drafts")
    assert "telegram-secret" not in caplog.text

    complete = settings.__class__(**{
        **settings.__dict__,
        "telegram_enabled": True,
        "telegram_bot_token": "telegram-secret",
        "telegram_chat_id": "chat-1",
    })
    failing = FakeSession([requests.ConnectionError("https://api.telegram.org/bottelegram-secret/sendMessage")])
    with caplog.at_level(logging.ERROR):
        assert not TelegramNotifier(complete, session=failing).notify("Drafts")
    assert "telegram-secret" not in caplog.text


def test_indexnow_only_submits_confirmed_published_same_site_url():
    session = FakeSession([
        FakeResponse(status_code=202),
        FakeResponse(status_code=200, text="<urlset/>"),
    ])
    result = notify_published_url(
        "https://israinsols.com/published-article/",
        post_status="publish",
        indexnow_key="sample-key",
        session=session,
    )
    assert result.status == "success"
    assert result.indexnow_status == 202
    assert result.sitemap_status == 200
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url == INDEXNOW_ENDPOINT
    assert kwargs["json"]["urlList"] == ["https://israinsols.com/published-article/"]
    assert kwargs["json"]["keyLocation"] == "https://israinsols.com/sample-key.txt"
    assert session.calls[1][1] == "https://israinsols.com/sitemap_index.xml"


def test_indexing_skips_unpublished_and_missing_key_without_requests():
    session = FakeSession([])
    unpublished = notify_published_url(
        "https://israinsols.com/draft/",
        post_status="draft",
        indexnow_key="key",
        session=session,
    )
    assert unpublished.status == "skipped"
    assert not session.calls
    missing_key = notify_published_url(
        "https://israinsols.com/live/",
        post_status="publish",
        indexnow_key="",
        session=session,
    )
    assert missing_key.status == "skipped"
    assert "INDEXNOW_KEY" in missing_key.message
    assert INDEXNOW_KEY_FILE_INSTRUCTIONS
    assert not session.calls


def test_indexing_rejects_external_url_and_handles_api_failure():
    with pytest.raises(ValueError, match="configured site"):
        notify_published_url(
            "https://other.example/page/",
            post_status="publish",
            indexnow_key="key",
            session=FakeSession([]),
        )
    session = FakeSession([
        FakeResponse(status_code=429),
        FakeResponse(status_code=200),
    ])
    result = notify_published_url(
        "https://israinsols.com/live/",
        post_status="publish",
        indexnow_key="key",
        session=session,
    )
    assert result.status == "partial_failure"
    assert result.indexnow_status == 429
    assert result.sitemap_status == 200
