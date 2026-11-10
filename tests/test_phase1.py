from __future__ import annotations

import copy
import json

import pytest
import requests

from config import load_settings
from db import connect, record_run
from wp_client import TOKEN_TTL_SECONDS, WordPressClient, WordPressError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.reason = "Unauthorized"

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, copy.deepcopy(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_AUTH_MODE", "jwt")
    monkeypatch.setenv("WP_USER", "editor")
    monkeypatch.setenv("WP_PASSWORD", "test-password")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "engine.sqlite3"))
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "engine.log"))
    return load_settings(env_file=tmp_path / "missing.env")


def test_database_schema_and_run_recording(tmp_path):
    connection = connect(tmp_path / "engine.sqlite3")
    run_id = record_run(connection, "check", "success", "ok")
    row = connection.execute("SELECT step, status, message FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert tuple(row) == ("check", "success", "ok")
    expected_tables = {"topics", "articles", "metrics", "social_posts", "refresh_queue", "runs"}
    tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert expected_tables <= tables
    connection.close()


def test_file_mode_saves_html_and_required_json_metadata(settings):
    settings = settings.__class__(**{**settings.__dict__, "wp_auth_mode": "file"})
    client = WordPressClient(settings)
    result = client.create_draft(
        {
            "title": "Build a faster business website",
            "slug": "faster-website",
            "html": "<p>Educational draft.</p>",
            "meta_description": "A practical guide.",
            "tags": ["web development"],
            "category": "web-development",
            "verify_claims": ["Check current browser guidance"],
        }
    )
    assert result.status == "draft"
    assert result.file_path and result.file_path.exists()
    metadata_path = result.file_path.with_suffix(".json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata == {
        "title": "Build a faster business website",
        "slug": "faster-website",
        "meta_description": "A practical guide.",
        "tags": ["web development"],
        "category": "web-development",
        "verify_claims": ["Check current browser guidance"],
        "focus_keyword": "",
        "rank_math_title": "Build a faster business website",
        "rank_math_description": "A practical guide.",
        "rank_math_focus_keyword": "",
    }
    assert "Educational draft" in result.file_path.read_text(encoding="utf-8")


def test_file_mode_does_not_overwrite_same_day_draft(settings):
    settings = settings.__class__(**{**settings.__dict__, "wp_auth_mode": "file"})
    client = WordPressClient(settings)
    first = client.save_file_draft({"title": "Same title", "html": "First"})
    second = client.save_file_draft({"title": "Same title", "html": "Second"})
    assert first.file_path != second.file_path
    assert "First" in first.file_path.read_text(encoding="utf-8")


def test_jwt_token_is_cached_and_sent_as_bearer(settings):
    session = FakeSession(
        [
            FakeResponse(payload={"token": "secret-token"}),
            FakeResponse(payload=[{"id": 7, "title": {"rendered": "Existing"}}]),
            FakeResponse(payload=[{"id": 8}]),
        ]
    )
    client = WordPressClient(settings, session=session)
    assert client.check_connection()[0]["id"] == 7
    assert client.list_posts()[0]["id"] == 8
    assert len([call for call in session.calls if call[0] == "POST"]) == 1
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer secret-token"
    assert session.calls[2][2]["headers"]["Authorization"] == "Bearer secret-token"


def test_jwt_token_refreshes_after_expiry(settings):
    now = [100.0]
    session = FakeSession(
        [
            FakeResponse(payload={"data": {"token": "first"}}),
            FakeResponse(payload=[]),
            FakeResponse(payload={"token": "second"}),
            FakeResponse(payload=[]),
        ]
    )
    client = WordPressClient(settings, session=session, clock=lambda: now[0])
    client.check_connection()
    now[0] += TOKEN_TTL_SECONDS + 1
    client.check_connection()
    assert [call[0] for call in session.calls].count("POST") == 2


@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_auth_http_error_falls_back_to_file_mode(settings, status_code):
    session = FakeSession(
        [
            FakeResponse(payload={"token": "token"}),
            FakeResponse(status_code=status_code, text="REST access denied"),
        ]
    )
    client = WordPressClient(settings, session=session)
    with pytest.raises(Exception, match=f"HTTP {status_code}"):
        client.check_connection()
    assert client.mode == "file"
    assert "REST access denied" in client.fallback_reason
    draft = client.create_draft({"title": "Fallback draft", "html": "<p>Local</p>"})
    assert draft.file_path and draft.file_path.exists()


@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_jwt_auth_endpoint_error_falls_back_to_file_mode(settings, status_code):
    session = FakeSession([FakeResponse(status_code=status_code, text="JWT route unavailable")])
    client = WordPressClient(settings, session=session)
    with pytest.raises(WordPressError, match=f"HTTP {status_code}"):
        client.check_connection()
    assert client.mode == "file"
    assert "JWT route unavailable" in client.fallback_reason


def test_configured_file_mode_never_attempts_wordpress(settings):
    settings = settings.__class__(**{**settings.__dict__, "wp_auth_mode": "file"})
    session = FakeSession([])
    client = WordPressClient(settings, session=session)
    with pytest.raises(WordPressError, match="WP_AUTH_MODE=file"):
        client.check_connection()
    assert session.calls == []


def test_non_writable_rank_math_meta_retries_without_meta(settings):
    session = FakeSession(
        [
            FakeResponse(payload={"token": "token"}),
            FakeResponse(status_code=400, text="meta is not allowed"),
            FakeResponse(payload={"id": 21}),
        ]
    )
    client = WordPressClient(settings, session=session)
    result = client.create_draft(
        {
            "title": "A useful guide",
            "html": "<p>Body</p>",
            "meta_description": "Summary",
            "focus_keyword": "useful guide",
        }
    )
    assert result.post_id == 21
    post_requests = [call for call in session.calls if call[0] == "POST" and "wp/v2/posts" in call[1]]
    assert len(post_requests) == 2
    assert "meta" in post_requests[0][2]["json"]
    assert "meta" not in post_requests[1][2]["json"]


def test_jwt_transport_error_does_not_leak_password_in_error(settings):
    session = FakeSession([requests.ConnectionError("connection refused")])
    client = WordPressClient(settings, session=session)
    with pytest.raises(Exception) as error:
        client.check_connection()
    assert "test-password" not in str(error.value)


def test_invalid_auth_mode_and_post_limit_are_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("WP_AUTH_MODE", "unknown")
    with pytest.raises(ValueError, match="WP_AUTH_MODE"):
        load_settings(env_file=tmp_path / "missing.env")
    monkeypatch.setenv("WP_AUTH_MODE", "file")
    monkeypatch.setenv("POSTS_PER_DAY", "4")
    with pytest.raises(ValueError, match="POSTS_PER_DAY"):
        load_settings(env_file=tmp_path / "missing.env")
