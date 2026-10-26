"""Bot configuration with pydantic-settings."""
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class NatsServerConfig(BaseModel):
    """NATS server connection settings."""

    uri: str
    token: SecretStr | None = None
    manager_name: str = "manager"
    meta_timeout_seconds: float = 10.0
    download_timeout_seconds: float = 3.0


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Paths
    base_dir: Any = Field(default=Path(__file__).parent.parent)

    # Bot settings
    bot_token: SecretStr = Field(..., description="Telegram bot token")
    bot_name: str = Field(default="YtbVideoBot", description="Bot username without @")
    admin_ids: Any = Field(default_factory=list, description="List of admin user IDs")

    # Webhook settings (optional)
    webhook_enabled: bool = Field(default=False, description="Enable webhook mode")
    webhook_url: str | None = Field(default=None, description="Webhook URL")
    webhook_path: str = Field(default="/webhook", description="Webhook path")
    webhook_host: str = Field(default="0.0.0.0", description="Webhook host")
    webhook_port: int = Field(default=8080, description="Webhook port")

    # Database settings
    db_host: str = Field(default="localhost", description="MongoDB host")
    db_port: int = Field(default=27017, description="MongoDB port")
    db_name: str = Field(default="YtbVideoBot", description="MongoDB database name")
    db_user: str | None = Field(default=None, description="MongoDB username")
    db_password: SecretStr | None = Field(default=None, description="MongoDB password")

    # Redis settings (for FSM)
    redis_enabled: bool = Field(default=False, description="Enable Redis for FSM storage")
    redis_host: str = Field(default="localhost", description="Redis host")
    redis_port: int = Field(default=6379, description="Redis port")
    redis_db: int = Field(default=0, description="Redis database number")
    redis_password: SecretStr | None = Field(default=None, description="Redis password")

    # NATS settings
    nats_uri: str = Field(default="nats://135.181.26.26:4222", description="NATS server URI")
    nats_token: SecretStr | None = Field(default=None, description="NATS auth token")
    nats_manager_name: str = Field(default="manager", description="NATS manager prefix")
    nats_meta_timeout_seconds: float = Field(default=10.0, description="NATS meta request timeout")
    nats_download_timeout_seconds: float = Field(
        default=3.0,
        description="NATS manager_download request timeout",
    )

    nats_transcript_timeout_seconds: float = Field(
        default=60.0,
        description="NATS manager_transcript request timeout (manager runs 2 waves x 20 s)",
    )

    # Premium (Telegram Stars). PREMIUM_ENABLED=false turns the paywall off:
    # everyone gets every quality and size again, paid users still skip ads.
    premium_enabled: bool = Field(default=True, description="Enable free-tier limits and paywall")
    premium_month_stars: int = Field(default=99, description="30-day auto-renewing subscription price")
    premium_quarter_stars: int = Field(default=249, description="90-day one-time pack price")
    premium_year_stars: int = Field(default=799, description="365-day one-time pack price")
    single_download_stars: int = Field(default=10, description="One premium download or transcript")
    free_daily_downloads: int = Field(default=3, description="Free downloads per rolling 24 h (cache hits excluded)")
    free_max_file_mb: int = Field(
        default=500,
        description="Free tier max file size in MB; 0 = no limit beyond Telegram's 2 GB",
    )

    # Advertisement settings (legacy fastbots API)
    ad_token: str | None = Field(default=None, description="Advert API token")
    api_url_available_campaigns: str | None = Field(
        default="https://addpromoapi.fastbots.net/available-adverts",
        description="Campaign listing API URL",
    )
    api_url_get_advert: str | None = Field(
        default="https://addpromoapi.fastbots.net/get-advert",
        description="Get advert API URL",
    )

    # Advertisement settings (BotAdsLib)
    ad_api_base_url: str | None = Field(
        default=None,
        description="Advertisement API base URL",
    )
    ad_bot_hash: str | None = Field(
        default=None,
        description="Bot hash for BotAds API (x-bot-hash)",
    )
    ad_cache_channel_id: int | None = Field(
        default=None,
        description="Service channel ID for BotAds media cache",
    )
    ad_media_cache_path: str = Field(
        default=".botads-media-cache.json",
        description="Path to persistent BotAds media cache file",
    )

    # Access control for channels
    allowed_channel_ids: Any = Field(
        default_factory=list,
        description="List of allowed channel IDs where bot may stay",
    )

    # Localization
    default_language: Literal["uk", "en", "ru"] = Field(
        default="en",
        description="Default language",
    )
    available_languages: Any = Field(
        default=["uk", "en", "ru"],
        description="Available languages",
    )
    allowed_source_channel_ids: list[int] = Field(
        default_factory=list,
        description="Telegram channel IDs where bot is allowed to search files",
    )

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Logging level",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Customize settings sources."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        """Parse admin IDs from string or list."""
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("available_languages", mode="before")
    @classmethod
    def parse_languages(cls, v):
        """Parse languages from string or list."""
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("allowed_channel_ids", mode="before")
    @classmethod
    def parse_allowed_channels(cls, v):
        """Parse allowed channel IDs from list, JSON/YAML-like list, or CSV string."""
        if v is None:
            return []

        raw_items: list[Any]
        if isinstance(v, (list, tuple, set)):
            raw_items = list(v)
        elif isinstance(v, str):
            value = v.strip()
            if not value:
                return []
            if value.startswith("[") and value.endswith("]"):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        raw_items = parsed
                    else:
                        raw_items = [value]
                except json.JSONDecodeError:
                    inner = value[1:-1]
                    raw_items = [item for item in inner.replace("\n", ",").split(",")]
            else:
                raw_items = [item for item in value.replace("\n", ",").split(",")]
        else:
            raw_items = [v]

        channel_ids: list[int] = []
        seen: set[int] = set()
        for item in raw_items:
            text = str(item).strip().strip('"').strip("'")
            if not text:
                continue
            channel_id = int(text)
            if channel_id in seen:
                continue
            seen.add(channel_id)
            channel_ids.append(channel_id)
        return channel_ids

    @field_validator("allowed_source_channel_ids", mode="before")
    @classmethod
    def parse_allowed_source_channel_ids(cls, v):
        """Parse allowed channel IDs from list, JSON/YAML-like list, or CSV string."""
        if v is None:
            return []

        raw_items: list[Any]
        if isinstance(v, (list, tuple, set)):
            raw_items = list(v)
        elif isinstance(v, str):
            value = v.strip()
            if not value:
                return []

            if value.startswith("[") and value.endswith("]"):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        raw_items = parsed
                    else:
                        raw_items = [value]
                except json.JSONDecodeError:
                    # Fallback for YAML-like list in env vars.
                    inner = value[1:-1]
                    raw_items = [item for item in inner.replace("\n", ",").split(",")]
            else:
                raw_items = [item for item in value.replace("\n", ",").split(",")]
        else:
            raw_items = [v]

        channel_ids: list[int] = []
        seen: set[int] = set()
        for item in raw_items:
            text = str(item).strip().strip('"').strip("'")
            if not text:
                continue
            channel_id = int(text)
            if channel_id in seen:
                continue
            seen.add(channel_id)
            channel_ids.append(channel_id)

        return channel_ids

    @field_validator("ad_token", mode="before")
    @classmethod
    def parse_ad_token(cls, v):
        """Normalize ad token."""
        if v is None:
            return None
        if isinstance(v, str):
            value = v.strip()
            return value or None
        return v

    @field_validator("ad_cache_channel_id", mode="before")
    @classmethod
    def parse_ad_cache_channel_id(cls, v):
        """Parse cache channel ID from string/int."""
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return int(v.strip())
        return v

    @field_validator("ad_api_base_url", mode="before")
    @classmethod
    def parse_ad_api_base_url(cls, v):
        """Normalize BotAds API base URL."""
        if v is None:
            return None
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return None
            return value.rstrip("/")
        return v

    @field_validator("nats_token", mode="before")
    @classmethod
    def parse_nats_token(cls, v):
        """Normalize nats token."""
        if v is None:
            return None
        if isinstance(v, str):
            value = v.strip()
            return value or None
        return v

    @property
    def db_url(self) -> str:
        """Get MongoDB connection URL."""
        if self.db_user and self.db_password:
            password = self.db_password.get_secret_value()
            return f"mongodb://{self.db_user}:{password}@{self.db_host}:{self.db_port}"
        return f"mongodb://{self.db_host}:{self.db_port}"

    @property
    def redis_url(self) -> str:
        """Get Redis connection URL."""
        if self.redis_password:
            password = self.redis_password.get_secret_value()
            return f"redis://:{password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def nats_server(self) -> NatsServerConfig:
        """Get grouped NATS server settings."""
        return NatsServerConfig(
            uri=self.nats_uri,
            token=self.nats_token,
            manager_name=self.nats_manager_name,
            meta_timeout_seconds=self.nats_meta_timeout_seconds,
            download_timeout_seconds=self.nats_download_timeout_seconds,
        )


settings = Settings()
