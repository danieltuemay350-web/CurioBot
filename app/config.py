"""Validated environment configuration."""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    telegram_admin_ids: str = ""
    telegram_target_chat_id: str = ""
    gemini_api_key: str = ""
    gemini_model: str = ""
    database_url: str = "sqlite:///./data/bot.sqlite3"
    log_level: str = "INFO"
    timezone: str = "Africa/Addis_Ababa"
    gemini_daily_request_budget: int = Field(default=10, ge=1, le=10000)
    gemini_daily_token_budget: int = Field(default=0, ge=0)
    gemini_reset_timezone: str = "UTC"
    article_text_retention_days: int = Field(default=7, ge=1, le=365)
    rss_poll_interval_minutes: int = Field(default=30, ge=5, le=1440)
    process_interval_minutes: int = Field(default=5, ge=1, le=1440)
    publication_daily_limit: int = Field(default=5, ge=1, le=20)
    publication_window_start: str = "08:00"
    publication_window_end: str = "21:00"
    max_overdue_age_hours: int = Field(default=24, ge=1, le=168)
    catchup_min_interval_minutes: int = Field(default=180, ge=30, le=1440)
    publish_retry_limit: int = Field(default=3, ge=1, le=10)
    retry_base_seconds: int = Field(default=60, ge=1, le=3600)
    http_connect_timeout_seconds: float = Field(default=5, gt=0, le=30)
    http_read_timeout_seconds: float = Field(default=10, gt=0, le=60)
    http_max_response_bytes: int = Field(default=2_000_000, ge=1024, le=20_000_000)
    http_max_redirects: int = Field(default=3, ge=0, le=10)
    http_allowed_ports: str = "80,443"
    rss_feed_urls: str = ""

    @field_validator("timezone", "gemini_reset_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {value}") from exc
        return value

    @field_validator("publication_window_start", "publication_window_end")
    @classmethod
    def valid_hhmm(cls, value: str) -> str:
        try:
            hour, minute = map(int, value.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
        except ValueError as exc:
            raise ValueError("Time must use HH:MM, 24-hour format") from exc
        return value

    @field_validator("http_allowed_ports")
    @classmethod
    def valid_ports(cls, value: str) -> str:
        ports = [int(port.strip()) for port in value.split(",") if port.strip()]
        if not ports or any(port < 1 or port > 65535 for port in ports):
            raise ValueError("HTTP_ALLOWED_PORTS must contain valid comma-separated ports")
        return value

    @field_validator("database_url")
    @classmethod
    def sqlite_only(cls, value: str) -> str:
        if not value.startswith("sqlite:///"):
            raise ValueError("DATABASE_URL must name a file-backed SQLite database")
        if value.removeprefix("sqlite:///").split("?", 1)[0] in {"", ":memory:"}:
            raise ValueError("DATABASE_URL must be file-backed so application state survives restart")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "Settings":
        if self.publication_window_start == self.publication_window_end:
            raise ValueError("Publication window start and end must differ")
        return self

    @property
    def admin_ids(self) -> frozenset[int]:
        try:
            values = frozenset(int(value.strip()) for value in self.telegram_admin_ids.split(",") if value.strip())
        except ValueError as exc:
            raise ValueError("TELEGRAM_ADMIN_IDS must be comma-separated integers") from exc
        if any(value <= 0 for value in values):
            raise ValueError("TELEGRAM_ADMIN_IDS must contain positive Telegram user IDs")
        return values

    @property
    def feed_urls(self) -> tuple[str, ...]:
        return tuple(url.strip() for url in self.rss_feed_urls.split(",") if url.strip())

    @property
    def allowed_ports(self) -> frozenset[int]:
        return frozenset(int(port.strip()) for port in self.http_allowed_ports.split(","))

    @property
    def credentials_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.admin_ids and self.telegram_target_chat_id and self.gemini_api_key and self.gemini_model)

    def require_runtime_credentials(self) -> None:
        missing = [
            key for key, value in (
                ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token),
                ("TELEGRAM_ADMIN_IDS", self.telegram_admin_ids),
                ("TELEGRAM_TARGET_CHAT_ID", self.telegram_target_chat_id),
                ("GEMINI_API_KEY", self.gemini_api_key),
                ("GEMINI_MODEL", self.gemini_model),
            ) if not value
        ]
        if missing or not self.admin_ids:
            names = ", ".join(missing or ["TELEGRAM_ADMIN_IDS"])
            raise ValueError(f"Required configuration is missing or invalid: {names}")


@lru_cache
def get_settings() -> Settings:
    return load_settings()


def load_settings(env_file: str | None = ".env") -> Settings:
    """Load settings with Pydantic's normal process-environment precedence."""
    return Settings(_env_file=env_file)
