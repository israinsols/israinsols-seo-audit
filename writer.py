"""Gemini-backed article drafting with strict JSON parsing and bounded retries."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import requests

from config import Settings

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_FALLBACK_MODELS = ("gemini-2.5-flash",)
GEMINI_URL = f"{GEMINI_API_BASE}/models/{DEFAULT_MODEL}:generateContent"
REQUIRED_FIELDS = {
    "title", "slug", "meta_description", "html", "tags",
    "category", "image_prompt", "verify_claims",
}


class WriterError(RuntimeError):
    """Raised when Gemini cannot return a valid article after retries."""


class GeminiHTTPError(WriterError):
    """An API error with a response status and model-fallback eligibility."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Gemini HTTP {status_code}: {detail}")


def _redact(value: str, api_key: str) -> str:
    return value.replace(api_key, "[REDACTED]") if api_key else value


def _models(settings: Settings) -> tuple[str, ...]:
    primary = (
        getattr(settings, "gemini_model", "")
        or os.getenv("GEMINI_MODEL", "")
        or DEFAULT_MODEL
    )
    configured = getattr(settings, "gemini_fallback_models", DEFAULT_FALLBACK_MODELS)
    if isinstance(configured, str):
        configured = tuple(item.strip() for item in configured.split(",") if item.strip())
    return tuple(dict.fromkeys([primary.strip(), *(model.strip() for model in configured if model.strip())]))


def list_generate_content_models(
    api_key: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 20,
) -> list[str]:
    """List model names whose API metadata advertises generateContent support."""
    if not api_key:
        raise WriterError("GEMINI_API_KEY is missing; add it to the local .env file.")
    try:
        response = (session or requests.Session()).get(
            f"{GEMINI_API_BASE}/models",
            params={"key": api_key},
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise WriterError(
                f"Gemini models request failed with HTTP {response.status_code}. "
                "Check the API key and Generative Language API access."
            )
        payload = response.json()
    except requests.RequestException as exc:
        raise WriterError(
            f"Could not list Gemini models ({type(exc).__name__}). Check network access and API key."
        ) from exc
    except ValueError as exc:
        raise WriterError("Gemini models endpoint returned invalid JSON.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise WriterError("Gemini models endpoint response did not contain a models list.")
    return sorted({
        str(item["name"]).removeprefix("models/")
        for item in payload["models"]
        if isinstance(item, dict)
        and "generateContent" in item.get("supportedGenerationMethods", [])
        and isinstance(item.get("name"), str)
    })


def _extract_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.I | re.S)
    if fenced:
        candidate = fenced.group(1).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Response did not contain a JSON object.")
    value = json.loads(candidate[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Response JSON must be an object.")
    missing = REQUIRED_FIELDS - value.keys()
    if missing:
        raise ValueError(f"Response is missing fields: {', '.join(sorted(missing))}.")
    if not isinstance(value["title"], str) or not value["title"].strip():
        raise ValueError("Article title must be a non-empty string.")
    if not isinstance(value["html"], str) or not value["html"].strip():
        raise ValueError("Article HTML must be a non-empty string.")
    if not isinstance(value["meta_description"], str) or len(value["meta_description"]) > 155:
        raise ValueError("Meta description must be a string of at most 155 characters.")
    if not isinstance(value["tags"], list) or not all(isinstance(item, str) for item in value["tags"]):
        raise ValueError("Tags must be a list of strings.")
    if (
        not isinstance(value["verify_claims"], list)
        or len(value["verify_claims"]) != 3
        or not all(isinstance(item, str) for item in value["verify_claims"])
    ):
        raise ValueError("verify_claims must contain exactly three strings for human review.")
    return value


def _business_fact_sections(path) -> dict[str, list[str]]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logging.warning("Business facts file %s is missing; company-specific claims are disabled.", path)
        return {}
    sections: dict[str, list[str]] = {}
    current = ""
    for line in content.splitlines():
        if line.startswith("#"):
            current = line.lstrip("# ").strip().casefold()
            sections[current] = []
            continue
        value = line.strip()
        if not value or value.startswith("TODO:") or "TODO:" in value:
            continue
        if value.startswith("- "):
            if current:
                sections[current].append(value[2:].strip())
        elif current and not value.startswith("Only complete") and not value.startswith("fields below"):
            sections[current].append(value)
    return sections


def load_business_facts(path) -> str:
    """Return only owner-filled facts, excluding the section of prohibited claims."""
    sections = _business_fact_sections(path)
    return "\n".join(
        f"{heading}: {'; '.join(values)}"
        for heading, values in sections.items()
        if values and heading != "forbidden claims"
    )


def load_forbidden_claims(path) -> str:
    """Return owner-filled claims that the writer must not make."""
    sections = _business_fact_sections(path)
    return "\n".join(sections.get("forbidden claims", []))


def _prompt(keyword: str, cluster: str, settings: Settings, retry_note: str = "") -> str:
    sections = _business_fact_sections(settings.business_facts_path)
    facts = "\n".join(
        f"{heading}: {'; '.join(values)}"
        for heading, values in sections.items()
        if values and heading != "forbidden claims"
    )
    forbidden_claims = "\n".join(sections.get("forbidden claims", []))
    facts_section = (
        facts if facts else
        "No verified business facts have been supplied. Do not state company services, "
        "locations, prices, timelines, technologies, client types, outcomes, or metrics. "
        "Write general educational content and use only a neutral contact CTA."
    )
    return f"""Return one STRICT JSON object only with fields title, slug, meta_description,
html, tags, category, image_prompt, verify_claims. Write a useful, original educational
article for {settings.audience}, focused on "{keyword}" in the "{cluster}" cluster.
Use a distinct practical angle, not generic filler. Title must be at most 60 characters,
contain the main keyword, and include Pakistan or 2026 where relevant without becoming
unnatural. The HTML body must be 900-1300 words, with <h2>/<h3>, short paragraphs,
at least three concrete steps or examples, and at least one useful checklist or
comparison table, a four-question FAQ, and a soft CTA. Use the company facts below
as the ONLY source for statements about IsrainSols; never infer or embellish them.
Do not invent statistics, facts, clients, case
studies, testimonials, results, or prices. Phrase uncertain material generally and
list exactly 3 concrete claims for a human to verify. meta_description must be <=155
characters. Include the keyword naturally in the title and opening. tags and
verify_claims are JSON string arrays. No markdown fences.

Verified IsrainSols facts:
{facts_section}

Claims specifically forbidden by the owner (never include or imply these):
{forbidden_claims or "No additional forbidden claims have been supplied."}
{retry_note}"""


class GeminiWriter:
    """Generate an article with a small, configurable number of response retries."""

    def __init__(
        self,
        settings: Settings,
        *,
        api_key: str | None = None,
        session: requests.Session | None = None,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
        sleep=time.sleep,
    ) -> None:
        self.settings = settings
        self.api_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")
        self.session = session or requests.Session()
        self.max_retries = max(0, max_retries)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.sleep = sleep

    @staticmethod
    def _response_error(response: requests.Response, api_key: str) -> GeminiHTTPError:
        try:
            detail = response.text
        except Exception:
            detail = ""
        detail = _redact(detail[:500], api_key)
        return GeminiHTTPError(response.status_code, detail or "No response details.")

    def generate(
        self,
        keyword: str,
        cluster: str,
        *,
        quality_feedback: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return parsed article JSON, retrying invalid model output."""
        if not self.api_key:
            raise WriterError("GEMINI_API_KEY is missing; add it to the local .env file.")
        errors: list[str] = []
        models = _models(self.settings)
        for model_index, model in enumerate(models):
            retry_note = ""
            for attempt in range(self.max_retries + 1):
                if errors:
                    retry_note = (
                        f"Your previous response was invalid ({errors[-1]}). "
                        "Return corrected JSON."
                    )
                if quality_feedback:
                    retry_note += (
                        " Fix these quality issues from the prior draft: "
                        + "; ".join(quality_feedback)
                        + "."
                    )
                try:
                    response = self.session.post(
                        f"{GEMINI_API_BASE}/models/{model}:generateContent",
                        params={"key": self.api_key},
                        json={
                            "contents": [{
                                "parts": [{"text": _prompt(keyword, cluster, self.settings, retry_note)}]
                            }],
                            "generationConfig": {"responseMimeType": "application/json"},
                        },
                        timeout=self.settings.request_timeout,
                    )
                    if response.status_code >= 400:
                        error = self._response_error(response, self.api_key)
                        if error.status_code == 404 and model_index + 1 < len(models):
                            logging.warning("Gemini model %s was not found; trying configured fallback.", model)
                            break
                        if error.status_code in {429} or error.status_code >= 500:
                            errors.append(str(error))
                            if attempt < self.max_retries:
                                self.sleep(self.backoff_seconds * (2 ** attempt))
                                continue
                        raise error
                    payload = response.json()
                    text = payload["candidates"][0]["content"]["parts"][0]["text"]
                    article = _extract_json(text)
                    article.setdefault("focus_keyword", keyword)
                    return article
                except GeminiHTTPError:
                    raise
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    errors.append(_redact(str(exc), self.api_key))
                    if attempt < self.max_retries:
                        self.sleep(self.backoff_seconds * (2 ** attempt))
                        logging.warning("Invalid Gemini response; retrying attempt %s.", attempt + 2)
                        continue
                    raise WriterError(
                        f"Gemini returned invalid article JSON after {attempt + 1} attempts: "
                        f"{_redact(str(exc), self.api_key)}"
                    ) from exc
                except requests.RequestException as exc:
                    errors.append(_redact(str(exc), self.api_key))
                    raise WriterError(
                        f"Gemini request failed ({type(exc).__name__}); network errors are not retried."
                    ) from exc
            else:
                # Retryable responses were exhausted; do not switch models for them.
                raise WriterError(
                    f"Gemini request failed after {self.max_retries + 1} attempts: {errors[-1]}"
                )
        raise GeminiHTTPError(404, "All configured Gemini model names returned not found.")
