"""Premium access, free-tier quota, single credits and Telegram Stars invoices.

Premium fields on User are only written here, with atomic Mongo updates, so a
concurrent full save() of a stale User snapshot can't roll a payment back.
"""
from __future__ import annotations

import datetime
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, LabeledPrice
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pymongo import ReturnDocument

from app.config import settings
from app.constants import (
    ACTION_TRANSCRIPT,
    DOWNLOAD_QUALITIES,
    PREMIUM_QUALITIES,
    QUALITY_AUDIO,
    STARS_SUBSCRIPTION_PERIOD_SECONDS,
    TELEGRAM_MAX_UPLOAD_BYTES,
)
from app.database import DownloadRequest, User
from app.locales import localization
from app.utils import get_logger

logger = get_logger(__name__)

STARS_CURRENCY = "XTR"
QUOTA_WINDOW = datetime.timedelta(hours=24)
DAY_MS = 24 * 60 * 60 * 1000

PRODUCT_MONTH = "month"
PRODUCT_QUARTER = "quarter"
PRODUCT_YEAR = "year"
PRODUCT_SINGLE = "single"
PRODUCT_DAYS = {PRODUCT_MONTH: 30, PRODUCT_QUARTER: 90, PRODUCT_YEAR: 365}

# Lock reasons, also used as locale keys premium/locked_<reason>_mt.
REASON_QUALITY = "quality"
REASON_SIZE = "size"
REASON_QUOTA = "quota"
REASON_TRANSCRIPT = "transcript"

SINGLE_PAYLOAD_PREFIX = "one:"
VIDEO_ID_RE = re.compile(r"^[\w-]{6,32}$")
PENDING_ACTION_TTL_SECONDS = 30 * 60


def free_max_mb() -> int:
    """Free tier file size cap in MB; 0 when free users have no cap below Telegram's limit."""
    mb = settings.free_max_file_mb
    if mb <= 0 or mb * 1024 * 1024 >= TELEGRAM_MAX_UPLOAD_BYTES:
        return 0
    return mb


def free_max_bytes() -> int:
    """Free tier file size cap in bytes; 0 = no cap."""
    return free_max_mb() * 1024 * 1024


def utc(value: datetime.datetime | None) -> datetime.datetime | None:
    """Mongo returns naive UTC datetimes."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value


def from_unix(value: int | float | datetime.datetime | None) -> datetime.datetime | None:
    """Telegram sends dates (e.g. subscription_expiration_date) as Unix timestamps."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return utc(value)
    return datetime.datetime.fromtimestamp(value, datetime.UTC)


@dataclass(slots=True, frozen=True)
class Access:
    premium: bool  # paid premium active: no ads
    unlimited: bool  # no free-tier limits (premium, admin, or paywall disabled)
    priority: bool = False  # busy servers: download retried automatically (premium, admin)


def is_admin(user: User | None) -> bool:
    return bool(user and user.id in settings.admin_ids)


def get_access(user: User | None) -> Access:
    """Admins (ADMIN_IDS) get every limit lifted but still see ads, like the paywall-off mode."""
    premium = bool(user and user.is_premium)
    admin = is_admin(user)
    return Access(
        premium=premium,
        unlimited=premium or admin or not settings.premium_enabled,
        priority=premium or admin,
    )


@dataclass(slots=True, frozen=True)
class Grant:
    """How an allowed action runs."""

    no_ads: bool
    counts_to_quota: bool
    paid_credit: bool
    priority: bool = False


GRANT_FREE = Grant(no_ads=False, counts_to_quota=True, paid_credit=False)
GRANT_CREDIT = Grant(no_ads=True, counts_to_quota=False, paid_credit=True, priority=True)


def unlimited_grant(access: Access) -> Grant:
    return Grant(no_ads=access.premium, counts_to_quota=False, paid_credit=False, priority=access.priority)


def static_lock_reason(action: str, over_free_size: bool) -> str | None:
    """Lock reason that doesn't depend on the quota (free tier only)."""
    if action == ACTION_TRANSCRIPT:
        return REASON_TRANSCRIPT
    if action in PREMIUM_QUALITIES:
        return REASON_QUALITY
    if over_free_size and free_max_bytes():
        return REASON_SIZE
    return None


@dataclass(slots=True, frozen=True)
class QuotaStatus:
    used: int
    limit: int
    resets_at: datetime.datetime | None  # when the oldest counted download leaves the window

    @property
    def left(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


async def get_quota_status(user_id: int) -> QuotaStatus:
    """Free downloads used in the last 24 h. Cache hits and failed requests don't count."""
    now = datetime.datetime.now(datetime.UTC)
    counted = await DownloadRequest.find(
        {
            "user_id": user_id,
            "counts_to_quota": True,
            "status": {"$ne": "failed"},
            "created_at": {"$gte": now - QUOTA_WINDOW},
        },
    ).sort("created_at").to_list()
    resets_at = utc(counted[0].created_at) + QUOTA_WINDOW if counted else None
    return QuotaStatus(used=len(counted), limit=settings.free_daily_downloads, resets_at=resets_at)


def _users():
    return User.get_pymongo_collection()


async def grant_premium_days(user_id: int, days: int) -> datetime.datetime | None:
    """Extend premium by `days` from max(premium_until, now). Returns the new end."""
    now = datetime.datetime.now(datetime.UTC)
    doc = await _users().find_one_and_update(
        {"_id": user_id},
        [{"$set": {"premium_until": {"$add": [
            {"$max": [{"$ifNull": ["$premium_until", now]}, now]},
            int(days) * DAY_MS,
        ]}}}],
        return_document=ReturnDocument.AFTER,
    )
    return utc(doc.get("premium_until")) if doc else None


async def shift_premium_days(user_id: int, days: int) -> datetime.datetime | None:
    """Move premium_until by `days` (negative to take days back, e.g. on refund)."""
    doc = await _users().find_one_and_update(
        {"_id": user_id, "premium_until": {"$ne": None}},
        [{"$set": {"premium_until": {"$add": ["$premium_until", int(days) * DAY_MS]}}}],
        return_document=ReturnDocument.AFTER,
    )
    return utc(doc.get("premium_until")) if doc else None


async def revoke_premium(user_id: int) -> None:
    await _users().update_one(
        {"_id": user_id},
        {"$set": {"premium_until": None, "premium_auto_renew": False}},
    )


async def set_subscription(user_id: int, *, charge_id: str | None = None, auto_renew: bool) -> None:
    fields: dict = {"premium_auto_renew": auto_renew}
    if charge_id:
        fields["premium_subscription_charge_id"] = charge_id
    await _users().update_one({"_id": user_id}, {"$set": fields})


async def add_credits(user_id: int, count: int) -> int:
    """Add (or with a negative count remove, floored at 0) single credits; returns the balance."""
    doc = await _users().find_one_and_update(
        {"_id": user_id},
        [{"$set": {"single_credits": {"$max": [0, {"$add": [{"$ifNull": ["$single_credits", 0]}, int(count)]}]}}}],
        return_document=ReturnDocument.AFTER,
    )
    return int(doc.get("single_credits") or 0) if doc else 0


async def consume_credit(user_id: int) -> int | None:
    """Spend one single credit. Returns the remaining balance, or None if there was none."""
    doc = await _users().find_one_and_update(
        {"_id": user_id, "single_credits": {"$gt": 0}},
        {"$inc": {"single_credits": -1}},
        return_document=ReturnDocument.AFTER,
    )
    return int(doc.get("single_credits") or 0) if doc else None


async def return_request_credit(request: DownloadRequest) -> bool:
    """Give the single credit back once when a paid request fails."""
    if not request.paid_credit or request.credit_returned or request.id is None:
        return False
    result = await DownloadRequest.get_pymongo_collection().update_one(
        {"_id": request.id, "paid_credit": True, "credit_returned": {"$ne": True}},
        {"$set": {"credit_returned": True}},
    )
    if result.modified_count != 1:
        return False
    request.credit_returned = True
    await add_credits(request.user_id, 1)
    logger.info(f"Returned single credit to user_id={request.user_id} for request_id={request.request_id}")
    return True


# --- Invoice payloads -------------------------------------------------------

def build_single_payload(action: str, video_id: str) -> str:
    return f"{SINGLE_PAYLOAD_PREFIX}{action}:{video_id}"


@dataclass(slots=True, frozen=True)
class ParsedPayload:
    product: str
    action: str | None = None
    video_id: str | None = None


def parse_payload(payload: str) -> ParsedPayload | None:
    if payload in PRODUCT_DAYS:
        return ParsedPayload(product=payload)
    if payload.startswith(SINGLE_PAYLOAD_PREFIX):
        action, _sep, video_id = payload.removeprefix(SINGLE_PAYLOAD_PREFIX).partition(":")
        if (action in DOWNLOAD_QUALITIES or action == ACTION_TRANSCRIPT) and VIDEO_ID_RE.match(video_id):
            return ParsedPayload(product=PRODUCT_SINGLE, action=action, video_id=video_id)
    return None


def product_price(product: str) -> int:
    return {
        PRODUCT_MONTH: settings.premium_month_stars,
        PRODUCT_QUARTER: settings.premium_quarter_stars,
        PRODUCT_YEAR: settings.premium_year_stars,
        PRODUCT_SINGLE: settings.single_download_stars,
    }[product]


def action_label(action: str, language: str) -> str:
    if action == ACTION_TRANSCRIPT:
        return localization.get_text(language, "premium/action_transcript_mt").strip()
    if action == QUALITY_AUDIO:
        return localization.get_text(language, "download/audio_button_mt").strip()
    return action


# Invoice links for plans don't depend on the buyer, so one link per
# (language, product, price) is reused until restart.
_plan_links: dict[tuple[str, str, int], str] = {}


async def _create_invoice_link(
    bot: Bot,
    language: str,
    *,
    product: str,
    payload: str,
    description_kwargs: dict | None = None,
) -> str:
    def _(path: str, **kwargs) -> str:
        return localization.get_text(language, path, **kwargs).strip()

    price = product_price(product)
    return await bot.create_invoice_link(
        title=_(f"premium/invoice_{product}_title_mt")[:32],
        description=_(f"premium/invoice_{product}_desc_mt", **(description_kwargs or {}))[:255],
        payload=payload,
        currency=STARS_CURRENCY,
        prices=[LabeledPrice(label=_(f"premium/invoice_{product}_title_mt")[:32], amount=price)],
        subscription_period=STARS_SUBSCRIPTION_PERIOD_SECONDS if product == PRODUCT_MONTH else None,
    )


async def get_plan_link(bot: Bot, language: str, product: str) -> str:
    key = (language, product, product_price(product))
    link = _plan_links.get(key)
    if link is None:
        link = await _create_invoice_link(bot, language, product=product, payload=product)
        _plan_links[key] = link
    return link


async def get_single_link(bot: Bot, language: str, action: str, video_id: str) -> str:
    return await _create_invoice_link(
        bot,
        language,
        product=PRODUCT_SINGLE,
        payload=build_single_payload(action, video_id),
        description_kwargs={"what": action_label(action, language)},
    )


async def build_buy_keyboard(
    bot: Bot,
    language: str,
    *,
    user: User | None,
    single: tuple[str, str] | None = None,
) -> InlineKeyboardMarkup:
    """Plan buttons (invoice links); `single` = (action, video_id) adds the one-off option.

    The monthly subscription is hidden while one is already renewing, so a user
    doesn't end up with two parallel subscriptions.
    """
    def _(path: str, **kwargs) -> str:
        return localization.get_text(language, path, **kwargs).strip()

    builder = InlineKeyboardBuilder()
    sizes: list[int] = []
    if not (user and user.is_premium and user.premium_auto_renew):
        builder.button(
            text=_("premium/btn_month_mt", price=settings.premium_month_stars),
            url=await get_plan_link(bot, language, PRODUCT_MONTH),
        )
        sizes.append(1)
    builder.button(
        text=_("premium/btn_quarter_mt", price=settings.premium_quarter_stars),
        url=await get_plan_link(bot, language, PRODUCT_QUARTER),
    )
    builder.button(
        text=_("premium/btn_year_mt", price=settings.premium_year_stars),
        url=await get_plan_link(bot, language, PRODUCT_YEAR),
    )
    sizes.append(2)
    if single:
        action, video_id = single
        builder.button(
            text=_("premium/btn_single_mt", price=settings.single_download_stars, what=action_label(action, language)),
            url=await get_single_link(bot, language, action, video_id),
        )
        sizes.append(1)
    builder.adjust(*sizes)
    return builder.as_markup()


def format_wait(delta: datetime.timedelta, _: Callable[..., str]) -> str:
    """'5 год 12 хв' style countdown, at least 1 minute."""
    minutes = max(1, int(delta.total_seconds() + 59) // 60)
    hours, minutes = divmod(minutes, 60)
    return _("premium/wait_mt", hours=hours, minutes=minutes).strip()


def format_date(value: datetime.datetime | None) -> str:
    return utc(value).strftime("%d.%m.%Y") if value else ""


def build_benefits_text(_: Callable[..., str]) -> str:
    return _(
        "premium/benefits_mt",
        free_limit=settings.free_daily_downloads,
        free_mb=free_max_mb(),
    ).strip()


# --- Pending action ---------------------------------------------------------
# The plan links are shared, so their payload can't say which button sent the
# user to the paywall. Remember it here and start it after the payment.

@dataclass(slots=True, frozen=True)
class PendingAction:
    action: str
    video_id: str
    title: str
    created_at: float


_pending_actions: dict[int, PendingAction] = {}


def remember_pending_action(user_id: int, action: str, video_id: str, title: str = "") -> None:
    _pending_actions[user_id] = PendingAction(action, video_id, title, time.monotonic())


def pop_pending_action(user_id: int) -> PendingAction | None:
    pending = _pending_actions.pop(user_id, None)
    if pending and time.monotonic() - pending.created_at <= PENDING_ACTION_TTL_SECONDS:
        return pending
    return None
