"""User handlers: TikTok link -> video or photos (and the old 🎵 button -> the post's sound)."""
from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, MessageEntity
from aiogram.utils.keyboard import InlineKeyboardBuilder
from nats.aio.client import Client as NatsClient
from nats.errors import NoRespondersError, TimeoutError

from app.config import settings
from app.constants import QUALITY_AUDIO, QUALITY_PHOTOS, QUALITY_VIDEO
from app.database import DownloadRequest, User
from app.locales import localization
from app.services import premium, request_media_download
from app.services.media_cache import AUDIO_CALLBACK_PREFIX, try_send_cached
from app.services.nats_meta import send_post_delivery_advert
from app.utils import get_logger
from app.utils.tiktok import POST_ID_RE, canonical_url, extract_post_id, find_tiktok_url, is_short_url, resolve_post_id

router = Router(name="user")
# Groups are not supported: the catch-all text handler would answer every message.
router.message.filter(F.chat.type == ChatType.PRIVATE)
logger = get_logger(__name__)

LANG_CALLBACK_PREFIX = "setlang:"
LANG_ORDER = ("uk", "en", "ru")
# Free users can't reach this through the daily quota (FREE_DAILY_DOWNLOADS);
# it stops scripts that hammer the bot with links.
RATE_LIMIT_REQUESTS_PER_HOUR = 40
# Premium and admins: a ceiling against scripts and shared accounts, without the long block.
PREMIUM_RATE_LIMIT_REQUESTS_PER_HOUR = 200
RATE_LIMIT_WINDOW = datetime.timedelta(hours=1)
RATE_LIMIT_BLOCK_DURATION = datetime.timedelta(hours=6)
REQUEST_CREATED_EMOJI = "👨‍💻"
REQUEST_CREATED_EMOJI_UTF16_LENGTH = 5
REQUEST_CREATED_CUSTOM_EMOJI_ID = "5413623448440160154"

# (user, post, quality) requests being set up right now: a double tap or the
# same link sent twice must not start two downloads.
_requests_in_progress: set[tuple[int, str, str]] = set()


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


async def _send_request_created(bot: Bot, chat_id: int, _: Callable[..., str], reply_to: int | None = None) -> Message:
    text = _("download/request_created_mt")
    return await bot.send_message(
        chat_id,
        text,
        parse_mode=None,
        entities=_build_request_created_entities(text),
        reply_to_message_id=reply_to,
    )


async def _save_cache_hit_request(
    *,
    url: str,
    post_id: str,
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
        site_id=post_id,
        media_type=quality,
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


@router.message(F.text)
async def tiktok_link_handler(
    message: Message,
    nats_client: NatsClient,
    language: str,
    _: Callable[..., str],
    user: User | None = None,
) -> None:
    """A TikTok link: send the video (or the photos of a photo post)."""
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    url = find_tiktok_url(text)
    if not url:
        await message.answer(_("download/send_tiktok_link_mt"))
        return
    if not message.from_user:
        return
    user_id = message.from_user.id

    db_user = await _get_or_create_db_user(user, user_id)
    rate_limited_text = await _get_rate_limit_text(db_user, user_id, _)
    if rate_limited_text:
        await message.answer(rate_limited_text)
        return

    if is_short_url(url):
        # Following a short link takes an HTTP round trip; show activity
        # without a message that a cache hit would delete right away.
        await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)
    post_id = await resolve_post_id(url)
    # Full links go as sent; a resolved short link goes as a plain post URL,
    # an unresolved one as sent (the worker follows it itself).
    download_url = url if extract_post_id(url) else (canonical_url(post_id) if post_id else url)
    await _process_request(
        bot=message.bot,
        chat_id=message.chat.id,
        reply_to=message.message_id,
        nats_client=nats_client,
        db_user=db_user,
        user_id=user_id,
        url=download_url,
        post_id=post_id,
        quality=QUALITY_VIDEO,
        language=language,
        _=_,
    )


@router.callback_query(F.data.startswith(AUDIO_CALLBACK_PREFIX))
async def audio_button_handler(
    callback: CallbackQuery,
    nats_client: NatsClient,
    language: str,
    _: Callable[..., str],
    user: User | None = None,
) -> None:
    """🎵 under videos delivered before 2026-10-26: send the post's sound."""
    post_id = (callback.data or "").removeprefix(AUDIO_CALLBACK_PREFIX)
    if not POST_ID_RE.match(post_id):
        await callback.answer(_("download/button_expired_mt"), show_alert=True)
        return
    user_id = callback.from_user.id
    await callback.answer()

    db_user = await _get_or_create_db_user(user, user_id)
    rate_limited_text = await _get_rate_limit_text(db_user, user_id, _)
    if rate_limited_text:
        await callback.bot.send_message(user_id, rate_limited_text)
        return

    await _process_request(
        bot=callback.bot,
        chat_id=user_id,
        reply_to=callback.message.message_id if isinstance(callback.message, Message) else None,
        nats_client=nats_client,
        db_user=db_user,
        user_id=user_id,
        url=canonical_url(post_id),
        post_id=post_id,
        quality=QUALITY_AUDIO,
        language=language,
        _=_,
    )


async def _process_request(
    *,
    bot: Bot,
    chat_id: int,
    reply_to: int | None,
    nats_client: NatsClient,
    db_user: User,
    user_id: int,
    url: str,
    post_id: str | None,
    quality: str,
    language: str,
    _: Callable[..., str],
) -> None:
    """Cache first (free and instant), then the daily limit or paywall, then a download.

    A cache hit sends the media and nothing else. Otherwise a "request created"
    message (a reply to `reply_to`) becomes the download status or the paywall.
    """
    key = (user_id, post_id or url, quality)
    if key in _requests_in_progress:
        await bot.send_message(chat_id, _("download/already_requested_mt"), reply_to_message_id=reply_to)
        return
    _requests_in_progress.add(key)
    try:
        if post_id:
            # A video request may turn out to be a photo post: either is a hit.
            qualities = (QUALITY_AUDIO,) if quality == QUALITY_AUDIO else (QUALITY_VIDEO, QUALITY_PHOTOS)
            sent_quality = await try_send_cached(
                bot,
                user_id=user_id,
                post_id=post_id,
                qualities=qualities,
            )
            if sent_quality:
                await _finish_cache_hit(
                    bot=bot,
                    db_user=db_user,
                    user_id=user_id,
                    url=url,
                    post_id=post_id,
                    quality=sent_quality,
                    message_id=reply_to or 0,
                    language=language,
                )
                return

        status = await _send_request_created(bot, chat_id, _, reply_to=reply_to)
        grant = await _authorize_or_paywall(
            status=status,
            db_user=db_user,
            user_id=user_id,
            action=quality,
            post_id=post_id or "",
            language=language,
            _=_,
        )
        if grant is None:
            return
        await _start_download(
            status=status,
            nats_client=nats_client,
            user_id=user_id,
            url=url,
            post_id=post_id,
            quality=quality,
            language=language,
            _=_,
            grant=grant,
        )
    finally:
        _requests_in_progress.discard(key)


async def _finish_cache_hit(
    *,
    bot: Bot,
    db_user: User,
    user_id: int,
    url: str,
    post_id: str,
    quality: str,
    message_id: int,
    language: str,
) -> None:
    try:
        await _save_cache_hit_request(
            url=url,
            post_id=post_id,
            quality=quality,
            user_id=user_id,
            message_id=message_id,
            language=language,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to save cache_hit request: {exc}")
    if not premium.get_access(db_user).premium:
        await send_post_delivery_advert(bot, user_id)


# --- Access checks and paywall ------------------------------------------------

@dataclass(slots=True, frozen=True)
class _Decision:
    grant: premium.Grant | None  # None: locked, show the paywall
    quota: premium.QuotaStatus | None = None
    credits_left: int | None = None  # set when a single credit was spent


async def _authorize(*, db_user: User, user_id: int) -> _Decision:
    """Decide whether a download runs free, on premium, on a single credit, or is locked.

    Only the daily quota limits free users; cache hits never get here.
    """
    access = premium.get_access(db_user)
    if access.unlimited:
        return _Decision(premium.unlimited_grant(access))

    quota = await premium.get_quota_status(user_id)
    if not quota.exhausted:
        return _Decision(premium.GRANT_FREE, quota=quota)

    credits_left = await premium.consume_credit(user_id)
    if credits_left is not None:
        return _Decision(premium.GRANT_CREDIT, credits_left=credits_left)
    return _Decision(None, quota=quota)


async def _authorize_or_paywall(
    *,
    status: Message,
    db_user: User,
    user_id: int,
    action: str,
    post_id: str,
    language: str,
    _: Callable[..., str],
) -> premium.Grant | None:
    """Grant for the download, or None after turning `status` into the paywall."""
    decision = await _authorize(db_user=db_user, user_id=user_id)
    if decision.grant is not None:
        if decision.credits_left is not None:
            await status.answer(_("download/credit_used_mt", left=decision.credits_left))
        return decision.grant

    quota_wait = ""
    if decision.quota and decision.quota.resets_at:
        quota_wait = premium.format_wait(decision.quota.resets_at - datetime.datetime.now(datetime.UTC), _)
    text = "\n".join([
        _(
            f"premium/locked_{premium.REASON_QUOTA}_mt",
            free_limit=settings.free_daily_downloads,
            wait=quota_wait,
        ).strip(),
        "",
        premium.build_benefits_text(_),
    ])
    keyboard = None
    # A single purchase needs the post id to start the download after paying.
    single = (action, post_id) if post_id else None
    if post_id:
        premium.remember_pending_action(user_id, action, post_id)
    try:
        keyboard = await premium.build_buy_keyboard(status.bot, language, user=db_user, single=single)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to create invoice links: {exc}")
    await status.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    return None


# --- Download -------------------------------------------------------------------

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
    status: Message,
    nats_client: NatsClient,
    user_id: int,
    url: str,
    post_id: str | None,
    quality: str,
    language: str,
    _: Callable[..., str],
    grant: premium.Grant = premium.GRANT_FREE,
) -> None:
    """Queue a download in YT-API; the `status` message shows its progress."""
    request = DownloadRequest(
        url=url,
        quality=quality,
        site_id=post_id,
        bot_name=settings.bot_name,
        language=language,
        user_language=language,
        user_id=user_id,
        message_id=status.message_id,
        status="queued",
        premium=grant.no_ads,
        counts_to_quota=grant.counts_to_quota,
        paid_credit=grant.paid_credit,
        priority=grant.priority,
    )
    await request.insert()

    try:
        ack = await request_media_download(
            url,
            nats_client,
            user_id=user_id,
            message_id=status.message_id,
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
        await status.edit_text(_("download/queue_failed_mt", request_id=request.request_id))
        await _return_credit(request, status, _)
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to request {quality} download: {exc}")
        await _fail_request(request, error_code="request_failed", error_message=str(exc))
        await status.edit_text(_("download/start_failed_mt", request_id=request.request_id))
        await _return_credit(request, status, _)
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


# --- After a payment ------------------------------------------------------------

async def start_action_after_payment(
    *,
    bot: Bot,
    nats_client: NatsClient,
    user_id: int,
    action: str,
    video_id: str,
    title: str,  # noqa: ARG001 - kept for the payments handler's call
    language: str,
    _: Callable[..., str],
    use_credit: bool,
) -> None:
    """Run the download the user paid for, in a new status message.

    `video_id` is the TikTok post id from the paywall's invoice payload.
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

    status = await _send_request_created(bot, user_id, _)
    await _start_download(
        status=status,
        nats_client=nats_client,
        user_id=user_id,
        url=canonical_url(video_id),
        post_id=video_id,
        quality=action,
        language=language,
        _=_,
        grant=grant,
    )
