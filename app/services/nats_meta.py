"""Helpers for requesting social media metadata over NATS."""
from __future__ import annotations

import asyncio
import datetime
import html
import re
import secrets
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import orjson
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, MessageEntity
from beanie import PydanticObjectId
from nats.aio.client import Client as NatsClient

from app.config import settings
from app.constants import QUALITY_AUDIO
from app.database import DownloadRequest, User
from app.locales import localization
from app.services.media_cache import cache_track_from_channel_post
from app.services.meta_cache import invalidate_meta_for_url
from app.services.premium import return_request_credit
from app.utils import get_logger
from app.utils.youtube import extract_youtube_video_id, is_youtube_host

logger = get_logger(__name__)

TRACKING_CAPTION_PREFIX = "YTB"
TRACKING_CAPTION_VERSION = "v1"
REQUEST_CREATED_EMOJI = "👨‍💻"
REQUEST_CREATED_EMOJI_UTF16_LENGTH = 5
REQUEST_CREATED_CUSTOM_EMOJI_ID = "5413623448440160154"
MEDIA_READY_STATUSES = {
    "uploaded_to_telegram",
    "media_uploaded",
    "uploaded",
    "completed",
}
DOWNLOAD_STARTED_STATUSES = {
    "downloaded",
    "processing_optimization",
    "uploading_to_telegram",
    "uploaded_to_telegram",
    "media_uploaded",
    "uploaded",
    "completed",
}
ERROR_CODE_ALIASES = {
    "301": "301",
    "MAX_WORKER_TASKS": "301",
    "302": "302",
    "DOWNLOAD_FAILED": "302",
    "303": "303",
    "META_FAILED": "303",
    "304": "304",
    "META_TIMEOUT": "304",
    "305": "305",
    "DOWNLOAD_TIMEOUT": "305",
    "306": "306",
    "TRANSCRIPT_FAILED": "306",
    "307": "307",
    "TRANSCRIPT_TIMEOUT": "307",
    "308": "308",
    "TRANSCRIPT_NOT_AVAILABLE": "308",
    "310": "310",
    "UPLOAD_FAILED": "310",
    "311": "311",
    "UPLOAD_CHANNELS_EXHAUSTED": "311",
    "312": "312",
    "UPLOADER_TIMEOUT": "312",
    "401": "401",
    "INTERNAL_ERROR": "401",
    "402": "402",
    "SOURCE_BLOCKED": "402",
    "403": "403",
    "WORKER_UNAVAILABLE": "403",
    "404": "404",
    "NO_HEALTHY_WORKERS": "404",
    "405": "405",
    "VPN_BLOCKED": "405",
    "406": "406",
    "WORKER_RESTARTING": "406",
    "407": "407",
    "YOUTUBE_TAB_LINK": "407",
    "408": "408",
    "YOUTUBE_LIVE_STREAM": "408",
}
ERROR_CODE_LOCALE_KEYS = {
    "301": "download_status/error_code_301_mt",
    "302": "download_status/error_code_302_mt",
    "303": "download_status/error_code_303_mt",
    "304": "download_status/error_code_304_mt",
    "305": "download_status/error_code_305_mt",
    "306": "download_status/error_code_306_mt",
    "307": "download_status/error_code_307_mt",
    "308": "download_status/error_code_308_mt",
    "310": "download_status/error_code_310_mt",
    "311": "download_status/error_code_311_mt",
    "312": "download_status/error_code_312_mt",
    "401": "download_status/error_code_401_mt",
    "402": "download_status/error_code_402_mt",
    "403": "download_status/error_code_403_mt",
    "404": "download_status/error_code_404_mt",
    "405": "download_status/error_code_405_mt",
    "406": "download_status/error_code_406_mt",
    "407": "download_status/error_code_407_mt",
    "408": "download_status/error_code_408_mt",
}


def _extract_source_info(url: str) -> tuple[str | None, str | None]:
    """Best-effort extractor detection for known social media URLs."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path.strip("/")

    if is_youtube_host(host):
        video_id = extract_youtube_video_id(url)
        if video_id:
            return "youtube", video_id

    if "soundcloud.com" in host and path:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            return "soundcloud", parts[-1]

    return None, None


def _build_subject() -> str:
    request_hash = secrets.token_urlsafe(8)
    return f"{settings.nats_server.manager_name}_info.{settings.bot_name}.{request_hash}"


def _build_download_subject() -> str:
    return f"{settings.nats_server.manager_name}_download.{settings.bot_name}"


def _build_transcript_subject() -> str:
    request_hash = secrets.token_urlsafe(8)
    return f"{settings.nats_server.manager_name}_transcript.{settings.bot_name}.{request_hash}"


def _build_download_status_subject() -> str:
    return f"download_status.{settings.bot_name}"


def _build_edit_message_subject() -> str:
    return f"edit_message.{settings.bot_name}"


def _build_payload(url: str, user_id: int, message_id: int, user_language: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "url": url,
        "user_id": user_id,
        "message_id": message_id,
        "user_language": user_language,
    }
    extractor, site_id = _extract_source_info(url)
    if extractor and site_id:
        payload["extractor"] = extractor
        payload["site_id"] = site_id
    return payload


def _build_connect_kwargs() -> dict[str, Any]:
    nats_cfg = settings.nats_server
    connect_kwargs: dict[str, Any] = {}
    if nats_cfg.token:
        connect_kwargs["token"] = nats_cfg.token.get_secret_value()
    return connect_kwargs


def _pretty_json(value: Any) -> str:
    """Return human-readable JSON for console logs."""
    try:
        return orjson.dumps(
            value,
            option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
        ).decode("utf-8")
    except Exception:  # noqa: BLE001
        return repr(value)


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_error_code(value: Any) -> str | None:
    if value is None:
        return None

    raw_code = str(value).strip().upper()
    if not raw_code:
        return None

    return ERROR_CODE_ALIASES.get(raw_code, raw_code if raw_code.isdigit() else None)


def _compact_error_message(value: Any, max_length: int = 220) -> str:
    if value is None:
        return ""
    compact = re.sub(r"\s+", " ", str(value)).strip()
    if len(compact) <= max_length:
        return compact
    return f"{compact[:max_length - 1].rstrip()}…"


def _resolve_error_text_for_user(payload: dict[str, Any], language: str) -> tuple[str, str | None]:
    error_code = _normalize_error_code(payload.get("error_code"))
    if error_code and error_code in ERROR_CODE_LOCALE_KEYS:
        return localization.get_text(language, ERROR_CODE_LOCALE_KEYS[error_code]), error_code

    error_message = payload.get("error_message") or payload.get("error")
    if error_message:
        return _compact_error_message(error_message), error_code

    return localization.get_text(language, "download_status/unknown_error_mt"), error_code


def describe_error(payload: dict[str, Any], language: str) -> str:
    """User-facing text for an error payload (error_code / error_message)."""
    error_text, _ = _resolve_error_text_for_user(payload, language)
    return error_text


# "All servers busy" failures. Priority requests (premium, admin, single
# credit) are retried with a fresh request id instead of failing: the manager
# drops every status for a request id that already ended.
BUSY_ERROR_CODES = frozenset({"301", "403", "404", "406"})
PRIORITY_RETRY_WINDOW = datetime.timedelta(minutes=2)
PRIORITY_RETRY_DELAY_SECONDS = 15.0
_retry_tasks: set[asyncio.Task] = set()


def _as_utc(value: datetime.datetime | None) -> datetime.datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value


# Meta errors a download would hit as well (tab/playlist link, live stream),
# so the quality picker is replaced by the error text.
FATAL_META_ERROR_CODES = {"407", "408"}


def describe_fatal_meta_error(payload: dict[str, Any], language: str) -> str | None:
    """User-facing error text when meta failed for a reason a download can't fix."""
    if payload.get("result") == "ok":
        return None
    error_code = _normalize_error_code(payload.get("error_code"))
    if error_code not in FATAL_META_ERROR_CODES:
        return None
    error_text, _ = _resolve_error_text_for_user(payload, language)
    return error_text


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


def _normalize_attempt_info(payload: dict[str, Any]) -> tuple[int, int]:
    attempt = _safe_int(payload.get("attempt"))
    total_attempts = _safe_int(payload.get("total_attempts"))
    if attempt is None or attempt < 1:
        attempt = 1
    if total_attempts is None or total_attempts < attempt:
        total_attempts = attempt
    return attempt, total_attempts


def _build_processing_status_text(payload: dict[str, Any], fallback_language: str | None = None) -> tuple[str, list[MessageEntity] | None]:
    language = _resolve_language(payload, fallback_language=fallback_language)
    attempt, total_attempts = _normalize_attempt_info(payload)

    if attempt <= 1:
        text = localization.get_text(language, "download/request_created_mt")
        return text, _build_request_created_entities(text)

    text = localization.get_text(
        language,
        "download/processing_info_attempt_mt",
        attempt=attempt,
        total_attempts=total_attempts,
    )
    return text, _build_request_created_entities(text)


def _build_failed_before_progress_text(
    payload: dict[str, Any],
    fallback_language: str | None = None,
) -> tuple[str, list[MessageEntity] | None]:
    language = _resolve_language(payload, fallback_language=fallback_language)
    error_text, _ = _resolve_error_text_for_user(payload, language)
    attempt, total_attempts = _normalize_attempt_info(payload)
    text = localization.get_text(
        language,
        "download/failed_before_progress_mt",
        error_message=error_text,
        attempt=attempt,
        total_attempts=total_attempts,
    )
    return text, _build_request_created_entities(text)


def _has_download_started_status(
    status: str | None,
    payload: dict[str, Any],
) -> bool:
    normalized_status = (status or "").strip().lower()
    progress = _safe_int(payload.get("progress"))

    if normalized_status == "failed":
        return True

    if progress is not None and progress > 0:
        return True

    if normalized_status == "downloading":
        return False

    if normalized_status in DOWNLOAD_STARTED_STATUSES:
        return True

    return False


def _is_media_ready_status(status: str | None) -> bool:
    return (status or "").strip().lower() in MEDIA_READY_STATUSES


def _extract_source_pointer(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    """Extract source channel message pointer from status payload."""
    chat_id = _safe_int(
        payload.get("source_chat_id")
        or payload.get("channel_id")
        or payload.get("media_chat_id")
    )
    message_id = _safe_int(
        payload.get("source_message_id")
        or payload.get("channel_message_id")
        or payload.get("media_message_id")
    )
    return chat_id, message_id


def _parse_tracking_caption(caption: str | None) -> dict[str, str] | None:
    """Parse tracking caption like: YTB|v1|rid=...|uid=...|mid=...|q=audio."""
    if not caption:
        return None

    first_line = caption.strip().splitlines()[0].strip()
    if not first_line:
        return None

    parts = [item.strip() for item in first_line.split("|") if item.strip()]
    if len(parts) < 3:
        return None

    prefix = parts[0].upper()
    version = parts[1].lower()
    if prefix != TRACKING_CAPTION_PREFIX or version != TRACKING_CAPTION_VERSION:
        return None

    parsed: dict[str, str] = {}
    for token in parts[2:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            parsed[key] = value

    request_id = parsed.get("rid")
    if not request_id:
        return None

    return parsed


async def _get_request_by_request_id(request_id: str) -> DownloadRequest | None:
    try:
        request_obj_id = PydanticObjectId(str(request_id))
    except Exception:  # noqa: BLE001
        return None
    return await DownloadRequest.get(request_obj_id)


async def _try_acquire_delivery_lock(request: DownloadRequest) -> bool:
    """Atomically mark request as media_sent to prevent duplicate delivery races."""
    if request.id is None:
        return False

    now = datetime.datetime.now(datetime.UTC)
    update_result = await DownloadRequest.find_one(
        {"_id": request.id, "media_sent": {"$ne": True}},
    ).update_one(
        {"$set": {"media_sent": True, "delivered_at": now, "updated_at": now}},
    )
    return bool(update_result and update_result.modified_count == 1)


async def _release_delivery_lock_on_failure(request: DownloadRequest, error_message: str) -> None:
    """Rollback delivery lock when sending media fails."""
    if request.id is None:
        return

    now = datetime.datetime.now(datetime.UTC)
    await DownloadRequest.find_one({"_id": request.id}).update_one(
        {
            "$set": {
                "media_sent": False,
                "updated_at": now,
                "error_code": "copy_message_failed",
                "error_message": error_message,
            },
            "$unset": {"delivered_at": ""},
        },
    )


async def _finalize_delivery_success(request: DownloadRequest) -> None:
    """Persist successful delivery timestamp after lock was acquired."""
    if request.id is None:
        return

    delivered_at = datetime.datetime.now(datetime.UTC)
    await DownloadRequest.find_one({"_id": request.id}).update_one(
        {
            "$set": {
                "media_sent": True,
                "delivered_at": delivered_at,
                "updated_at": delivered_at,
                "error_code": None,
                "error_message": None,
            },
        },
    )


async def _deliver_request_media_to_user(bot: Bot, request: DownloadRequest) -> bool:
    """Copy uploaded media from source channel to request owner."""
    if request.media_sent:
        return False

    if request.source_chat_id is None or request.source_message_id is None:
        return False

    delivery_locked = await _try_acquire_delivery_lock(request)
    if not delivery_locked:
        return False

    bot_username = None
    try:
        bot_info = await bot.get_me()
        bot_username = bot_info.username
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to resolve bot username from Bot API, fallback to settings: {exc}")

    if bot_username:
        delivery_caption = f"@{bot_username.lstrip('@')}"
    else:
        delivery_caption = f"@{settings.bot_name.lstrip('@')}"
    try:
        await bot.copy_message(
            chat_id=request.user_id,
            from_chat_id=request.source_chat_id,
            message_id=request.source_message_id,
            caption=delivery_caption,
        )
    except TelegramBadRequest as first_exc:
        # Fallback for media types that don't support captions (e.g. video_note).
        logger.warning(
            "Failed to copy media with caption for request_id=%s, retrying without caption: %s",
            request.request_id,
            first_exc,
        )
        try:
            await bot.copy_message(
                chat_id=request.user_id,
                from_chat_id=request.source_chat_id,
                message_id=request.source_message_id,
            )
        except TelegramBadRequest as exc:
            await _release_delivery_lock_on_failure(request, str(exc))
            logger.warning(f"Failed to copy media for request_id={request.request_id}: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001
            await _release_delivery_lock_on_failure(request, str(exc))
            logger.warning(f"Unexpected copy_message failure for request_id={request.request_id}: {exc}")
            return False
    except Exception as exc:  # noqa: BLE001
        await _release_delivery_lock_on_failure(request, str(exc))
        logger.warning(f"Unexpected copy_message failure for request_id={request.request_id}: {exc}")
        return False

    await _finalize_delivery_success(request)
    request.media_sent = True
    request.delivered_at = datetime.datetime.now(datetime.UTC)
    request.updated_at = request.delivered_at
    logger.info(
        "Delivered media to user: request_id=%s from %s:%s to user_id=%s",
        request.request_id,
        request.source_chat_id,
        request.source_message_id,
        request.user_id,
    )

    if not request.premium:
        await send_post_delivery_advert(bot, request.user_id)
    return True


async def send_post_delivery_advert(bot: Bot, user_id: int) -> None:
    """Try to send action advert after successful media delivery.

    Priority:
    1) BotAdsLib action advert
    2) Legacy advert API fallback

    Premium users never get one.
    """
    try:
        user = await User.get(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to load user {user_id} for advert premium check: {exc}")
        user = None
    if user is not None and user.is_premium:
        return

    botads_sent = False
    broadcast_service = None
    try:
        from app.services.broadcast import BroadcastService

        broadcast_service = BroadcastService(bot)
        botads_sent = await broadcast_service.send_action_advert(user_id, placement="after_result")
        if botads_sent:
            logger.info(f"Post-delivery BotAds advert sent to user_id={user_id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to send BotAds action advert to user_id={user_id}: {exc}")
    finally:
        if broadcast_service is not None:
            try:
                await broadcast_service.close()
            except Exception:  # noqa: BLE001
                pass

    if botads_sent:
        return

    try:
        from app.services.adverts import send_action_advert as send_legacy_action_advert

        legacy_sent = await send_legacy_action_advert(bot, user_id)
        if legacy_sent:
            logger.info(f"Post-delivery legacy advert sent to user_id={user_id}")
        else:
            logger.info(f"No post-delivery advert available for user_id={user_id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to send legacy action advert to user_id={user_id}: {exc}")


def _resolve_language(payload: dict[str, Any], fallback_language: str | None = None) -> str:
    """Resolve status language from payload with fallback to settings."""
    candidate = str(
        payload.get("user_language")
        or payload.get("language")
        or fallback_language
        or settings.default_language
    ).lower()
    if candidate.startswith("uk"):
        language = "uk"
    elif candidate.startswith("ru"):
        language = "ru"
    else:
        language = "en"

    if language in localization.available_languages:
        return language
    return settings.default_language


def _get_bottom_ad_text(language: str) -> str:
    """Get optional bottom advert block for download status message."""
    try:
        return localization.get_text(language, "download_status/bottom_ad_mt").strip()
    except Exception:  # noqa: BLE001
        return ""


def _build_download_status_text(
    payload: dict[str, Any],
    fallback_failed_on: str | None = None,
    fallback_language: str | None = None,
    quality: str | None = None,
    show_bottom_ad: bool = True,
) -> str:
    status = str(payload.get("status", "")).strip().lower()
    progress = _safe_int(payload.get("progress"))
    language = _resolve_language(payload, fallback_language=fallback_language)

    def _(path: str, **kwargs) -> str:
        return localization.get_text(language, path, **kwargs)

    step_keys = [
        "download_status/step_downloading_mt",
        "download_status/step_processing_optimization_mt",
        "download_status/step_uploading_to_telegram_mt",
    ]

    def _label(step_idx: int) -> str:
        key = step_keys[step_idx]
        # Percent only on the step it belongs to: download % while downloading,
        # the uploader's estimated % while uploading to Telegram.
        clamped = min(max(progress, 0), 100) if progress is not None else None
        if step_idx == 0:
            return _(key, progress=clamped if status == "downloading" else None, quality=quality)
        if step_idx == 2:
            return _(key, progress=clamped if status == "uploading_to_telegram" else None)
        return _(key)

    def _status_to_idx(value: str) -> int:
        if value == "downloading":
            return 0
        if value in {"downloaded", "processing_optimization"}:
            return 1
        if value in {"uploading_to_telegram", "uploaded_to_telegram", "media_uploaded", "uploaded", "completed"}:
            return 2
        return 0

    lines = [_("download_status/header_mt"), ""]

    if status == "failed":
        failed_on = str(
            payload.get("failed_on")
            or payload.get("failed_step")
            or payload.get("stage")
            or fallback_failed_on
            or "downloading"
        ).strip().lower()
        failed_idx = _status_to_idx(failed_on)
        for idx, _step_key in enumerate(step_keys):
            if idx < failed_idx:
                icon = "✅"
            elif idx == failed_idx:
                icon = "❌"
            else:
                icon = "◻️"
            lines.append(f"  {icon} {_label(idx)}")
        error_text, error_code = _resolve_error_text_for_user(payload, language)
        lines.append("")
        safe_error_text = html.escape(error_text, quote=False)
        lines.append(_("download_status/error_mt", error_message=safe_error_text))
        if error_code:
            lines.append(_("download_status/code_mt", error_code=error_code))
        bottom_ad_text = _get_bottom_ad_text(language) if show_bottom_ad else ""
        if bottom_ad_text:
            lines.append("")
            lines.append(bottom_ad_text)
        return "\n".join(lines)

    current_idx = _status_to_idx(status)
    done_all = status in {"completed", "uploaded_to_telegram", "media_uploaded", "uploaded"}
    for idx, _step_key in enumerate(step_keys):
        if done_all or idx < current_idx:
            icon = "✅"
        elif idx == current_idx:
            icon = "⏳"
        else:
            icon = "◻️"
        lines.append(f"  {icon} {_label(idx)}")

    bottom_ad_text = _get_bottom_ad_text(language) if show_bottom_ad else ""
    if bottom_ad_text:
        lines.append("")
        lines.append(bottom_ad_text)

    return "\n".join(lines)


class _StaleStatusUpdate(Exception):
    """A non-terminal status arrived after the request finished or was delivered."""


async def _update_download_request_from_status(
    payload: dict[str, Any],
) -> tuple[DownloadRequest | None, str | None, str | None]:
    """Persist async status updates in DB by request_id.

    Only the status fields are written, with an atomic $set: a full save() of
    this snapshot would roll back media_sent / delivered_at written meanwhile by
    the channel-post delivery. Raises _StaleStatusUpdate for a non-terminal
    update (late upload progress tick) once the request is terminal or delivered.
    """
    request_id = payload.get("request_id")
    if not request_id:
        return None, None, None

    request = await _get_request_by_request_id(str(request_id))
    if not request:
        logger.warning(f"DownloadRequest not found for request_id={request_id}")
        return None, None, None

    previous_status = request.status
    stored_language = request.user_language or request.language

    status = str(payload.get("status", "")).strip().lower()
    if status:
        request.status = status

    progress = _safe_int(payload.get("progress"))
    if progress is not None:
        request.progress = progress

    error_code = payload.get("error_code")
    error_message = payload.get("error_message") or payload.get("error")
    request.error_code = str(error_code) if error_code is not None else request.error_code
    request.error_message = str(error_message) if error_message is not None else request.error_message

    source_chat_id, source_message_id = _extract_source_pointer(payload)
    if source_chat_id is not None and source_message_id is not None:
        request.source_chat_id = source_chat_id
        request.source_message_id = source_message_id
    source_caption = payload.get("source_caption")
    if source_caption is not None:
        request.source_caption = str(source_caption)

    terminal = payload.get("terminal")
    if isinstance(terminal, bool):
        request.terminal = terminal
    elif isinstance(terminal, str):
        request.terminal = terminal.lower() == "true"
    else:
        request.terminal = request.status in {"completed", "failed"}

    request.updated_at = datetime.datetime.now(datetime.UTC)
    fields = {
        "status": request.status,
        "progress": request.progress,
        "error_code": request.error_code,
        "error_message": request.error_message,
        "source_chat_id": request.source_chat_id,
        "source_message_id": request.source_message_id,
        "source_caption": request.source_caption,
        "terminal": request.terminal,
        "updated_at": request.updated_at,
    }
    query: dict[str, Any] = {"_id": request.id}
    if not request.terminal:
        query["terminal"] = {"$ne": True}
        query["media_sent"] = {"$ne": True}
    result = await DownloadRequest.get_pymongo_collection().update_one(query, {"$set": fields})
    if result.matched_count == 0:
        raise _StaleStatusUpdate(str(request_id))
    return request, previous_status, stored_language


async def _return_credit_and_notify(bot: Bot, request: DownloadRequest, language: str | None) -> None:
    try:
        returned = await return_request_credit(request)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to return single credit for request_id={request.request_id}: {exc}")
        return
    if not returned:
        return
    try:
        await bot.send_message(
            request.user_id,
            localization.get_text(_resolve_language({}, fallback_language=language), "download/credit_returned_mt"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to notify user {request.user_id} about returned credit: {exc}")


async def create_nats_client() -> NatsClient:
    """Create and connect shared NATS client."""
    nats_cfg = settings.nats_server
    client = NatsClient()
    await client.connect(servers=[nats_cfg.uri], **_build_connect_kwargs())
    return client


async def close_nats_client(client: NatsClient | None) -> None:
    """Close shared NATS client if connected."""
    if client and client.is_connected:
        await client.close()


async def subscribe_download_status(client: NatsClient, bot: Bot):
    """Subscribe to async status updates and edit user messages."""
    download_status_subject = _build_download_status_subject()
    edit_message_subject = _build_edit_message_subject()

    async def _download_status_handler(msg) -> None:
        try:
            payload = orjson.loads(msg.data) if msg.data else {}
        except orjson.JSONDecodeError:
            raw_data = msg.data.decode("utf-8", errors="replace") if msg.data else ""
            logger.warning(f"Invalid JSON in {download_status_subject}: {raw_data}")
            return

        if not isinstance(payload, dict):
            logger.warning(f"Unexpected payload type in {download_status_subject}: {type(payload).__name__}")
            return

        payload_bot_name = payload.get("bot_name")
        if payload_bot_name and str(payload_bot_name) != settings.bot_name:
            return

        try:
            request, previous_status, stored_language = await _update_download_request_from_status(payload)
        except _StaleStatusUpdate:
            # Late progress tick: don't turn a finished status message back into ⏳.
            return
        if request and request.status == "failed" and request.terminal:
            if await _schedule_priority_retry(request, payload, stored_language):
                return
            # Stale meta (deleted video, format gone) is one possible cause; refetch next time.
            await invalidate_meta_for_url(request.url)
            if request.paid_credit:
                await _return_credit_and_notify(bot, request, stored_language)

        user_id = _safe_int(payload.get("user_id")) or (request.user_id if request else None)
        message_id = _safe_int(payload.get("message_id")) or (request.message_id if request else None)
        if user_id is None or message_id is None:
            logger.warning(f"Missing user_id/message_id in {download_status_subject}: {payload}")
        else:
            status = str(payload.get("status", "")).strip().lower()
            if status == "failed":
                request_progress = _safe_int(request.progress) if request else None
                payload_progress = _safe_int(payload.get("progress"))
                has_download_progress = (
                    (request_progress is not None and request_progress > 0)
                    or (payload_progress is not None and payload_progress > 0)
                )
                if not has_download_progress:
                    one_line_error_text, entities = _build_failed_before_progress_text(
                        payload,
                        fallback_language=stored_language,
                    )
                    try:
                        await bot.edit_message_text(
                            chat_id=user_id,
                            message_id=message_id,
                            text=one_line_error_text,
                            parse_mode=None,
                            entities=entities,
                        )
                    except TelegramBadRequest as exc:
                        error_text = str(exc)
                        if "message is not modified" not in error_text.lower():
                            logger.warning(f"Failed to edit message for pre-progress failure: {error_text}")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"Unexpected error while handling pre-progress failure: {exc}")
                    return

            if _has_download_started_status(status, payload):
                status_text = _build_download_status_text(
                    payload,
                    fallback_failed_on=previous_status,
                    fallback_language=stored_language,
                    quality=request.quality if request else None,
                    show_bottom_ad=not (request and request.premium),
                )

                try:
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=message_id,
                        text=status_text,
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramBadRequest as exc:
                    error_text = str(exc)
                    if "message is not modified" not in error_text.lower():
                        logger.warning(f"Failed to edit message for download status: {error_text}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Unexpected error while handling download status: {exc}")

        if request and _is_media_ready_status(request.status):
            await _deliver_request_media_to_user(bot, request)

    async def _schedule_priority_retry(
        request: DownloadRequest,
        payload: dict[str, Any],
        language: str | None,
    ) -> bool:
        """Queue a retry of a busy-failed priority download; False when it should fail now."""
        if not request.priority or _normalize_error_code(payload.get("error_code")) not in BUSY_ERROR_CODES:
            return False
        now = datetime.datetime.now(datetime.UTC)
        busy_since = _as_utc(request.busy_since) or now
        if now - busy_since >= PRIORITY_RETRY_WINDOW:
            return False

        retry = DownloadRequest(
            url=request.url,
            quality=request.quality,
            bot_name=request.bot_name,
            language=request.language,
            user_language=request.user_language,
            user_id=request.user_id,
            message_id=request.message_id,
            status="retry_wait",
            premium=request.premium,
            paid_credit=request.paid_credit,
            priority=True,
            busy_since=busy_since,
            retry_attempt=request.retry_attempt + 1,
            is_retry=True,
        )
        await retry.insert()
        if request.paid_credit:
            # The credit moves to the retry; this failed attempt must not return it.
            await DownloadRequest.get_pymongo_collection().update_one(
                {"_id": request.id}, {"$set": {"paid_credit": False}},
            )
            request.paid_credit = False

        text = localization.get_text(
            _resolve_language(payload, fallback_language=language),
            "download_status/busy_retry_mt",
            attempt=retry.retry_attempt,
            delay=int(PRIORITY_RETRY_DELAY_SECONDS),
        )
        try:
            await bot.edit_message_text(
                chat_id=request.user_id,
                message_id=request.message_id,
                text=text,
                parse_mode=None,
            )
        except Exception as exc:  # noqa: BLE001
            if "message is not modified" not in str(exc).lower():
                logger.warning(f"Failed to show busy-retry status: {exc}")

        task = asyncio.create_task(_run_priority_retry(retry.request_id))
        _retry_tasks.add(task)
        task.add_done_callback(_retry_tasks.discard)
        logger.info(
            "Servers busy (code %s): retry %s for request_id=%s as %s",
            payload.get("error_code"),
            retry.retry_attempt,
            request.request_id,
            retry.request_id,
        )
        return True

    async def _run_priority_retry(request_id: str) -> None:
        await asyncio.sleep(PRIORITY_RETRY_DELAY_SECONDS)
        request = await _get_request_by_request_id(request_id)
        if request is None or request.status != "retry_wait":
            return
        request.status = "queued"
        request.updated_at = datetime.datetime.now(datetime.UTC)
        await request.save()
        try:
            ack = await request_media_download(
                request.url,
                client,
                user_id=request.user_id,
                message_id=request.message_id,
                quality=request.quality,
                user_language=request.user_language,
                language=request.language,
                request_id=request.request_id,
            )
        except Exception as exc:  # noqa: BLE001
            # Manager unreachable counts as busy: retry again or fail with the usual text.
            logger.warning(f"Priority retry dispatch failed for request_id={request_id}: {exc!r}")
            await _download_status_handler(SimpleNamespace(data=orjson.dumps({
                "request_id": request.request_id,
                "status": "failed",
                "terminal": True,
                "error_code": "403",
                "error_message": f"Manager unreachable: {exc!r}",
                "user_id": request.user_id,
                "message_id": request.message_id,
                "bot_name": settings.bot_name,
            })))
            return
        await DownloadRequest.get_pymongo_collection().update_one(
            {"_id": request.id},
            {"$set": {"nats_ack": ack, "updated_at": datetime.datetime.now(datetime.UTC)}},
        )

    async def _edit_message_handler(msg) -> None:
        try:
            payload = orjson.loads(msg.data) if msg.data else {}
        except orjson.JSONDecodeError:
            raw_data = msg.data.decode("utf-8", errors="replace") if msg.data else ""
            logger.warning(f"Invalid JSON in {edit_message_subject}: {raw_data}")
            return

        if not isinstance(payload, dict):
            logger.warning(f"Unexpected payload type in {edit_message_subject}: {type(payload).__name__}")
            return

        payload_bot_name = payload.get("bot_name")
        if payload_bot_name and str(payload_bot_name) != settings.bot_name:
            return

        status = str(payload.get("status", "")).strip().lower()
        if status != "processing":
            return

        phase = str(payload.get("phase", "")).strip().lower()
        if phase == "download":
            request_id = payload.get("request_id")
            if request_id:
                request = await _get_request_by_request_id(str(request_id))
                if request:
                    request_status = (request.status or "").strip().lower()
                    request_progress = _safe_int(request.progress)
                    if (request_progress is not None and request_progress > 0) or request_status in DOWNLOAD_STARTED_STATUSES or request_status == "failed":
                        return

        user_id = _safe_int(payload.get("user_id"))
        message_id = _safe_int(payload.get("message_id"))
        if user_id is None or message_id is None:
            logger.warning(f"Missing user_id/message_id in {edit_message_subject}: {payload}")
            return

        text, entities = _build_processing_status_text(payload)

        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text=text,
                parse_mode=None,
                entities=entities,
            )
        except TelegramBadRequest as exc:
            error_text = str(exc)
            if "message is not modified" not in error_text.lower():
                logger.warning(f"Failed to edit message for processing status: {error_text}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Unexpected error while handling processing status: {exc}")

    download_status_subscription = await client.subscribe(download_status_subject, cb=_download_status_handler)
    edit_message_subscription = await client.subscribe(edit_message_subject, cb=_edit_message_handler)
    await client.flush()
    logger.info(f"Subscribed to NATS download status subject: {download_status_subject}")
    logger.info(f"Subscribed to NATS edit message subject: {edit_message_subject}")
    return [download_status_subscription, edit_message_subscription]


async def unsubscribe_nats_subscription(subscription) -> None:
    """Safely unsubscribe NATS subscription."""
    if not subscription:
        return

    if isinstance(subscription, (list, tuple, set)):
        for item in subscription:
            await unsubscribe_nats_subscription(item)
        return

    try:
        await subscription.unsubscribe()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to unsubscribe NATS subscription: {exc}")


async def process_uploaded_media_channel_post(message: Message, bot: Bot) -> None:
    """Handle uploaded media in source channels and deliver it to user when request is ready."""
    chat_id = message.chat.id if message.chat else None
    if chat_id is None:
        return

    if settings.allowed_source_channel_ids and chat_id not in settings.allowed_source_channel_ids:
        return

    has_supported_media = any((
        message.audio,
        message.document,
        message.video,
        message.voice,
        message.video_note,
    ))
    if not has_supported_media:
        return

    parsed_caption = _parse_tracking_caption(message.caption)
    if not parsed_caption:
        return

    request_id = parsed_caption["rid"]
    request = await _get_request_by_request_id(request_id)
    if not request:
        logger.warning(f"Channel post has unknown request_id in caption: {request_id}")
        return

    caption_user_id = _safe_int(parsed_caption.get("uid"))
    if caption_user_id is not None and caption_user_id != request.user_id:
        logger.warning(
            "Caption user mismatch for request_id=%s: caption uid=%s, db uid=%s",
            request.request_id,
            caption_user_id,
            request.user_id,
        )
        return

    caption_message_id = _safe_int(parsed_caption.get("mid"))
    if caption_message_id is not None and request.message_id and caption_message_id != request.message_id:
        logger.warning(
            "Caption message mismatch for request_id=%s: caption mid=%s, db mid=%s",
            request.request_id,
            caption_message_id,
            request.message_id,
        )
        return

    request.source_chat_id = chat_id
    request.source_message_id = message.message_id
    request.source_caption = message.caption
    request.updated_at = datetime.datetime.now(datetime.UTC)
    await DownloadRequest.get_pymongo_collection().update_one(
        {"_id": request.id},
        {"$set": {
            "source_chat_id": request.source_chat_id,
            "source_message_id": request.source_message_id,
            "source_caption": request.source_caption,
            "updated_at": request.updated_at,
        }},
    )
    logger.info(
        "Registered source media message: request_id=%s source=%s:%s",
        request.request_id,
        request.source_chat_id,
        request.source_message_id,
    )

    try:
        await cache_track_from_channel_post(
            request=request,
            message=message,
            quality=parsed_caption.get("q") or request.quality,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to cache media for request_id={request.request_id}: {exc}")

    # Channel post already means media exists in source channel, so we should
    # attempt delivery even if async status events are delayed or missing.
    await _deliver_request_media_to_user(bot, request)


async def request_social_meta(
    url: str,
    client: NatsClient,
    user_id: int,
    message_id: int,
    user_language: str = "en",
) -> dict[str, Any]:
    """Request metadata for social media URL through NATS manager."""
    nats_cfg = settings.nats_server

    subject = _build_subject()
    payload = _build_payload(url, user_id, message_id, user_language)
    payload_body = orjson.dumps(payload)
    logger.debug("NATS metadata request subject: %s", subject)

    response = await client.request(
        subject,
        payload_body,
        timeout=nats_cfg.meta_timeout_seconds,
    )

    if not response.data:
        result: dict[str, Any] = {}
    else:
        try:
            result = orjson.loads(response.data)
        except orjson.JSONDecodeError:
            raw_response = response.data.decode("utf-8", errors="replace")
            logger.warning(f"NATS metadata non-JSON response:\n{raw_response}")
            return {"raw_response": raw_response}

    if isinstance(result, dict):
        logger.debug(
            "NATS metadata response received: request_id=%s result=%s",
            result.get("request_id"),
            result.get("result"),
        )
    else:
        logger.debug("NATS metadata response received")
    return result


async def request_transcript(
    video_id: str,
    client: NatsClient,
    *,
    language: str = "en",
    max_chars: int = 200_000,
    timestamps: bool = True,
    timestamp_interval_sec: int = 30,
) -> dict[str, Any]:
    """Request a YouTube transcript via NATS manager_transcript.

    The worker prefers uk/ru/en captions in the order of `language`. Errors come
    back as result=0 with error_code 306 (failed), 307 (timeout) or 308 (video
    has no captions).
    """
    subject = _build_transcript_subject()
    payload = {
        "video_id": video_id,
        "language": language,
        "max_chars": max_chars,
        "timestamps": timestamps,
        "timestamp_interval_sec": timestamp_interval_sec,
    }
    logger.debug("NATS transcript request subject: %s", subject)
    response = await client.request(
        subject,
        orjson.dumps(payload),
        timeout=settings.nats_transcript_timeout_seconds,
    )
    if not response.data:
        return {}
    try:
        result = orjson.loads(response.data)
    except orjson.JSONDecodeError:
        raw_response = response.data.decode("utf-8", errors="replace")
        logger.warning(f"NATS transcript non-JSON response:\n{raw_response[:500]}")
        return {"raw_response": raw_response}
    return result if isinstance(result, dict) else {}


async def request_media_download(
    url: str,
    client: NatsClient,
    user_id: int,
    message_id: int,
    quality: str = QUALITY_AUDIO,
    user_language: str = "en",
    language: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Request video (360p/480p/720p) or audio download via NATS manager_download."""
    nats_cfg = settings.nats_server
    subject = _build_download_subject()

    lang = language or user_language or "en"
    user_lang = user_language or language or "en"

    payload: dict[str, Any] = {
        "url": url,
        "quality": quality,
        "bot_name": settings.bot_name,
        "language": lang,
        "user_language": user_lang,
        "user_id": user_id,
        "message_id": message_id,
    }
    if request_id:
        payload["request_id"] = request_id

    payload_body = orjson.dumps(payload)
    logger.debug("NATS download request subject: %s", subject)

    response = await client.request(
        subject,
        payload_body,
        timeout=nats_cfg.download_timeout_seconds,
    )

    if not response.data:
        result: dict[str, Any] = {}
    else:
        try:
            result = orjson.loads(response.data)
        except orjson.JSONDecodeError:
            raw_response = response.data.decode("utf-8", errors="replace")
            logger.warning(f"NATS download ACK non-JSON response:\n{raw_response}")
            return {"raw_response": raw_response}

    if isinstance(result, dict):
        logger.debug(
            "NATS download ACK received: request_id=%s status=%s result=%s",
            result.get("request_id"),
            result.get("status"),
            result.get("result"),
        )
    else:
        logger.debug("NATS download ACK received")
    return result
