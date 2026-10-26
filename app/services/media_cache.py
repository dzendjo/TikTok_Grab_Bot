"""Media cache helpers backed by legacy-compatible Track model."""
from __future__ import annotations

import datetime
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from pymongo.errors import DuplicateKeyError

from app.database import DownloadRequest, Track
from app.utils import get_logger
from app.utils.youtube import extract_youtube_video_id, is_youtube_host

logger = get_logger(__name__)

# Truncation heuristic for cached audio. Telegram Bot API cannot report the
# real duration of a stored file (message duration is whatever the uploader
# passed, and getFile is capped at 20 MB), so we compare the actual file size
# reported by Telegram against the size implied by the expected duration.
# YouTube format 140 (AAC) is ~128 kbps, so a file noticeably smaller than
# duration * bitrate was cut short during download.
NOMINAL_AUDIO_BITRATE_BPS = 129_500
TRUNCATION_MIN_EXPECTED_DURATION_SEC = 300
TRUNCATION_RATIO_THRESHOLD = 0.92
TRUNCATION_MIN_SHORTFALL_SEC = 60
TRUNCATION_CHECK_MIME_TYPES = {
    "audio/mp4",
    "audio/m4a",
    "audio/x-m4a",
    "audio/aac",
    "audio/mpeg",
}


def to_mb(count, need_sep=True):
    """Legacy-compatible bytes formatter."""
    if count is None or count == 0:
        return ""

    try:
        if count and isinstance(count, str):
            count = int(count)
    except Exception:  # noqa: BLE001
        return 0

    first_part = ""
    if need_sep:
        first_part = "- "

    if count > 1024 * 1024:
        return f"{first_part}{count // (1024 * 1024)}.{str(count % (1024 * 1024))[0]}MB"
    if count > 1024:
        return f"{first_part}{count // 1024}.{str(count % 1024)[0]}KB"
    return f"{first_part}{count}B"


def _extract_source_info(url: str) -> tuple[str | None, str | None]:
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


def quality_to_format_id(quality: str | None) -> str:
    value = (quality or "").strip().lower()
    mapping = {
        "audio": "140",
        "140": "140",
        "video_low": "134",
        "360p": "134",
        "134": "134",
        "video_mid": "135",
        "480p": "135",
        "135": "135",
        "video_high": "136",
        "720p": "136",
        "136": "136",
        "video_extra_high": "137",
        "1080p": "137",
        "137": "137",
    }
    return mapping.get(value, "140")


def _extract_message_media_info(message: Message) -> dict[str, Any] | None:
    if message.audio:
        media = message.audio
        return {
            "label": "audio",
            "mime_type": media.mime_type or "",
            "file_id": media.file_id,
            "file_unique_id": media.file_unique_id,
            "file_size": int(media.file_size or 0),
            "duration": int(media.duration or 0),
            "title": media.title or "",
            "performer": media.performer or "",
        }

    if message.document:
        media = message.document
        return {
            "label": "document",
            "mime_type": media.mime_type or "",
            "file_id": media.file_id,
            "file_unique_id": media.file_unique_id,
            "file_size": int(media.file_size or 0),
            "duration": 0,
            "title": media.file_name or "",
            "performer": "",
        }

    if message.video:
        media = message.video
        return {
            "label": "video",
            "mime_type": media.mime_type or "",
            "file_id": media.file_id,
            "file_unique_id": media.file_unique_id,
            "file_size": int(media.file_size or 0),
            "duration": int(media.duration or 0),
            "title": "",
            "performer": "",
            "width": int(media.width or 0),
            "height": int(media.height or 0),
        }

    if message.voice:
        media = message.voice
        return {
            "label": "audio",
            "mime_type": media.mime_type or "",
            "file_id": media.file_id,
            "file_unique_id": media.file_unique_id,
            "file_size": int(media.file_size or 0),
            "duration": int(media.duration or 0),
            "title": "",
            "performer": "",
        }

    return None


def _build_track_inner_id(site_id: str, quality: str | None) -> str:
    return f"{site_id}_{quality_to_format_id(quality)}"


def _build_legacy_track_inner_id(extractor: str, site_id: str) -> str:
    return f"{extractor}___{site_id}"


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_media_label(label: Any) -> str:
    return str(label or "").strip().lower()


def _pick_download_item_for_quality(
    download_urls: list[dict[str, Any]],
    target_format_id: str,
    quality: str | None,
) -> dict[str, Any] | None:
    for item in download_urls:
        if str(item.get("format_id", "")).strip() == target_format_id:
            return dict(item)

    requested_quality = (quality or "").strip().lower()
    if requested_quality not in {"audio", "140"}:
        # Video qualities are cached per format_id; any other item would be a
        # different resolution (or audio), so treat it as a cache miss.
        return None

    for item in download_urls:
        if _normalize_media_label(item.get("label")) in {"audio", "voice"}:
            return dict(item)

    if download_urls:
        return dict(download_urls[0])
    return None


def _normalize_download_item(
    item: dict[str, Any],
    quality: str | None,
    target_format_id: str,
) -> dict[str, Any]:
    normalized = dict(item)

    label = _normalize_media_label(normalized.get("label"))
    if not label:
        label = "audio" if target_format_id == "140" else "video"
    normalized["label"] = label

    format_id = str(normalized.get("format_id", "")).strip()
    if not format_id:
        format_id = "140" if label in {"audio", "voice"} else target_format_id
    normalized["format_id"] = format_id

    if not normalized.get("quality"):
        normalized["quality"] = quality or ("audio" if format_id == "140" else "video")

    file_size = int(normalized.get("file_size") or 0)
    normalized["file_size"] = file_size
    if not normalized.get("file_size_text"):
        normalized["file_size_text"] = to_mb(file_size, need_sep=False)

    return normalized


async def _resolve_cached_track_hit(
    url: str,
    quality: str,
) -> dict[str, Any] | None:
    extractor, site_id = _extract_source_info(url)
    if not extractor or not site_id:
        return None

    target_format_id = quality_to_format_id(quality)
    normalized_inner_id = _build_track_inner_id(site_id, quality)
    legacy_inner_id = _build_legacy_track_inner_id(extractor, site_id)

    candidate_inner_ids: list[str] = [normalized_inner_id]
    if legacy_inner_id != normalized_inner_id:
        candidate_inner_ids.append(legacy_inner_id)

    for inner_id in candidate_inner_ids:
        track = await Track.find_one(Track.inner_id == inner_id)
        if not track:
            continue

        item = _pick_download_item_for_quality(track.download_urls, target_format_id, quality)
        if not item:
            continue

        return {
            "track": track,
            "item": _normalize_download_item(item, quality, target_format_id),
            "extractor": extractor,
            "site_id": site_id,
            "target_format_id": target_format_id,
            "normalized_inner_id": normalized_inner_id,
            "quality": quality,
            "url": url,
        }

    return None


def _is_truncation_check_applicable(track: Track, item: dict[str, Any]) -> bool:
    """Whether the size-vs-duration heuristic can judge this cached item."""
    if _normalize_media_label(item.get("label")) not in {"audio", "voice"}:
        return False
    mime_type = str(item.get("mime_type") or "").strip().lower()
    if mime_type not in TRUNCATION_CHECK_MIME_TYPES:
        return False
    expected_duration = int(track.duration or 0)
    file_size = int(item.get("file_size") or 0)
    return expected_duration >= TRUNCATION_MIN_EXPECTED_DURATION_SEC and file_size > 0


def _is_cached_item_truncated(track: Track, item: dict[str, Any]) -> bool:
    """Detect cached audio that is shorter than the source video.

    Items with duration_verified were validated before (worker-side ffprobe
    for new uploads, or a passed heuristic check persisted earlier) and are
    trusted without recomputing anything.
    """
    if item.get("duration_verified"):
        return False
    if not _is_truncation_check_applicable(track, item):
        return False

    expected_duration = int(track.duration or 0)
    implied_duration = int(item.get("file_size") or 0) * 8 / NOMINAL_AUDIO_BITRATE_BPS
    return (
        implied_duration < expected_duration * TRUNCATION_RATIO_THRESHOLD
        and (expected_duration - implied_duration) > TRUNCATION_MIN_SHORTFALL_SEC
    )


async def _mark_cached_item_verified(hit: dict[str, Any]) -> None:
    """Persist a passed truncation check so the item is never re-evaluated."""
    track: Track = hit["track"]
    item: dict[str, Any] = hit["item"]
    target_file_unique_id = str(item.get("file_unique_id") or "").strip()
    target_format_id = str(item.get("format_id") or "").strip()

    changed = False
    for candidate in track.download_urls:
        if target_file_unique_id:
            matches = str(candidate.get("file_unique_id") or "").strip() == target_file_unique_id
        else:
            matches = str(candidate.get("format_id") or "").strip() == target_format_id
        if matches and not candidate.get("duration_verified"):
            candidate["duration_verified"] = True
            changed = True

    if changed:
        item["duration_verified"] = True
        await track.save()
        logger.info(f"Marked cached item duration_verified: inner_id={track.inner_id}")


async def _quarantine_cached_item(hit: dict[str, Any]) -> None:
    """Drop a truncated item from the track so a fresh download replaces it."""
    track: Track = hit["track"]
    item: dict[str, Any] = hit["item"]
    target_file_unique_id = str(item.get("file_unique_id") or "").strip()
    target_format_id = str(item.get("format_id") or "").strip()

    def _matches(candidate: dict[str, Any]) -> bool:
        if target_file_unique_id:
            return str(candidate.get("file_unique_id") or "").strip() == target_file_unique_id
        return str(candidate.get("format_id") or "").strip() == target_format_id

    remaining = [candidate for candidate in track.download_urls if not _matches(candidate)]
    if len(remaining) == len(track.download_urls):
        return

    track.download_urls = remaining
    await track.save()
    logger.warning(
        "Quarantined truncated cached media: inner_id=%s file_unique_id=%s "
        "expected_duration=%s file_size=%s",
        track.inner_id,
        target_file_unique_id or "<none>",
        track.duration,
        item.get("file_size"),
    )


async def _send_cached_file_id_to_user(
    bot: Bot,
    user_id: int,
    item: dict[str, Any],
) -> bool:
    file_id = str(item.get("file_id") or "").strip()
    if not file_id:
        return False

    bot_username = None
    try:
        bot_info = await bot.get_me()
        bot_username = bot_info.username
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to resolve bot username for file_id send: {exc}")

    caption = f"@{(bot_username or '').lstrip('@')}" if bot_username else None

    label = _normalize_media_label(item.get("label"))
    send_method = bot.send_document
    if label in {"audio", "voice"}:
        send_method = bot.send_audio
    elif label == "video":
        send_method = bot.send_video

    try:
        await send_method(user_id, file_id, caption=caption)
        return True
    except TelegramBadRequest as first_exc:
        logger.warning(f"Cached file_id send with caption failed, retrying without caption: {first_exc}")
        try:
            await send_method(user_id, file_id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Cached file_id send failed for user_id={user_id}: {exc}")
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Cached file_id send failed for user_id={user_id}: {exc}")
        return False


async def _upsert_normalized_track_from_hit(hit: dict[str, Any]) -> None:
    track: Track = hit["track"]
    normalized_inner_id = hit["normalized_inner_id"]
    extractor = hit["extractor"]
    site_id = hit["site_id"]
    url = hit["url"]
    item = dict(hit["item"])
    item["cached_at"] = datetime.datetime.now(datetime.UTC).isoformat()

    normalized_track = await Track.find_one(Track.inner_id == normalized_inner_id)
    if not normalized_track:
        normalized_track = Track(
            inner_id=normalized_inner_id,
            extractor=track.extractor or extractor,
            site_id=track.site_id or site_id,
            add_date=track.add_date or datetime.datetime.now(datetime.UTC),
            title=track.title or "",
            thumb_url=track.thumb_url or "",
            small_thumb_url=track.small_thumb_url or "",
            url=track.url or url,
            upload_date=track.upload_date or "",
            uploader=track.uploader or "",
            duration=int(track.duration or 0),
            view_count=int(track.view_count or 0),
            like_count=track.like_count,
            dislike_count=int(track.dislike_count or 0),
            download_urls=[item],
        )
        try:
            await normalized_track.insert()
            return
        except Exception:  # noqa: BLE001
            # Race condition: another worker may create it first.
            normalized_track = await Track.find_one(Track.inner_id == normalized_inner_id)
            if not normalized_track:
                raise

    normalized_track.extractor = normalized_track.extractor or track.extractor or extractor
    normalized_track.site_id = normalized_track.site_id or track.site_id or site_id
    normalized_track.url = normalized_track.url or track.url or url
    normalized_track.title = normalized_track.title or track.title or ""
    normalized_track.uploader = normalized_track.uploader or track.uploader or ""
    if not normalized_track.duration:
        normalized_track.duration = int(track.duration or 0)
    if not normalized_track.add_date:
        normalized_track.add_date = track.add_date or datetime.datetime.now(datetime.UTC)

    normalized_track.download_urls = _merge_download_item(normalized_track.download_urls, item)
    await normalized_track.save()


def _merge_download_item(download_urls: list[dict[str, Any]], new_item: dict[str, Any]) -> list[dict[str, Any]]:
    format_id = str(new_item.get("format_id", "")).strip()
    merged: list[dict[str, Any]] = []
    replaced = False

    for item in download_urls:
        current_format_id = str(item.get("format_id", "")).strip()
        if format_id and current_format_id == format_id:
            merged.append(new_item)
            replaced = True
            continue
        merged.append(item)

    if not replaced:
        merged.append(new_item)

    return merged


async def cache_track_from_channel_post(
    request: DownloadRequest,
    message: Message,
    quality: str | None = None,
) -> Track | None:
    """Upsert cache entry in legacy `tracks` collection from source channel media."""
    extractor, site_id = _extract_source_info(request.url)
    if not extractor or not site_id:
        return None

    media_info = _extract_message_media_info(message)
    if not media_info:
        return None

    format_id = quality_to_format_id(quality or request.quality)
    inner_id = _build_track_inner_id(site_id, quality or request.quality)
    now = datetime.datetime.now(datetime.UTC)

    download_item: dict[str, Any] = {
        "label": media_info.get("label") or ("audio" if format_id == "140" else "video"),
        "format_id": format_id,
        "quality": quality or request.quality or "audio",
        "file_id": media_info.get("file_id", ""),
        "file_unique_id": media_info.get("file_unique_id", ""),
        "file_size": int(media_info.get("file_size") or 0),
        "file_size_text": to_mb(int(media_info.get("file_size") or 0), need_sep=False),
        "mime_type": media_info.get("mime_type", ""),
        "source_chat_id": request.source_chat_id,
        "source_message_id": request.source_message_id,
        "cached_at": now.isoformat(),
        # Worker validates output duration with ffprobe before upload, so
        # entries cached from now on are trusted by the truncation heuristic.
        "duration_verified": True,
    }

    track = await Track.find_one(Track.inner_id == inner_id)
    if not track:
        track = Track(
            inner_id=inner_id,
            extractor=extractor,
            site_id=site_id,
            add_date=now,
            title=str(media_info.get("title") or ""),
            thumb_url="",
            small_thumb_url="",
            url=request.url,
            upload_date="",
            uploader=str(media_info.get("performer") or ""),
            duration=int(media_info.get("duration") or 0),
            view_count=0,
            like_count=0,
            dislike_count=0,
            download_urls=[download_item],
        )
        try:
            await track.insert()
            logger.info(f"Created media cache track: inner_id={track.inner_id}")
            return track
        except DuplicateKeyError:
            # Concurrent writer inserted the same inner_id after our find_one.
            track = await Track.find_one(Track.inner_id == inner_id)
            if not track:
                raise

    track.extractor = track.extractor or extractor
    track.site_id = track.site_id or site_id
    track.url = track.url or request.url
    if not track.title:
        track.title = str(media_info.get("title") or "")
    if not track.uploader:
        track.uploader = str(media_info.get("performer") or "")
    if not track.duration:
        track.duration = int(media_info.get("duration") or 0)
    if not track.add_date:
        track.add_date = now

    track.download_urls = _merge_download_item(track.download_urls, download_item)
    await track.save()
    logger.info(f"Updated media cache track: inner_id={track.inner_id}")
    return track


async def get_cached_source_for_url(
    url: str,
    quality: str = "audio",
) -> tuple[int, int] | None:
    """Return cached source channel pointer for URL/quality if available."""
    hit = await _resolve_cached_track_hit(url, quality)
    if not hit:
        return None

    item = hit["item"]
    source_chat_id = _safe_int(item.get("source_chat_id"))
    source_message_id = _safe_int(item.get("source_message_id"))
    if source_chat_id is None or source_message_id is None:
        return None
    return source_chat_id, source_message_id


async def get_cached_sizes(url: str, qualities: Iterable[str]) -> dict[str, int]:
    """Return {quality: cached file size in bytes} for qualities servable from cache.

    Mirrors try_send_cached_media_for_url: truncated items or items without a
    source pointer / file_id are not reported as cached.
    """
    cached: dict[str, int] = {}
    for quality in qualities:
        hit = await _resolve_cached_track_hit(url, quality)
        if not hit or _is_cached_item_truncated(hit["track"], hit["item"]):
            continue
        item = hit["item"]
        has_source = (
            _safe_int(item.get("source_chat_id")) is not None
            and _safe_int(item.get("source_message_id")) is not None
        )
        if has_source or str(item.get("file_id") or "").strip():
            cached[quality] = int(item.get("file_size") or 0)
    return cached


async def send_cached_media_to_user(
    bot: Bot,
    user_id: int,
    source_chat_id: int,
    source_message_id: int,
) -> bool:
    """Send cached media to user via copy_message from source channel."""
    bot_username = None
    try:
        bot_info = await bot.get_me()
        bot_username = bot_info.username
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to resolve bot username for cache send: {exc}")

    caption = f"@{(bot_username or '').lstrip('@')}" if bot_username else None

    try:
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=source_chat_id,
            message_id=source_message_id,
            caption=caption,
        )
        return True
    except TelegramBadRequest as first_exc:
        logger.warning(f"Cache copy with caption failed, retrying without caption: {first_exc}")
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Cache copy failed for user_id={user_id}: {exc}")
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Cache copy failed for user_id={user_id}: {exc}")
        return False


async def try_send_cached_media_for_url(
    bot: Bot,
    user_id: int,
    url: str,
    quality: str = "audio",
) -> bool:
    """Try to send cached media by source pointer first, then legacy file_id."""
    hit = await _resolve_cached_track_hit(url, quality)
    if not hit:
        return False

    item = hit["item"]
    if _is_cached_item_truncated(hit["track"], item):
        # Serve nothing from cache: returning False sends the request through
        # the regular download flow, so the user sees the standard progress
        # statuses and gets a freshly validated file that then replaces this
        # cache entry.
        try:
            await _quarantine_cached_item(hit)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to quarantine truncated cache entry: {exc}")
        return False

    source_chat_id = _safe_int(item.get("source_chat_id"))
    source_message_id = _safe_int(item.get("source_message_id"))
    file_id = str(item.get("file_id") or "").strip()

    sent = False
    if source_chat_id is not None and source_message_id is not None:
        sent = await send_cached_media_to_user(
            bot=bot,
            user_id=user_id,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
        )
        if not sent and file_id:
            sent = await _send_cached_file_id_to_user(bot, user_id, item)
    elif file_id:
        sent = await _send_cached_file_id_to_user(bot, user_id, item)

    if not sent:
        return False

    if not item.get("duration_verified") and _is_truncation_check_applicable(hit["track"], item):
        # Heuristic passed above and the file was served fine: persist the
        # verdict so this legacy item skips the check on every future hit.
        try:
            await _mark_cached_item_verified(hit)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to mark cached item as verified: {exc}")

    needs_self_heal = (
        hit["track"].inner_id != hit["normalized_inner_id"]
        or source_chat_id is None
        or source_message_id is None
    )
    if needs_self_heal:
        try:
            await _upsert_normalized_track_from_hit(hit)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to self-heal normalized track cache: {exc}")

    return True
