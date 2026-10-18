from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

IST = ZoneInfo("Asia/Kolkata")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )

    database_url: str = "postgresql+psycopg://tradio:tradio@localhost:5432/tradio"
    # Optional until the bot milestone; data and signal commands don't need them.
    telegram_bot_token: SecretStr | None = None
    admin_telegram_id: int | None = None
    # v2 intraday alerts; without it only the evening run works.
    upstox_analytics_token: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
