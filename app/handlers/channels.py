"""Channel handlers: whitelist guard + uploaded media routing."""
from __future__ import annotations

from aiogram import Bot, Router
from aiogram.types import ChatMemberUpdated, Message

from app.config import settings
from app.services.nats_meta import process_uploaded_media_channel_post
from app.utils import get_logger

router = Router(name="channels")
logger = get_logger(__name__)

def _allowed_channel_ids() -> set[int]:
    """Build whitelist from source channels and BotAds cache channel only."""
    allowed: set[int] = set()

    for channel_id in settings.allowed_source_channel_ids or []:
        try:
            allowed.add(int(channel_id))
        except (TypeError, ValueError):
            continue

    if settings.ad_cache_channel_id is not None:
        try:
            allowed.add(int(settings.ad_cache_channel_id))
        except (TypeError, ValueError):
            pass

    return allowed


def _is_allowed_channel(chat_id: int) -> bool:
    return chat_id in _allowed_channel_ids()


@router.channel_post()
async def channel_post_handler(message: Message, bot: Bot) -> None:
    """Leave unknown channels and process media only in allowed ones."""
    chat = message.chat
    if chat.type != "channel":
        return

    if not _is_allowed_channel(chat.id):
        try:
            await bot.leave_chat(chat.id)
            logger.info(f"Left channel {chat.title or ''} ({chat.id}) — not in whitelist")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to leave channel {chat.title or ''} ({chat.id}): {exc}")
        return

    await process_uploaded_media_channel_post(message, bot)


@router.my_chat_member()
async def my_chat_member_handler(update: ChatMemberUpdated, bot: Bot) -> None:
    """Leave channel right after being added if it is not in whitelist."""
    chat = update.chat
    if chat.type != "channel":
        return

    if _is_allowed_channel(chat.id):
        logger.info(f"Stay in allowed channel {chat.title or ''} ({chat.id})")
        return

    try:
        await bot.leave_chat(chat.id)
        logger.info(f"Left channel {chat.title or ''} ({chat.id}) — not in whitelist (my_chat_member)")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to leave channel {chat.title or ''} ({chat.id}): {exc}")
