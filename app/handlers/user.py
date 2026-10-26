"""User handlers for YouTube download flow."""
from __future__ import annotations

import asyncio
import datetime
import html
import re
from collections.abc import Callable
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message, MessageEntity
from aiogram.utils.keyboard import InlineKeyboardBuilder
from nats.aio.client import Client as NatsClient
from nats.errors import NoRespondersError, TimeoutError

from app.config import settings
from app.constants import (
    ACTION_TRANSCRIPT,
    CACHED_ICON,
    DOWNLOAD_ICON,
    DOWNLOAD_QUALITIES,
    LOCKED_ICON,
    QUALITY_AUDIO,
    TRANSCRIPT_ICON,
)
from app.database import DownloadRequest, User
from app.locales import localization
from app.services import meta_cache, premium
from app.services.nats_meta import describe_error, describe_fatal_meta_error, send_post_delivery_advert
from app.services.media_cache import get_cached_sizes, try_send_cached_media_for_url
from app.services.quality_options import QualityOption, build_quality_options, format_duration, format_size
from app.services import request_media_download, request_social_meta, request_transcript
from app.utils import get_logger
from app.utils.youtube import extract_youtube_video_id

router = Router(name="user")
logger = get_logger(__name__)

YOUTUBE_URL_RE = re.compile(r"https?://(?:[\w-]+\.)*(?:youtube\.com|youtu\.be)/\S+", re.IGNORECASE)
YOUTUBE_VIDEO_ID_RE = re.compile(r"^[\w-]{6,32}$")
DURATION_RE = re.compile(r"\d+:\d{2}(?::\d{2})?")
FILENAME_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\s]+')
LANG_CALLBACK_PREFIX = "setlang:"
QUALITY_CALLBACK_PREFIX = "dl:"
TRANSCRIPT_CALLBACK_PREFIX = "tx:"
# Appended to dl: callback data when the option is over the free size limit.
OVER_FREE_SIZE_FLAG = "b"
LANG_ORDER = ("uk", "en", "ru")
RATE_LIMIT_REQUESTS_PER_HOUR = 20
# Premium and admins: a ceiling against scripts and shared accounts, without the long block.
PREMIUM_RATE_LIMIT_REQUESTS_PER_HOUR = 100
RATE_LIMIT_WINDOW = datetime.timedelta(hours=1)
RATE_LIMIT_BLOCK_DURATION = datetime.timedelta(hours=6)
REQUEST_CREATED_EMOJI = "👨‍💻"
REQUEST_CREATED_EMOJI_UTF16_LENGTH = 5
REQUEST_CREATED_CUSTOM_EMOJI_ID = "5413623448440160154"
FULL_ACCESS = premium.Access(premium=False, unlimited=True)

# Quality pickers whose callback is being processed right now. Guards against
# double taps creating two downloads of the same video: YT-API workers share a
# work dir and parallel downloads of one URL can ship a broken file.
_pickers_in_progress: set[tuple] = set()


def _build_language_keyboard():
    """Build inline keyboard with supported languages."""
    builder = InlineKeyboardBuilder()
    available = localization.available_languages

    ordered_codes: list[str] = [code for code in LANG_ORDER if code in available]
    ordered_codes.extend(code for code in available if code not in ordered_codes)

    for code in ordered_codes:
        builder.button(
            text=available[code],
            callback_data=f"{LANG_CALLBACK_PREFIX}{code}",
        )
    builder.adjust(3)
    return builder.as_markup()


def _is_over_free_size(option: QualityOption) -> bool:
    limit = premium.free_max_bytes()
    return bool(limit and option.size_bytes and option.size_bytes > limit)


def _is_locked(option: QualityOption, access: premium.Access, quota: premium.QuotaStatus | None) -> bool:
    if access.unlimited:
        return False
    if premium.static_lock_reason(option.quality, _is_over_free_size(option)):
        return True
    return not option.cached and quota is not None and quota.exhausted


def _build_quality_keyboard(
    video_id: str,
    options: list[QualityOption],
    _: Callable[..., str],
    access: premium.Access = FULL_ACCESS,
    quota: premium.QuotaStatus | None = None,
):
    """One button per row: cache/lock icon, quality, size; transcript button last."""
    builder = InlineKeyboardBuilder()
    for option in options:
        if _is_locked(option, access, quota):
            icon = LOCKED_ICON
        else:
            icon = CACHED_ICON if option.cached else DOWNLOAD_ICON
        label = _("download/audio_button_mt") if option.quality == QUALITY_AUDIO else option.label
        size = f" · {format_size(option.size_bytes)}" if option.size_bytes else ""
        flag = f":{OVER_FREE_SIZE_FLAG}" if _is_over_free_size(option) else ""
        builder.button(
            text=f"{icon} {label}{size}",
            callback_data=f"{QUALITY_CALLBACK_PREFIX}{option.quality}:{video_id}{flag}",
        )
    transcript_icon = TRANSCRIPT_ICON if access.unlimited else LOCKED_ICON
    builder.button(
        text=f"{transcript_icon} {_('download/transcript_button_mt').strip()}",
        callback_data=f"{TRANSCRIPT_CALLBACK_PREFIX}{video_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def _build_picker_text(
    meta: dict | None,
    _: Callable[..., str],
    access: premium.Access = FULL_ACCESS,
    quota: premium.QuotaStatus | None = None,
) -> str:
    title = ""
    duration = ""
    if meta and meta.get("result") == "ok":
        title = html.escape(str(meta.get("title") or "").strip(), quote=False)
        if meta.get("duration"):
            duration = format_duration(int(meta["duration"]))
    show_quota = quota is not None and not access.unlimited
    quota_wait = ""
    if show_quota and quota.exhausted and quota.resets_at:
        quota_wait = premium.format_wait(quota.resets_at - datetime.datetime.now(datetime.UTC), _)
    return _(
        "download/choose_quality_mt",
        title=title,
        duration=duration,
        cached_icon=CACHED_ICON,
        download_icon=DOWNLOAD_ICON,
        locked_icon=LOCKED_ICON,
        premium=access.premium,
        show_quota=show_quota,
        quota_used=quota.used if quota else 0,
        quota_left=quota.left if quota else 0,
        quota_limit=quota.limit if quota else 0,
        quota_wait=quota_wait,
    )


def _parse_quality_callback(data: str) -> tuple[str, str, bool] | None:
    """Parse `dl:<quality>:<video_id>[:b]` callback data."""
    parts = data.removeprefix(QUALITY_CALLBACK_PREFIX).split(":")
    if len(parts) not in (2, 3):
        return None
    quality, video_id = parts[0], parts[1]
    flag = parts[2] if len(parts) == 3 else ""
    if quality not in DOWNLOAD_QUALITIES or not YOUTUBE_VIDEO_ID_RE.match(video_id):
        return None
    if flag not in ("", OVER_FREE_SIZE_FLAG):
        return None
    return quality, video_id, flag == OVER_FREE_SIZE_FLAG


def _build_video_url(video_id: str) -> str:
    """Canonical watch URL: callback_data is capped at 64 bytes, so only the ID travels."""
    return f"https://www.youtube.com/watch?v={video_id}"


def _extract_youtube_url(text: str) -> str | None:
    match = YOUTUBE_URL_RE.search(text)
    if not match:
        return None

    url = match.group(0).strip(" \n\t\r<>[](){}.,!?\"'")
    if extract_youtube_video_id(url):
        return url

    return None


def _title_from_picker(picker: Message) -> str:
    """Video title from the picker's first line `🎬 <title> · <duration>`."""
    first_line = (picker.text or "").split("\n", 1)[0].strip()
    if not first_line.startswith("🎬"):
        return ""
    title = first_line.removeprefix("🎬").strip()
    head, sep, tail = title.rpartition(" · ")
    if sep and DURATION_RE.fullmatch(tail):
        title = head
    return title.strip()


def _build_request_created_entities(text: str) -> list[MessageEntity] | None:
    if not text.startswith(REQUEST_CREATED_EMOJI):
        return None
    return [
        MessageEntity(
            type="custom_emoji",
            offset=0,
            length=REQUEST_CREATED_EMOJI_UTF16_LENGTH,
            custom_emoji_id=REQUEST_CREATED_CUSTOM_EMOJI_ID,
        ),
    ]


async def _save_cache_hit_request(
    *,
    url: str,
    quality: str,
    user_id: int,
    message_id: int,
    language: str,
) -> None:
    """Persist cache hit as DownloadRequest for analytics/ratio reporting."""
    now = datetime.datetime.now(datetime.UTC)
    cache_request = DownloadRequest(
        url=url,
        quality=quality,
        bot_name=settings.bot_name,
        language=language,
        user_language=language,
        user_id=user_id,
        message_id=message_id,
        status="cache_hit",
        terminal=True,
        media_sent=True,
        delivered_at=now,
    )
    cache_request.updated_at = now
    await cache_request.insert()


def _rate_limited_requests(user_id: int, since: datetime.datetime):
    """Requests the antispam counts: automatic retries and ⚡ cache hits don't load the servers."""
    return DownloadRequest.find(
        DownloadRequest.user_id == user_id,
        DownloadRequest.created_at >= since,
        {"is_retry": {"$ne": True}, "status": {"$ne": "cache_hit"}},
    )


async def _get_rate_limit_text(user: User, user_id: int, _: Callable[..., str]) -> str | None:
    """Antispam reply, or None when the request may go ahead.

    Free users get blocked for RATE_LIMIT_BLOCK_DURATION after too many requests an hour.
    Premium users and admins have a higher ceiling and only wait until the oldest
    request leaves the window.
    """
    now = datetime.datetime.now(datetime.UTC)
    recent_window_start = now - RATE_LIMIT_WINDOW

    if user.is_premium or premium.is_admin(user):
        recent_requests_count = await _rate_limited_requests(user_id, recent_window_start).count()
        if recent_requests_count < PREMIUM_RATE_LIMIT_REQUESTS_PER_HOUR:
            return None
        oldest = await (
            _rate_limited_requests(user_id, recent_window_start)
            .sort(+DownloadRequest.created_at)
            .skip(recent_requests_count - PREMIUM_RATE_LIMIT_REQUESTS_PER_HOUR)
            .first_or_none()
        )
        if oldest is None:
            return None
        remaining_seconds = int((premium.utc(oldest.created_at) + RATE_LIMIT_WINDOW - now).total_seconds())
        return _("download/rate_limited_minutes_mt", retry_minutes=max(1, (remaining_seconds + 59) // 60))

    blocked_until = premium.utc(user.rate_limited_until)
    if blocked_until and blocked_until > now:
        remaining_seconds = int((blocked_until - now).total_seconds())
        return _("download/rate_limited_mt", retry_hours=max(1, (remaining_seconds + 3599) // 3600))

    if blocked_until and blocked_until <= now:
        await user.set({User.rate_limited_until: None})

    recent_requests_count = await _rate_limited_requests(user_id, recent_window_start).count()
    if recent_requests_count >= RATE_LIMIT_REQUESTS_PER_HOUR:
        await user.set({User.rate_limited_until: now + RATE_LIMIT_BLOCK_DURATION})
        return _("download/rate_limited_mt", retry_hours=int(RATE_LIMIT_BLOCK_DURATION.total_seconds() // 3600))

    return None


async def _get_or_create_db_user(user: User | None, user_id: int) -> User:
    db_user = user or await User.get(user_id)
    if db_user is None:
        db_user = User(_id=user_id)
        await db_user.save()
    return db_user


async def _get_quota_if_limited(user_id: int, access: premium.Access) -> premium.QuotaStatus | None:
    if access.unlimited:
        return None
    return await premium.get_quota_status(user_id)


@router.message(Command("start"))
async def start_handler(message: Message, _: Callable[..., str]) -> None:
    """Show language picker on /start."""
    await message.answer(
        _("start/choose_language_mt"),
        reply_markup=_build_language_keyboard(),
    )


@router.message(Command("help"))
async def help_handler(message: Message, _: Callable[..., str]) -> None:
    """Show user-facing help."""
    await message.answer(
        _(
            "help/mt",
            bot_name=f"@{settings.bot_name.lstrip('@')}",
            free_limit=settings.free_daily_downloads,
            free_mb=premium.free_max_mb(),
        ),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith(LANG_CALLBACK_PREFIX))
async def choose_language_handler(callback: CallbackQuery, user: User) -> None:
    """Save selected language from inline keyboard."""
    data = callback.data or ""
    selected_language = data.removeprefix(LANG_CALLBACK_PREFIX).strip().lower()

    available = localization.available_languages
    if selected_language not in available:
        current_language = user.language if user.language in available else settings.default_language
        await callback.answer(
            localization.get_text(current_language, "start/unsupported_language_mt"),
            show_alert=True,
        )
        return

    await user.set({
        User.language: selected_language,
        User.language_code: selected_language,
        User.visited: datetime.datetime.now(datetime.UTC),
    })

    confirmation = localization.get_text(
        selected_language,
        "start/language_saved_mt",
        language_text=available[selected_language],
    )

    if callback.message:
        await callback.message.edit_text(confirmation)
    await callback.answer()


async def _fetch_meta(
    url: str,
    nats_client: NatsClient,
    *,
    user_id: int,
    message_id: int,
    language: str,
) -> dict | None:
    """YT-API meta (title, duration, per-format sizes); None when unavailable."""
    try:
        meta = await request_social_meta(
            url,
            nats_client,
            user_id=user_id,
            message_id=message_id,
            user_language=language,
        )
    except (TimeoutError, NoRespondersError) as exc:
        logger.warning(f"Meta request timed out for {url}: {exc!r}")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Meta request failed for {url}: {exc!r}")
        return None
    return meta if isinstance(meta, dict) else None


@router.message(F.text)
async def youtube_link_handler(
    message: Message,
    nats_client: NatsClient,
    language: str,
    _: Callable[..., str],
    user: User | None = None,
) -> None:
    """Reply to a YouTube URL with the quality picker built from meta and cache."""
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    youtube_url = _extract_youtube_url(text)
    video_id = extract_youtube_video_id(youtube_url) if youtube_url else None
    if not video_id or not YOUTUBE_VIDEO_ID_RE.match(video_id):
        await message.answer(_("download/send_youtube_link_mt"))
        return

    if not message.from_user:
        return
    user_id = message.from_user.id

    db_user = await _get_or_create_db_user(user, user_id)
    rate_limited_text = await _get_rate_limit_text(db_user, user_id, _)
    if rate_limited_text:
        await message.answer(rate_limited_text)
        return
    access = premium.get_access(db_user)
    url = _build_video_url(video_id)

    # Cached meta: answer with the picker right away, no "getting metadata" step.
    meta = await meta_cache.get_cached_meta(video_id)
    if meta is not None:
        cached_sizes, quota = await asyncio.gather(
            get_cached_sizes(url, DOWNLOAD_QUALITIES),
            _get_quota_if_limited(user_id, access),
        )
        await message.reply(
            _build_picker_text(meta, _, access, quota),
            reply_markup=_build_quality_keyboard(video_id, build_quality_options(meta, cached_sizes), _, access, quota),
            disable_web_page_preview=True,
        )
        return

    loading_text = _("download/request_created_mt")
    picker = await message.reply(
        loading_text,
        parse_mode=None,
        entities=_build_request_created_entities(loading_text),
    )

    meta, cached_sizes, quota = await asyncio.gather(
        _fetch_meta(url, nats_client, user_id=user_id, message_id=picker.message_id, language=language),
        get_cached_sizes(url, DOWNLOAD_QUALITIES),
        _get_quota_if_limited(user_id, access),
    )
    await meta_cache.save_meta(video_id, meta)

    fatal_error = describe_fatal_meta_error(meta, language) if meta else None
    if fatal_error and not cached_sizes:
        await picker.edit_text(
            _("download/failed_before_progress_mt", error_message=fatal_error),
            parse_mode=None,
        )
        return

    await picker.edit_text(
        _build_picker_text(meta, _, access, quota),
        reply_markup=_build_quality_keyboard(video_id, build_quality_options(meta, cached_sizes), _, access, quota),
        disable_web_page_preview=True,
    )


# --- Access checks and paywall ------------------------------------------------

@dataclass(slots=True, frozen=True)
class _Decision:
    grant: premium.Grant | None  # None: locked, show the paywall
    reason: str | None = None
    quota: premium.QuotaStatus | None = None
    credits_left: int | None = None  # set when a single credit was spent


async def _authorize(
    *,
    db_user: User,
    user_id: int,
    action: str,
    url: str,
    over_free_size: bool,
) -> _Decision:
    """Decide whether the action runs free, on premium, on a single credit, or is locked."""
    access = premium.get_access(db_user)
    if access.unlimited:
        return _Decision(premium.unlimited_grant(access))

    reason = premium.static_lock_reason(action, over_free_size)
    quota = None
    if reason is None:
        cached = await get_cached_sizes(url, (action,))
        if action in cached:
            # Cache hits don't count toward the daily limit.
            return _Decision(premium.GRANT_FREE)
        quota = await premium.get_quota_status(user_id)
        if not quota.exhausted:
            return _Decision(premium.GRANT_FREE, quota=quota)
        reason = premium.REASON_QUOTA

    credits_left = await premium.consume_credit(user_id)
    if credits_left is not None:
        return _Decision(premium.GRANT_CREDIT, reason=reason, credits_left=credits_left)
    return _Decision(None, reason=reason, quota=quota)


async def _send_paywall(
    reply_to: Message,
    *,
    db_user: User,
    decision: _Decision,
    action: str,
    video_id: str,
    language: str,
    _: Callable[..., str],
) -> None:
    quota_wait = ""
    quota = decision.quota
    if quota and quota.resets_at:
        quota_wait = premium.format_wait(quota.resets_at - datetime.datetime.now(datetime.UTC), _)
    text = "\n".join([
        _(
            f"premium/locked_{decision.reason}_mt",
            what=premium.action_label(action, language),
            free_mb=premium.free_max_mb(),
            free_limit=settings.free_daily_downloads,
            wait=quota_wait,
        ).strip(),
        "",
        premium.build_benefits_text(_),
    ])
    try:
        keyboard = await premium.build_buy_keyboard(
            reply_to.bot, language, user=db_user, single=(action, video_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to create invoice links: {exc}")
        keyboard = None
    await reply_to.reply(text, reply_markup=keyboard, disable_web_page_preview=True)


async def _authorize_or_paywall(
    *,
    reply_to: Message,
    db_user: User,
    user_id: int,
    action: str,
    video_id: str,
    over_free_size: bool,
    title: str,
    language: str,
    _: Callable[..., str],
) -> premium.Grant | None:
    """Grant for the action, or None after showing the paywall."""
    decision = await _authorize(
        db_user=db_user,
        user_id=user_id,
        action=action,
        url=_build_video_url(video_id),
        over_free_size=over_free_size,
    )
    if decision.grant is None:
        premium.remember_pending_action(user_id, action, video_id, title)
        await _send_paywall(
            reply_to,
            db_user=db_user,
            decision=decision,
            action=action,
            video_id=video_id,
            language=language,
            _=_,
        )
        return None
    if decision.credits_left is not None:
        await reply_to.answer(_("download/credit_used_mt", left=decision.credits_left))
    return decision.grant


# --- Video / audio download -----------------------------------------------------

@router.callback_query(F.data.startswith(QUALITY_CALLBACK_PREFIX))
async def quality_selected_handler(
    callback: CallbackQuery,
    nats_client: NatsClient,
    language: str,
    _: Callable[..., str],
    user: User | None = None,
) -> None:
    """Check access, then start the download in the selected quality."""
    parsed = _parse_quality_callback(callback.data or "")
    picker = callback.message
    if parsed is None or not isinstance(picker, Message):
        await callback.answer(_("download/picker_expired_mt"), show_alert=True)
        return
    quality, video_id, over_free_size = parsed
    user_id = callback.from_user.id

    picker_key = (picker.chat.id, picker.message_id)
    if picker_key in _pickers_in_progress:
        await callback.answer(_("download/already_requested_mt"))
        return
    _pickers_in_progress.add(picker_key)
    try:
        already_requested = await DownloadRequest.find_one(
            DownloadRequest.user_id == user_id,
            DownloadRequest.message_id == picker.message_id,
        )
        if already_requested:
            await callback.answer(_("download/already_requested_mt"))
            return
        await callback.answer()

        db_user = await _get_or_create_db_user(user, user_id)
        rate_limited_text = await _get_rate_limit_text(db_user, user_id, _)
        if rate_limited_text:
            await picker.edit_text(rate_limited_text)
            return

        grant = await _authorize_or_paywall(
            reply_to=picker,
            db_user=db_user,
            user_id=user_id,
            action=quality,
            video_id=video_id,
            over_free_size=over_free_size,
            title=_title_from_picker(picker),
            language=language,
            _=_,
        )
        if grant is None:
            return

        await _start_download(
            picker=picker,
            nats_client=nats_client,
            user_id=user_id,
            url=_build_video_url(video_id),
            quality=quality,
            language=language,
            _=_,
            grant=grant,
        )
    finally:
        _pickers_in_progress.discard(picker_key)


async def _fail_request(request: DownloadRequest, *, error_code: str, error_message: str) -> None:
    request.status = "failed"
    request.terminal = True
    request.error_code = error_code
    request.error_message = error_message
    request.updated_at = datetime.datetime.now(datetime.UTC)
    await request.save()


async def _return_credit(request: DownloadRequest, reply_to: Message, _: Callable[..., str]) -> None:
    try:
        if await premium.return_request_credit(request):
            await reply_to.answer(_("download/credit_returned_mt"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to return single credit for request_id={request.request_id}: {exc}")


async def _start_download(
    *,
    picker: Message,
    nats_client: NatsClient,
    user_id: int,
    url: str,
    quality: str,
    language: str,
    _: Callable[..., str],
    grant: premium.Grant = premium.GRANT_FREE,
) -> None:
    """Serve from cache or queue a download; the picker message becomes the status message."""
    sent_from_cache = await try_send_cached_media_for_url(
        bot=picker.bot,
        user_id=user_id,
        url=url,
        quality=quality,
    )
    if sent_from_cache:
        try:
            await _save_cache_hit_request(
                url=url,
                quality=quality,
                user_id=user_id,
                message_id=picker.message_id,
                language=language,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to save cache_hit request: {exc}")
        try:
            await picker.delete()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to delete quality picker after cache hit: {exc}")
        if not grant.no_ads:
            await send_post_delivery_advert(picker.bot, user_id)
        return

    request = DownloadRequest(
        url=url,
        quality=quality,
        bot_name=settings.bot_name,
        language=language,
        user_language=language,
        user_id=user_id,
        message_id=picker.message_id,
        status="created",
        premium=grant.no_ads,
        counts_to_quota=grant.counts_to_quota,
        paid_credit=grant.paid_credit,
        priority=grant.priority,
    )
    await request.insert()

    # Editing without reply_markup also removes the quality keyboard.
    request_created_text = _("download/request_created_mt", request_id=request.request_id)
    await picker.edit_text(
        request_created_text,
        parse_mode=None,
        entities=_build_request_created_entities(request_created_text),
    )

    request.status = "queued"
    request.updated_at = datetime.datetime.now(datetime.UTC)
    await request.save()

    try:
        ack = await request_media_download(
            url,
            nats_client,
            user_id=user_id,
            message_id=picker.message_id,
            quality=quality,
            user_language=language,
            language=language,
            request_id=request.request_id,
        )
    except (TimeoutError, NoRespondersError):
        await _fail_request(
            request,
            error_code="nats_no_responders",
            error_message="NATS timeout or no responders on manager_download subject",
        )
        await picker.edit_text(_("download/queue_failed_mt", request_id=request.request_id))
        await _return_credit(request, picker, _)
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to request {quality} download: {exc}")
        await _fail_request(request, error_code="request_failed", error_message=str(exc))
        await picker.edit_text(_("download/start_failed_mt", request_id=request.request_id))
        await _return_credit(request, picker, _)
        return

    await _apply_download_ack(request, ack)


async def _apply_download_ack(request: DownloadRequest, ack: dict) -> None:
    """Store the ACK without a full save: a fast status update (e.g. busy
    servers -> failed) may already be in the DB and must not be overwritten."""
    now = datetime.datetime.now(datetime.UTC)
    ack_status = str(ack.get("status", "")).strip().lower() or "queued"
    collection = DownloadRequest.get_pymongo_collection()
    await collection.update_one({"_id": request.id}, {"$set": {"nats_ack": ack, "updated_at": now}})
    await collection.update_one(
        {"_id": request.id, "status": "queued"},
        {"$set": {"status": ack_status, "terminal": ack_status in {"completed", "failed"}}},
    )


# --- Transcript -----------------------------------------------------------------

@router.callback_query(F.data.startswith(TRANSCRIPT_CALLBACK_PREFIX))
async def transcript_selected_handler(
    callback: CallbackQuery,
    nats_client: NatsClient,
    language: str,
    _: Callable[..., str],
    user: User | None = None,
) -> None:
    """Send the video transcript as a .txt file; the picker stays usable."""
    video_id = (callback.data or "").removeprefix(TRANSCRIPT_CALLBACK_PREFIX)
    picker = callback.message
    if not YOUTUBE_VIDEO_ID_RE.match(video_id) or not isinstance(picker, Message):
        await callback.answer(_("download/picker_expired_mt"), show_alert=True)
        return
    user_id = callback.from_user.id

    picker_key = (picker.chat.id, picker.message_id, ACTION_TRANSCRIPT)
    if picker_key in _pickers_in_progress:
        await callback.answer(_("download/already_requested_mt"))
        return
    _pickers_in_progress.add(picker_key)
    try:
        await callback.answer()
        db_user = await _get_or_create_db_user(user, user_id)
        rate_limited_text = await _get_rate_limit_text(db_user, user_id, _)
        if rate_limited_text:
            await picker.reply(rate_limited_text)
            return

        title = _title_from_picker(picker)
        grant = await _authorize_or_paywall(
            reply_to=picker,
            db_user=db_user,
            user_id=user_id,
            action=ACTION_TRANSCRIPT,
            video_id=video_id,
            over_free_size=False,
            title=title,
            language=language,
            _=_,
        )
        if grant is None:
            return

        status = await picker.reply(_("download/transcript_loading_mt"))
        await _run_transcript(
            status=status,
            nats_client=nats_client,
            user_id=user_id,
            video_id=video_id,
            title=title,
            language=language,
            _=_,
            grant=grant,
        )
    finally:
        _pickers_in_progress.discard(picker_key)


def _transcript_filename(title: str, video_id: str) -> str:
    name = FILENAME_UNSAFE_RE.sub(" ", title).strip()[:80].strip()
    return f"{name or 'transcript_' + video_id}.txt"


async def _run_transcript(
    *,
    status: Message,
    nats_client: NatsClient,
    user_id: int,
    video_id: str,
    title: str,
    language: str,
    _: Callable[..., str],
    grant: premium.Grant,
) -> None:
    """Fetch the transcript into the `status` message, then replace it with the file."""
    url = _build_video_url(video_id)
    request = DownloadRequest(
        url=url,
        quality=ACTION_TRANSCRIPT,
        bot_name=settings.bot_name,
        language=language,
        user_language=language,
        user_id=user_id,
        message_id=status.message_id,
        status="transcript",
        premium=grant.no_ads,
        paid_credit=grant.paid_credit,
    )
    await request.insert()

    try:
        result = await request_transcript(video_id, nats_client, language=language)
    except (TimeoutError, NoRespondersError) as exc:
        logger.warning(f"Transcript request timed out for {video_id}: {exc!r}")
        result = {"result": 0, "error_code": 307, "error": "transcript timeout"}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Transcript request failed for {video_id}: {exc!r}")
        result = {"result": 0, "error_code": 306, "error": str(exc)}

    transcript = str(result.get("transcript_text") or "").strip() if result.get("result") == "ok" else ""
    if not transcript:
        error_payload = result if result.get("error_code") else {**result, "error_code": 306}
        await _fail_request(
            request,
            error_code=str(error_payload.get("error_code")),
            error_message=str(error_payload.get("error") or "empty transcript"),
        )
        await status.edit_text(_(
            "download/transcript_failed_mt",
            error_message=html.escape(describe_error(error_payload, language), quote=False),
        ))
        await _return_credit(request, status, _)
        return

    header = f"{title}\n{url}" if title else url
    document = BufferedInputFile(f"{header}\n\n{transcript}\n".encode(), filename=_transcript_filename(title, video_id))
    try:
        bot_username = (await status.bot.me()).username or ""
    except Exception:  # noqa: BLE001
        bot_username = settings.bot_name
    try:
        await status.answer_document(
            document,
            caption=_(
                "download/transcript_caption_mt",
                title=html.escape(title, quote=False),
                bot=f"@{bot_username.lstrip('@')}",
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to send transcript for {video_id}: {exc}")
        await _fail_request(request, error_code="send_failed", error_message=str(exc))
        await status.edit_text(_(
            "download/transcript_failed_mt",
            error_message=html.escape(describe_error({"error_code": 306}, language), quote=False),
        ))
        await _return_credit(request, status, _)
        return

    now = datetime.datetime.now(datetime.UTC)
    request.status = "completed"
    request.terminal = True
    request.media_sent = True
    request.delivered_at = now
    request.updated_at = now
    await request.save()
    try:
        await status.delete()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to delete transcript status message: {exc}")
    if not grant.no_ads:
        await send_post_delivery_advert(status.bot, user_id)


# --- After a payment ------------------------------------------------------------

async def start_action_after_payment(
    *,
    bot: Bot,
    nats_client: NatsClient,
    user_id: int,
    action: str,
    video_id: str,
    title: str,
    language: str,
    _: Callable[..., str],
    use_credit: bool,
) -> None:
    """Run the action the user paid for, in a new status message.

    A single purchase always spends its credit here (the payload doesn't carry
    the over-500 MB flag, so the free-tier check can't be repeated reliably).
    """
    db_user = await User.get(user_id)
    if db_user is None:
        return
    rate_limited_text = await _get_rate_limit_text(db_user, user_id, _)
    if rate_limited_text:
        await bot.send_message(user_id, rate_limited_text)
        return

    access = premium.get_access(db_user)
    if access.unlimited:
        grant = premium.unlimited_grant(access)
    elif use_credit and await premium.consume_credit(user_id) is not None:
        grant = premium.GRANT_CREDIT
    else:
        return

    if action == ACTION_TRANSCRIPT:
        status = await bot.send_message(user_id, _("download/transcript_loading_mt"))
        await _run_transcript(
            status=status,
            nats_client=nats_client,
            user_id=user_id,
            video_id=video_id,
            title=title,
            language=language,
            _=_,
            grant=grant,
        )
        return

    loading_text = _("download/request_created_mt")
    status = await bot.send_message(
        user_id,
        loading_text,
        parse_mode=None,
        entities=_build_request_created_entities(loading_text),
    )
    await _start_download(
        picker=status,
        nats_client=nats_client,
        user_id=user_id,
        url=_build_video_url(video_id),
        quality=action,
        language=language,
        _=_,
        grant=grant,
    )
