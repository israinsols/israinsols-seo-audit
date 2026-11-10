"""Environment-backed configuration for the SEO Traffic Engine."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

SITE_URL = "https://israinsols.com"
NICHE_CLUSTERS: dict[str, list[str]] = {
    "google-business-profile": [
        "google business profile verification",
        "gmb verification",
        "gmb verification failed",
        "google business profile suspended",
        "google business profile optimization",
    ],
    "local-software-house": [
        "software house in hyderabad sindh",
        "software house in pakistan",
        "software company in hyderabad",
        "best software house hyderabad",
    ],
    "web-development-pakistan": [
        "website development cost pakistan",
        "custom website vs wordpress pakistan",
        "ecommerce website pakistan",
        "website development hyderabad sindh",
    ],
    "mobile-app-development": [
        "mobile app development cost pakistan",
        "android vs ios app development",
        "flutter app development pakistan",
    ],
    "online-reputation-and-seo": [
        "online reputation management small business",
        "local seo pakistan",
        "online reviews for small business",
        "technical seo small business",
    ],
}
PRIORITY_TOPICS: tuple[tuple[str, str], ...] = (
    ("How to Verify Google Business Profile in Pakistan (Step by Step)", "google-business-profile"),
    ("GMB Verification Failed? 7 Common Reasons and Fixes", "google-business-profile"),
    ("Google Business Profile Suspended: How to Get It Reinstated", "google-business-profile"),
    ("Do You Need a GMB Verification Service? What It Includes", "google-business-profile"),
    ("How to Choose a Software House in Hyderabad, Sindh", "local-software-house"),
    ("Best Software Houses in Hyderabad: What to Check Before Hiring", "local-software-house"),
    ("Website Development Cost in Pakistan (2026)", "web-development-pakistan"),
    ("Custom Website vs WordPress for Small Business in Pakistan", "web-development-pakistan"),
    ("Mobile App Development Cost in Pakistan (Android vs iOS)", "mobile-app-development"),
    ("Online Reputation Management for Small Businesses in Pakistan", "online-reputation-and-seo"),
)
KNOWN_EXISTING_PAGES: tuple[dict[str, str], ...] = (
    {
        "title": "Google Business Profile Verification",
        "slug": "google-business-profile-verification",
        "link": f"{SITE_URL}/google-business-profile-verification/",
        "status": "publish",
    },
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    site_url: str
    wp_url: str
    wp_auth_mode: str
    wp_user: str
    wp_password: str
    wp_app_password: str
    auto_publish: bool
    posts_per_day: int
    min_words: int
    max_words: int
    region: str
    audience: str
    database_path: Path
    drafts_dir: Path
    log_path: Path
    request_timeout: float
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    indexnow_key: str
    gemini_model: str
    gemini_fallback_models: tuple[str, ...]
    gsc_service_account_file: Path
    gsc_client_secret_file: Path
    gsc_token_file: Path
    social_telegram_enabled: bool
    social_telegram_channel: str
    pinterest_enabled: bool
    facebook_enabled: bool
    x_enabled: bool
    business_facts_path: Path


def load_settings(env_file: Path | None = None) -> Settings:
    """Load and validate settings without exposing secret values."""
    load_dotenv(dotenv_path=env_file, override=False)
    site_url = os.getenv("SITE_URL", SITE_URL).rstrip("/")
    wp_url = os.getenv("WP_URL", site_url).rstrip("/")
    auth_mode = os.getenv("WP_AUTH_MODE", "jwt").strip().lower()
    if auth_mode not in {"jwt", "app_password", "file"}:
        raise ValueError("WP_AUTH_MODE must be jwt, app_password, or file.")
    posts_per_day = int(os.getenv("POSTS_PER_DAY", "1"))
    if not 1 <= posts_per_day <= 3:
        raise ValueError("POSTS_PER_DAY must be between 1 and 3.")
    min_words = int(os.getenv("MIN_WORDS", "900"))
    max_words = int(os.getenv("MAX_WORDS", "1400"))
    if min_words < 1 or max_words < min_words:
        raise ValueError("MAX_WORDS must be greater than or equal to MIN_WORDS.")

    return Settings(
        site_url=site_url,
        wp_url=wp_url,
        wp_auth_mode=auth_mode,
        wp_user=os.getenv("WP_USER", ""),
        wp_password=os.getenv("WP_PASSWORD", ""),
        wp_app_password=os.getenv("WP_APP_PASSWORD", ""),
        auto_publish=_env_bool("AUTO_PUBLISH", False),
        posts_per_day=posts_per_day,
        min_words=min_words,
        max_words=max_words,
        region=os.getenv("REGION", "PK"),
        audience=os.getenv(
            "AUDIENCE",
            "small businesses and startups in Pakistan (Hyderabad, Sindh, Karachi, Lahore), "
            "plus international clients",
        ),
        database_path=Path(os.getenv("DATABASE_PATH", "data/engine.sqlite3")),
        drafts_dir=Path(os.getenv("DRAFTS_DIR", "drafts")),
        log_path=Path(os.getenv("LOG_PATH", "logs/engine.log")),
        request_timeout=float(os.getenv("REQUEST_TIMEOUT", "20")),
        telegram_enabled=_env_bool("TELEGRAM_ENABLED", False),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        indexnow_key=os.getenv("INDEXNOW_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip(),
        gemini_fallback_models=tuple(
            item.strip()
            for item in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-2.5-flash").split(",")
            if item.strip()
        ),
        gsc_service_account_file=Path(os.getenv("GSC_SERVICE_ACCOUNT_FILE", "")),
        gsc_client_secret_file=Path(os.getenv("GSC_CLIENT_SECRET_FILE", "client_secret.json")),
        gsc_token_file=Path(os.getenv("GSC_TOKEN_FILE", "token.pickle")),
        social_telegram_enabled=_env_bool("SOCIAL_TELEGRAM_ENABLED", False),
        social_telegram_channel=os.getenv("TELEGRAM_CHANNEL_ID", ""),
        pinterest_enabled=_env_bool("PINTEREST_ENABLED", False),
        facebook_enabled=_env_bool("FACEBOOK_ENABLED", False),
        x_enabled=_env_bool("X_ENABLED", False),
        business_facts_path=Path(os.getenv("BUSINESS_FACTS_FILE", "business_facts.md")),
    )
