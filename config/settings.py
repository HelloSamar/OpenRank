"""Centralized application settings.

Configuration is kept separate from runtime logic so future modules can share
one source of truth for environment configuration.
"""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    bot_token: str
    chat_id: str
    request_timeout: int = 30
    max_attempts: int = 3


def load_settings() -> Settings:
    """Load runtime configuration from environment variables."""
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()

    if not token:
        raise RuntimeError("Missing required environment variable: BOT_TOKEN")
    if not chat_id:
        raise RuntimeError("Missing required environment variable: CHAT_ID")

    return Settings(
        bot_token=token,
        chat_id=chat_id,
        request_timeout=int(os.environ.get("REQUEST_TIMEOUT", "30")),
        max_attempts=int(os.environ.get("MAX_ATTEMPTS", "3")),
    )
