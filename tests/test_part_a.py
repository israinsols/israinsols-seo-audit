from __future__ import annotations

import logging

import pytest
import requests

from config import load_settings
from main import check_gemini
from writer import (
    DEFAULT_MODEL,
    GeminiHTTPError,
    GeminiWriter,
    WriterError,
    list_generate_content_models,
)


ARTICLE = {
    "title": "Website cost guide",
    "slug": "website-cost-guide",
    "meta_description": "Understand website cost planning.",
    "html": "<p>Educational content.</p>",
    "tags": ["website"],
    "category": "web-development",
    "image_prompt": "Planning illustration.",
    "verify_claims": ["Verify one", "Verify two", "Verify three"],
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


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


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "engine.sqlite3"))
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "engine.log"))
    return load_settings(env_file=tmp_path / "missing.env")


def _article_response():
    import json

    return FakeResponse(payload={"candidates": [{"content": {"parts": [{"text": json.dumps(ARTICLE)}]}}]})


def test_gemini_404_falls_back_to_next_configured_model(settings):
    session = FakeSession([
        FakeResponse(status_code=404, text="models/gemini-3.6-flash not found"),
        _article_response(),
    ])
    writer = GeminiWriter(settings, api_key="test-key", session=session, backoff_seconds=0)
    article = writer.generate("website cost", "web-development")
    assert article["focus_keyword"] == "website cost"
    assert [call[1].split("/models/")[1] for call in session.calls] == [
        "gemini-3.6-flash:generateContent",
        "gemini-2.5-flash:generateContent",
    ]


@pytest.mark.parametrize("status", [401, 403, 404])
def test_auth_or_final_model_404_is_not_retried_same_model(settings, status):
    settings = settings.__class__(**{**settings.__dict__, "gemini_fallback_models": ()})
    session = FakeSession([
        FakeResponse(status_code=status, text="credential error")
    ])
    writer = GeminiWriter(
        settings,
        api_key="test-key",
        session=session,
        max_retries=5,
        backoff_seconds=0,
    )
    with pytest.raises(GeminiHTTPError):
        writer.generate("website cost", "web-development")
    assert len(session.calls) == 1


def test_invalid_json_retries_same_model_with_backoff(settings):
    sleeps = []
    session = FakeSession([
        FakeResponse(payload={"candidates": [{"content": {"parts": [{"text": "invalid"}]}}]}),
        _article_response(),
    ])
    writer = GeminiWriter(
        settings,
        api_key="test-key",
        session=session,
        max_retries=1,
        backoff_seconds=0.25,
        sleep=sleeps.append,
    )
    assert writer.generate("website cost", "web-development")["title"] == ARTICLE["title"]
    assert len(session.calls) == 2
    assert all(call[1].endswith(f"/models/{DEFAULT_MODEL}:generateContent") for call in session.calls)
    assert sleeps == [0.25]


def test_requests_json_decode_error_is_retried(settings):
    malformed = requests.exceptions.JSONDecodeError("bad json", "x", 0)
    session = FakeSession([
        FakeResponse(payload=malformed),
        _article_response(),
    ])
    result = GeminiWriter(
        settings,
        api_key="test-key",
        session=session,
        max_retries=1,
        backoff_seconds=0,
    ).generate("website cost", "web-development")
    assert result["title"] == ARTICLE["title"]
    assert len(session.calls) == 2


def test_rate_limit_and_server_error_retry_but_do_not_fallback(settings):
    sleeps = []
    session = FakeSession([
        FakeResponse(status_code=429, text="busy"),
        FakeResponse(status_code=503, text="temporary"),
        _article_response(),
    ])
    writer = GeminiWriter(
        settings,
        api_key="test-key",
        session=session,
        max_retries=2,
        backoff_seconds=0.1,
        sleep=sleeps.append,
    )
    writer.generate("website cost", "web-development")
    assert len(session.calls) == 3
    assert all(call[1].endswith(f"/models/{DEFAULT_MODEL}:generateContent") for call in session.calls)
    assert sleeps == [0.1, 0.2]


def test_network_error_does_not_retry(settings):
    session = FakeSession([requests.ConnectionError("network unavailable")])
    with pytest.raises(WriterError, match="network errors are not retried"):
        GeminiWriter(settings, api_key="test-key", session=session, max_retries=3).generate(
            "website cost", "web-development"
        )
    assert len(session.calls) == 1


def test_gemini_model_environment_configuration(settings, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "custom-flash")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "fallback-a, fallback-b")
    configured = load_settings(env_file=settings.database_path.parent / "missing.env")
    session = FakeSession([_article_response()])
    GeminiWriter(configured, api_key="test-key", session=session).generate(
        "website cost", "web-development"
    )
    assert session.calls[0][1].endswith("/models/custom-flash:generateContent")


def test_model_list_filters_generate_content_and_never_logs_api_key(settings, monkeypatch, caplog):
    secret = "test-key-not-for-output"
    session = FakeSession([FakeResponse(payload={
        "models": [
            {"name": "models/gemini-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]},
        ]
    })])
    assert list_generate_content_models(secret, session=session) == ["gemini-flash"]
    assert session.calls[0][2]["params"]["key"] == secret

    monkeypatch.setenv("GEMINI_API_KEY", secret)
    with caplog.at_level(logging.INFO):
        assert check_gemini(settings, session=FakeSession([FakeResponse(payload={
            "models": [{"name": "models/gemini-flash", "supportedGenerationMethods": ["generateContent"]}]
        })]))
    assert secret not in caplog.text


def test_check_gemini_failure_has_actionable_hint_without_secret(settings, monkeypatch, caplog):
    secret = "test-key-not-for-output"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    with caplog.at_level(logging.ERROR):
        assert not check_gemini(
            settings,
            session=FakeSession([FakeResponse(status_code=403, text=secret)]),
        )
    assert secret not in caplog.text
    assert "Fix hint" in caplog.text
