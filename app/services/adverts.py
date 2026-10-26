"""Legacy advert API integration (action and draw)."""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import settings
from app.utils import get_logger

logger = get_logger(__name__)


def _build_buttons(buttons: list[dict[str, Any]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    builder = InlineKeyboardBuilder()
    for button in buttons:
        text = button.get("text")
        url = button.get("url")
        if text and url:
            builder.button(text=str(text), url=str(url))
    builder.adjust(1)
    return builder.as_markup()


async def send_advert_message(bot: Bot, user_id: int, advert: dict[str, Any]) -> bool:
    """Send advert payload returned by legacy ad API."""
    keyboard = _build_buttons(advert.get("buttons", []))
    text = str(advert.get("text", ""))

    try:
        media = advert.get("media")
        if media:
            media_type = str(media.get("type", "")).lower()
            file_id = media.get("file_id")
            if not file_id:
                return False
            if media_type == "video":
                await bot.send_video(user_id, file_id, caption=text, reply_markup=keyboard)
            elif media_type == "animation":
                await bot.send_animation(user_id, file_id, caption=text, reply_markup=keyboard)
            else:
                await bot.send_photo(user_id, file_id, caption=text, reply_markup=keyboard)
        else:
            await bot.send_message(
                user_id,
                text,
                reply_markup=keyboard,
                disable_web_page_preview=not bool(advert.get("is_preview_enable")),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to send advert to {user_id}: {exc}")
        return False


def _has_ad_settings() -> bool:
    return bool(settings.ad_token and settings.api_url_get_advert)


async def send_action_advert(bot: Bot, user_id: int) -> bool:
    """Request and send action advert."""
    if not _has_ad_settings():
        return False

    headers = {"token": settings.ad_token}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                str(settings.api_url_get_advert),
                headers=headers,
                json={"type": "action"},
            ) as response:
                payload = await response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Action advert request failed: {exc}")
        return False

    advert = payload.get("advert") if payload.get("result") else None
    if not advert:
        return False
    return await send_advert_message(bot, user_id, advert)


async def send_draw_adverts(bot: Bot, user_ids: list[int], *, count: int = 100) -> dict[str, list[int]]:
    """Request one draw advert and send it to recipients."""
    results = {"success": [], "fail": []}
    if not _has_ad_settings():
        return results

    headers = {"token": settings.ad_token}
    body = {"type": "draw", "count": count}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                str(settings.api_url_get_advert),
                headers=headers,
                json=body,
            ) as response:
                payload = await response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Draw advert request failed: {exc}")
        results["fail"] = list(user_ids)
        return results

    advert = payload.get("advert") if payload.get("result") else None
    if not advert:
        results["fail"] = list(user_ids)
        return results

    for user_id in user_ids:
        ok = await send_advert_message(bot, user_id, advert)
        if ok:
            results["success"].append(user_id)
        else:
            results["fail"].append(user_id)
        await asyncio.sleep(0.1)
    return results


async def fetch_available_campaigns() -> dict[str, Any]:
    """Fetch available draw campaigns for /draw command."""
    if not settings.ad_token or not settings.api_url_available_campaigns:
        return {"result": False, "error": "Advert API is not configured"}

    headers = {"token": settings.ad_token}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                str(settings.api_url_available_campaigns),
                headers=headers,
            ) as response:
                return await response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Campaign listing failed: {exc}")
        return {"result": False, "error": str(exc)}
