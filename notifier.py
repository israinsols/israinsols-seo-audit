"""Optional Telegram notifications with safe, non-blocking failures."""

from __future__ import annotations

import logging
from typing import Any

import requests

from config import Settings


class TelegramNotifier:
    """Send draft-ready notifications when explicitly enabled in environment config."""

    def __init__(
        self,
        settings: Settings,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()

    def notify(self, message: str) -> bool:
        """Return false and log a safe error if Telegram is disabled or unavailable."""
        if not self.settings.telegram_enabled:
            logging.info("Telegram notifier is disabled.")
            return False
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            logging.error(
                "Telegram is enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing."
            )
            return False
        endpoint = (
            "https://api.telegram.org/bot"
            f"{self.settings.telegram_bot_token}/sendMessage"
        )
        try:
            response = self.session.post(
                endpoint,
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": message,
                    "disable_web_page_preview": True,
                },
                timeout=self.settings.request_timeout,
            )
            response.raise_for_status()
            payload: Any = response.json()
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                logging.error("Telegram rejected the notification (HTTP %s).", response.status_code)
                return False
            return True
        except requests.RequestException as exc:
            # Requests exceptions can include the token-bearing URL.
            logging.error("Telegram notification failed (%s).", type(exc).__name__)
            return False
        except ValueError:
            logging.error("Telegram returned invalid JSON.")
            return False
