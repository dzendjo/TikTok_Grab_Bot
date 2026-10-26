"""Database models using Beanie ODM."""
import datetime
from typing import Any

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, IndexModel

DOWNLOAD_REQUESTS_TTL_SECONDS = 30 * 24 * 60 * 60


class User(Document):
    """Bot user model."""

    class Settings:
        name = "botusers"
        indexes = [
            IndexModel([("is_active", ASCENDING)], name="is_active_1"),
            IndexModel([("visited", ASCENDING)], name="visited_1"),
        ]

    id: int | None = Field(default=None, alias="_id")
    process_flag: bool = Field(default=False)
    created: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    visited: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    username: str | None = Field(default=None)
    first_name: str | None = Field(default=None)
    last_name: str | None = Field(default=None)
    language_code: str | None = Field(default=None)
    language: str = Field(default="en")
    is_active: bool = Field(default=True)
    rate_limited_until: datetime.datetime | None = Field(default=None)

    # Premium state. Written only with atomic updates (app/services/premium.py),
    # never by a full save(), so concurrent updates can't roll it back.
    premium_until: datetime.datetime | None = Field(default=None)
    premium_auto_renew: bool = Field(default=False)
    premium_subscription_charge_id: str | None = Field(default=None)
    # Paid one-off premium actions (10 ⭐ each) not yet used.
    single_credits: int = Field(default=0)

    @property
    def is_premium(self) -> bool:
        """Paid premium is active right now."""
        until = self.premium_until
        if until is None:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=datetime.UTC)
        return until > datetime.datetime.now(datetime.UTC)

    @property
    def full_name(self) -> str:
        """Get user full name."""
        parts = [self.first_name, self.last_name]
        return " ".join(filter(None, parts)) or self.username or str(self.id)


class Advert(Document):
    """Advertisement campaign model."""

    class Settings:
        name = "adverts"
        indexes = [
            IndexModel([("name", ASCENDING)], unique=True, name="name_1"),
        ]

    name: str
    type: str | None = None
    admin_user_id: int
    date: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    done_users: list[int] = Field(default_factory=list)


class Track(Document):
    """Legacy track metadata model kept for DB backward compatibility."""

    class Settings:
        name = "tracks"
        indexes = [
            IndexModel([("inner_id", ASCENDING)], unique=True, name="inner_id_1"),
        ]

    inner_id: str
    extractor: str = ""
    site_id: str = ""
    add_date: datetime.datetime | None = None
    title: str = ""
    thumb_url: str = ""
    small_thumb_url: str = ""
    url: str = ""
    upload_date: str = ""
    uploader: str = ""
    duration: int = 0
    view_count: int = 0
    like_count: int | None = 0
    dislike_count: int = 0
    download_urls: list[dict] = Field(default_factory=list)


class DownloadRequest(Document):
    """User download request with status tracking."""

    class Settings:
        name = "download_requests"
        indexes = [
            IndexModel([("user_id", ASCENDING)], name="user_id_1"),
            IndexModel([("status", ASCENDING)], name="status_1"),
            IndexModel(
                [("created_at", ASCENDING)],
                name="created_at_ttl_30d",
                expireAfterSeconds=DOWNLOAD_REQUESTS_TTL_SECONDS,
            ),
        ]

    url: str
    quality: str = "audio"
    bot_name: str = ""
    language: str = "en"
    user_language: str = "en"
    user_id: int
    message_id: int = 0

    status: str = "created"
    progress: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    terminal: bool = False
    nats_ack: dict[str, Any] | None = None
    source_chat_id: int | None = None
    source_message_id: int | None = None
    source_caption: str | None = None
    media_sent: bool = False
    delivered_at: datetime.datetime | None = None

    # Premium flags captured when the request is created.
    premium: bool = False  # no ads in status / after delivery
    counts_to_quota: bool = False  # free-tier download, counts toward the daily limit
    paid_credit: bool = False  # paid with a single credit, returned if the request fails
    credit_returned: bool = False
    # Premium/admin/credit requests are retried while all servers are busy.
    priority: bool = False
    busy_since: datetime.datetime | None = None  # first busy failure of this download
    retry_attempt: int = 0
    is_retry: bool = False  # automatic retry: not counted by the hourly antispam

    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    updated_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC))

    @property
    def request_id(self) -> str:
        """String form of document ObjectId used by NATS."""
        return str(self.id)


class Payment(Document):
    """Telegram Stars payment (subscription charge, pack or single credit)."""

    class Settings:
        name = "payments"
        indexes = [
            IndexModel([("charge_id", ASCENDING)], unique=True, name="charge_id_1"),
            IndexModel([("user_id", ASCENDING)], name="user_id_1"),
            IndexModel([("created_at", ASCENDING)], name="created_at_1"),
        ]

    user_id: int
    product: str
    payload: str
    amount: int
    currency: str = "XTR"
    charge_id: str
    is_recurring: bool = False
    is_first_recurring: bool = False
    subscription_expiration_date: datetime.datetime | None = None
    granted_days: int = 0
    granted_credits: int = 0
    refunded: bool = False
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC))


class VideoMeta(Document):
    """Cached YT-API meta for the quality picker, keyed by YouTube video id.

    Only what the picker needs is kept (no download links, they expire).
    expires_at=None means the entry is kept forever; a failed download deletes it.
    """

    class Settings:
        name = "video_meta"
        indexes = [
            # TTL only removes documents whose expires_at is a date; None stays.
            IndexModel([("expires_at", ASCENDING)], name="expires_at_ttl", expireAfterSeconds=0),
        ]

    id: str | None = Field(default=None, alias="_id")
    title: str = ""
    duration: int = 0
    upload_date: str = ""
    formats: list[dict[str, Any]] = Field(default_factory=list)
    fetched_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    expires_at: datetime.datetime | None = None
