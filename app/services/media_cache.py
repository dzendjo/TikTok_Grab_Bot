"""Media cache: TikTok posts already uploaded to the source channels.

Track documents (legacy `tracks` collection) are keyed "<post id>_<quality>"
with quality video, audio or photos. The item keeps the source channel
pointer, and delivery copies those channel posts, so any bot that is an admin
of the channel can serve them. An album is one post per photo.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pymongo.errors import DuplicateKeyError

from app.constants import AUDIO_BUTTON_ICON, QUALITY_AUDIO
from app.database import Track
from app.locales import localization
from app.utils import get_logger

logger = get_logger(__name__)

AUDIO_CALLBACK_PREFIX = "au:"
# copyMessages takes up to 100 ids; TikTok albums have at most 35 photos.
_COPY_MESSAGES_LIMIT = 100


@dataclass(slots=True, frozen=True)
class CachedMedia:
    quality: str
    source_chat_id: int
    message_ids: tuple[int, ...]


def track_inner_id(post_id: str, quality: str) -> str:
    return f"{post_id}_{quality}"


def build_audio_keyboard(post_id: str, language: str) -> InlineKeyboardMarkup:
    """"🎵 Sound" button under a delivered video or album."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{AUDIO_BUTTON_ICON} {localization.get_text(language, 'download/audio_button_mt').strip()}",
        callback_data=f"{AUDIO_CALLBACK_PREFIX}{post_id}",
    )
    return builder.as_markup()


async def delivery_caption(bot: Bot) -> str:
    me = await bot.me()
    return f"@{(me.username or '').lstrip('@')}"


def _item_pointer(item: dict[str, Any]) -> tuple[int, tuple[int, ...]] | None:
    try:
        chat_id = int(item["source_chat_id"])
    except (KeyError, TypeError, ValueError):
        return None
    ids = item.get("source_message_ids") or ([item["source_message_id"]] if item.get("source_message_id") else [])
    try:
        message_ids = tuple(int(value) for value in ids)
    except (TypeError, ValueError):
        return None
    return (chat_id, message_ids) if message_ids else None


async def find_cached(post_id: str, qualities: tuple[str, ...]) -> CachedMedia | None:
    """First cached quality of the post, in the order given."""
    for quality in qualities:
        track = await Track.find_one(Track.inner_id == track_inner_id(post_id, quality))
        if not track:
            continue
        for item in track.download_urls:
            pointer = _item_pointer(item)
            if pointer:
                return CachedMedia(quality=quality, source_chat_id=pointer[0], message_ids=pointer[1])
    return None


async def save_cached(
    *,
    post_id: str,
    quality: str,
    source_chat_id: int,
    message_ids: list[int],
    url: str = "",
) -> None:
    """Remember where the post's media sits; replaces the previous item."""
    if not post_id or not message_ids:
        return
    now = datetime.datetime.now(datetime.UTC)
    item = {
        "quality": quality,
        "label": quality,
        "format_id": quality,
        "source_chat_id": int(source_chat_id),
        "source_message_id": int(message_ids[0]),
        "source_message_ids": [int(value) for value in message_ids],
        "cached_at": now.isoformat(),
    }
    inner_id = track_inner_id(post_id, quality)
    collection = Track.get_pymongo_collection()
    try:
        await collection.update_one(
            {"inner_id": inner_id},
            {
                "$set": {"download_urls": [item]},
                "$setOnInsert": {
                    "inner_id": inner_id,
                    "extractor": "tiktok",
                    "site_id": post_id,
                    "url": url,
                    "add_date": now,
                },
            },
            upsert=True,
        )
    except DuplicateKeyError:
        # Two deliveries of the same post raced on the upsert; one item is enough.
        return
    logger.info(f"Cached media: inner_id={inner_id} source={source_chat_id}:{message_ids}")


async def drop_cached(post_id: str, quality: str) -> None:
    await Track.get_pymongo_collection().delete_one({"inner_id": track_inner_id(post_id, quality)})
    logger.warning(f"Dropped dead cache entry: inner_id={track_inner_id(post_id, quality)}")


async def copy_media_to_user(
    bot: Bot,
    *,
    user_id: int,
    source_chat_id: int,
    message_ids: list[int] | tuple[int, ...],
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Copy one channel post (with our caption and button) or an album.

    An album keeps its grouping with copyMessages, which can't carry a
    caption or buttons, so those follow in a short message of their own.
    """
    caption = await delivery_caption(bot)
    if len(message_ids) == 1:
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=message_ids[0],
                caption=caption,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            logger.warning(f"copy_message to user_id={user_id} failed: {exc}")
            return False
        return True

    try:
        await bot.copy_messages(
            chat_id=user_id,
            from_chat_id=source_chat_id,
            message_ids=list(message_ids)[:_COPY_MESSAGES_LIMIT],
            remove_caption=True,
        )
    except TelegramBadRequest as exc:
        logger.warning(f"copy_messages to user_id={user_id} failed: {exc}")
        return False
    try:
        await bot.send_message(user_id, caption, reply_markup=reply_markup)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Album follow-up message to user_id={user_id} failed: {exc}")
    return True


async def try_send_cached(
    bot: Bot,
    *,
    user_id: int,
    post_id: str,
    qualities: tuple[str, ...],
    language: str,
) -> str | None:
    """Serve the post from cache; returns the quality sent, or None for a miss.

    A pointer whose channel post is gone is dropped, so the request falls
    through to a fresh download that re-caches it.
    """
    cached = await find_cached(post_id, qualities)
    if cached is None:
        return None
    reply_markup = None if cached.quality == QUALITY_AUDIO else build_audio_keyboard(post_id, language)
    sent = await copy_media_to_user(
        bot,
        user_id=user_id,
        source_chat_id=cached.source_chat_id,
        message_ids=cached.message_ids,
        reply_markup=reply_markup,
    )
    if not sent:
        await drop_cached(post_id, cached.quality)
        return None
    return cached.quality
