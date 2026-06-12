"""Global configuration loaded from environment variables.

All secrets and tunables come from the environment (or a local .env file). Nothing here
should hold a default that is a real secret. See .env.example for the full list.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    BOT_TOKEN: str

    # Database (SQLAlchemy async URL, e.g. postgresql+asyncpg://...)
    DATABASE_URL: str

    # Sports data (API-Football v3)
    API_FOOTBALL_KEY: str
    API_FOOTBALL_HOST: str = "https://v3.football.api-sports.io"
    LEAGUE_ID: int = 1
    SEASON: int = 2026

    # Misc
    TZ: str = "Asia/Singapore"
    LOG_LEVEL: str = "INFO"

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.TZ)


# Imported wherever configuration is needed.
settings = Settings()  # type: ignore[call-arg]
