"""WordPress REST client with JWT, application-password, and local-file modes."""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import time
from dataclasses import dataclass
from datetime import date
from html import escape as html_escape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from requests.auth import HTTPBasicAuth

from config import Settings

TOKEN_TTL_SECONDS = 50 * 60


class WordPressError(RuntimeError):
    """A WordPress request failed; the message is safe to show to the operator."""

    def __init__(self, status_code: int | None, detail: str):
        self.status_code = status_code
        self.detail = detail
        status = f"HTTP {status_code}" if status_code is not None else "request error"
        super().__init__(f"{status}: {detail}")


@dataclass(frozen=True)
class DraftResult:
    post_id: int | None
    status: str
    file_path: Path | None = None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "untitled"


class WordPressClient:
    """WordPress client with automatic safe fallback to local draft files."""

    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._fallback_reason: str | None = None
        self._mode = settings.wp_auth_mode

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def fallback_reason(self) -> str | None:
        return self._fallback_reason

    def _url(self, path: str) -> str:
        return urljoin(f"{self.settings.wp_url}/", path.lstrip("/"))

    def _jwt_token(self) -> str:
        if self._token and self._clock() < self._token_expires_at:
            return self._token
        if not self.settings.wp_user or not self.settings.wp_password:
            raise WordPressError(
                None,
                "WP_USER/WP_PASSWORD are missing; configure a dedicated WordPress Editor account.",
            )
        try:
            response = self.session.post(
                self._url("/wp-json/jwt-auth/v1/token"),
                data={
                    "username": self.settings.wp_user,
                    "password": self.settings.wp_password,
                },
                timeout=self.settings.request_timeout,
            )
        except requests.RequestException as exc:
            raise WordPressError(None, f"JWT token request failed: {self._safe_text(str(exc))}") from exc
        if response.status_code != 200:
            error = self._response_error(response, "JWT authentication failed")
            if response.status_code in {401, 403, 404}:
                self._switch_to_file(str(error))
            raise error
        try:
            payload = response.json()
        except ValueError as exc:
            raise WordPressError(response.status_code, "JWT endpoint returned invalid JSON.") from exc
        token = payload.get("token")
        if not token and isinstance(payload.get("data"), dict):
            token = payload["data"].get("token")
        if not isinstance(token, str) or not token:
            raise WordPressError(
                response.status_code,
                "JWT endpoint response did not contain token or data.token.",
            )
        self._token = token
        self._token_expires_at = self._clock() + TOKEN_TTL_SECONDS
        return token

    def _safe_text(self, text: str) -> str:
        for secret in (self.settings.wp_password, self.settings.wp_app_password, self._token or ""):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def _response_error(self, response: requests.Response, context: str) -> WordPressError:
        detail = self._safe_text(response.text.strip() or response.reason or "No response details.")
        return WordPressError(response.status_code, f"{context}: {detail}")

    def _auth(self) -> dict[str, str] | HTTPBasicAuth:
        if self._mode == "jwt":
            return {"Authorization": f"Bearer {self._jwt_token()}"}
        if self._mode == "app_password":
            if not self.settings.wp_user or not self.settings.wp_app_password:
                raise WordPressError(
                    None,
                    "WP_USER/WP_APP_PASSWORD are missing; create a WordPress application password.",
                )
            return HTTPBasicAuth(self.settings.wp_user, self.settings.wp_app_password)
        return {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        allow_fallback: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        if self._mode == "file":
            raise WordPressError(None, "WordPress connection is disabled (file mode).")
        try:
            auth = self._auth()
            if isinstance(auth, HTTPBasicAuth):
                response = self.session.request(
                    method,
                    self._url(path),
                    auth=auth,
                    timeout=self.settings.request_timeout,
                    **kwargs,
                )
            else:
                headers = dict(kwargs.pop("headers", {}))
                headers.update(auth)
                response = self.session.request(
                    method,
                    self._url(path),
                    headers=headers,
                    timeout=self.settings.request_timeout,
                    **kwargs,
                )
        except requests.RequestException as exc:
            raise WordPressError(None, f"WordPress request failed: {self._safe_text(str(exc))}") from exc
        if response.status_code in {401, 403, 404} and allow_fallback:
            reason = self._response_error(response, "WordPress rejected the request")
            self._switch_to_file(str(reason))
            raise WordPressError(response.status_code, str(reason))
        if response.status_code >= 400:
            raise self._response_error(response, "WordPress request failed")
        return response

    def _switch_to_file(self, reason: str) -> None:
        self._mode = "file"
        self._fallback_reason = reason
        self._token = None

    def check_connection(self) -> list[dict[str, Any]]:
        """Authenticate and return up to five recent posts."""
        if self._mode == "file":
            raise WordPressError(None, "WP_AUTH_MODE=file; no WordPress connection was attempted.")
        response = self._request("GET", "/wp-json/wp/v2/posts?per_page=5")
        try:
            posts = response.json()
        except ValueError as exc:
            raise WordPressError(response.status_code, "Posts endpoint returned invalid JSON.") from exc
        if not isinstance(posts, list):
            raise WordPressError(response.status_code, "Posts endpoint response was not a list.")
        return posts

    def list_posts(self, per_page: int = 5) -> list[dict[str, Any]]:
        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100.")
        response = self._request("GET", f"/wp-json/wp/v2/posts?per_page={per_page}")
        payload = response.json()
        if not isinstance(payload, list):
            raise WordPressError(response.status_code, "Posts endpoint response was not a list.")
        return payload

    def get_post(self, post_id: int) -> dict[str, Any]:
        if post_id < 1:
            raise ValueError("post_id must be positive.")
        response = self._request("GET", f"/wp-json/wp/v2/posts/{post_id}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise WordPressError(response.status_code, "Post endpoint response was not an object.")
        return payload

    def list_categories(self, per_page: int = 100) -> list[dict[str, Any]]:
        return self._list_terms("categories", per_page)

    def list_tags(self, per_page: int = 100) -> list[dict[str, Any]]:
        return self._list_terms("tags", per_page)

    def _list_terms(self, taxonomy: str, per_page: int = 100) -> list[dict[str, Any]]:
        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100.")
        response = self._request("GET", f"/wp-json/wp/v2/{taxonomy}?per_page={per_page}")
        payload = response.json()
        if not isinstance(payload, list):
            raise WordPressError(response.status_code, f"{taxonomy} endpoint response was not a list.")
        return payload

    def create_category(self, name: str) -> int:
        return self._create_term("categories", name)

    def create_tag(self, name: str) -> int:
        return self._create_term("tags", name)

    def _create_term(self, taxonomy: str, name: str) -> int:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Taxonomy term name cannot be empty.")
        response = self._request(
            "POST",
            f"/wp-json/wp/v2/{taxonomy}",
            json={"name": clean_name},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise WordPressError(response.status_code, f"{taxonomy} endpoint returned invalid JSON.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
            raise WordPressError(response.status_code, f"{taxonomy} response did not include an ID.")
        return payload["id"]

    def upload_media(self, image_path: Path | str, alt_text: str) -> int:
        """Upload an image to WordPress Media Library and set descriptive alt text."""
        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(f"Featured image does not exist: {path}")
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise OSError(f"Could not read featured image {path}: {exc}") from exc
        response = self._request(
            "POST",
            "/wp-json/wp/v2/media",
            headers={
                "Content-Disposition": f'attachment; filename="{path.name}"',
                "Content-Type": mime_type,
            },
            data=data,
        )
        try:
            media = response.json()
        except ValueError as exc:
            raise WordPressError(response.status_code, "Media endpoint returned invalid JSON.") from exc
        if not isinstance(media, dict) or not isinstance(media.get("id"), int):
            raise WordPressError(response.status_code, "Media endpoint response did not include an ID.")
        media_id = media["id"]
        if alt_text.strip():
            self._request(
                "POST",
                f"/wp-json/wp/v2/media/{media_id}",
                json={"alt_text": alt_text.strip()},
            )
        return media_id

    def _term_id(self, taxonomy: str, value: str) -> int | None:
        name = value.strip()
        if not name:
            return None
        terms = self._list_terms(taxonomy)
        match = next(
            (
                term for term in terms
                if str(term.get("name", "")).casefold() == name.casefold()
            ),
            None,
        )
        if match:
            return match.get("id") if isinstance(match.get("id"), int) else None
        try:
            return self._create_term(taxonomy, name)
        except WordPressError as exc:
            # Editors may be permitted to assign existing terms but not create them.
            if exc.status_code == 400:
                terms = self._list_terms(taxonomy)
                match = next(
                    (
                        term for term in terms
                        if str(term.get("name", "")).casefold() == name.casefold()
                    ),
                    None,
                )
                if match and isinstance(match.get("id"), int):
                    return match["id"]
            raise

    def _taxonomy_ids(self, article: dict[str, Any]) -> tuple[list[int], list[int]]:
        category = article.get("category", "")
        categories = category if isinstance(category, list) else [category]
        category_ids = [
            term_id
            for value in categories
            if (term_id := self._term_id("categories", str(value))) is not None
        ]
        tags = article.get("tags", [])
        if not isinstance(tags, list):
            raise ValueError("Article tags must be a list.")
        tag_ids = [
            term_id
            for value in tags
            if (term_id := self._term_id("tags", str(value))) is not None
        ]
        return category_ids, tag_ids

    def create_draft(self, article: dict[str, Any]) -> DraftResult:
        """Create a draft or explicitly enabled live post, uploading media and terms."""
        if self._mode == "file":
            if self.settings.auto_publish:
                raise WordPressError(
                    None,
                    "AUTO_PUBLISH=True requires an authenticated WordPress connection; "
                    "file mode cannot publish live posts.",
                )
            return self.save_file_draft(article)
        title = str(article.get("title", "")).strip()
        if not title:
            raise ValueError("Article title cannot be empty.")
        slug = _slugify(str(article.get("slug") or title))
        try:
            category_ids, tag_ids = self._taxonomy_ids(article)
            image = article.get("featured_image_path") or article.get("featured_image")
            if image:
                image_path = Path(image)
                if not image_path.is_absolute():
                    image_path = self.settings.drafts_dir / image_path
                media_id = self.upload_media(image_path, str(article.get("image_alt", "")))
            else:
                media_id = None
        except WordPressError:
            if self._mode == "file":
                return self.save_file_draft(article)
            raise
        payload: dict[str, Any] = {
            "title": title,
            "slug": slug,
            "content": str(article.get("html", "")),
            "status": "publish" if self.settings.auto_publish else "draft",
            "excerpt": str(article.get("meta_description", "")),
        }
        if category_ids:
            payload["categories"] = category_ids
        if tag_ids:
            payload["tags"] = tag_ids
        if media_id:
            payload["featured_media"] = media_id
        meta: dict[str, str] = {}
        meta_values = {
            "rank_math_title": title,
            "rank_math_description": str(article.get("meta_description", "")),
            "rank_math_focus_keyword": str(article.get("focus_keyword", "")),
        }
        for key, value in meta_values.items():
            if value:
                meta[key] = value
        if meta:
            payload["meta"] = meta
        try:
            response = self._request("POST", "/wp-json/wp/v2/posts", json=payload)
        except WordPressError as exc:
            if exc.status_code == 400 and "meta" in payload:
                payload.pop("meta")
                try:
                    response = self._request("POST", "/wp-json/wp/v2/posts", json=payload)
                except WordPressError:
                    if self._mode == "file":
                        return self.save_file_draft(article)
                    raise
            elif self._mode == "file":
                return self.save_file_draft(article)
            else:
                raise
        try:
            result = response.json()
        except ValueError as exc:
            raise WordPressError(response.status_code, "Post endpoint returned invalid JSON.") from exc
        if not isinstance(result, dict) or not isinstance(result.get("id"), int):
            raise WordPressError(response.status_code, "Post creation response did not include an ID.")
        expected_status = "publish" if self.settings.auto_publish else "draft"
        status = result.get("status")
        confirmed_status = status == expected_status if self.settings.auto_publish else status in {
            None,
            expected_status,
        }
        if not confirmed_status:
            raise WordPressError(
                response.status_code,
                f"WordPress returned status {status!r}; expected {expected_status!r}.",
            )
        return DraftResult(post_id=result["id"], status=status or expected_status)

    def save_file_draft(self, article: dict[str, Any]) -> DraftResult:
        """Write article HTML and its metadata beside one another without overwriting."""
        title = str(article.get("title", "")).strip()
        if not title:
            raise ValueError("Article title cannot be empty.")
        slug = _slugify(str(article.get("slug") or title))
        base = f"{date.today().isoformat()}_{slug}"
        drafts_dir = self.settings.drafts_dir
        drafts_dir.mkdir(parents=True, exist_ok=True)
        stem = base
        suffix = 2
        while any(
            (drafts_dir / f"{stem}{extension}").exists()
            for extension in (".html", ".json", ".png")
        ):
            stem = f"{base}_{suffix}"
            suffix += 1
        html_path = drafts_dir / f"{stem}.html"
        json_path = drafts_dir / f"{stem}.json"
        content = str(article.get("html", ""))
        html_path.write_text(
            f"<h1>{html_escape(title)}</h1>\n"
            f"<p><strong>Meta description:</strong> "
            f"{html_escape(str(article.get('meta_description', '')))}</p>\n{content}\n",
            encoding="utf-8",
        )
        metadata = {
            "title": title,
            "slug": slug,
            "meta_description": article.get("meta_description", ""),
            "tags": article.get("tags", []),
            "category": article.get("category", ""),
            "verify_claims": article.get("verify_claims", []),
            "focus_keyword": article.get("focus_keyword", ""),
            "rank_math_title": article.get("title", ""),
            "rank_math_description": article.get("meta_description", ""),
            "rank_math_focus_keyword": article.get("focus_keyword", ""),
        }
        source_image = article.get("featured_image_path")
        if source_image:
            source_path = Path(source_image)
            if source_path.is_file():
                image_target = drafts_dir / source_path.name
                if source_path.resolve() != image_target.resolve():
                    if image_target.exists():
                        image_target = drafts_dir / f"{stem}_{source_path.name}"
                    shutil.copy2(source_path, image_target)
                metadata["featured_image"] = image_target.name
                metadata["image_alt"] = article.get("image_alt", "")
        elif article.get("featured_image"):
            metadata["featured_image"] = article["featured_image"]
            metadata["image_alt"] = article.get("image_alt", "")
        json_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return DraftResult(post_id=None, status="draft", file_path=html_path)
