"""Broadcast handlers for BotAds workflows."""
from __future__ import annotations

import html
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from botadslib import BotAdsAPIError, BotAdsError
from botadslib.models import AdItem

from app.config import settings
from app.filters import IsAdminFilter
from app.services import BroadcastService
from app.utils import get_logger

logger = get_logger(__name__)

BROADCAST_PLACEMENT = "manual_broadcast"
BROADCAST_START_CALLBACK = "broadcast_start"
BROADCAST_CANCEL_PENDING_CALLBACK = "broadcast_cancel_pending"
BROADCAST_KEEP_PENDING_CALLBACK = "broadcast_keep_pending"

router = Router(name="broadcast")
router.message.filter(IsAdminFilter())


@dataclass(slots=True)
class PendingBroadcast:
    """Pending broadcast payload for confirmation flow."""

    ad_item: AdItem
    recipients: list[str]
    campaign_key: str
    stage: str = "ready"


_pending_broadcasts: dict[int, PendingBroadcast] = {}


@dataclass(slots=True)
class PendingBatchLock:
    """Server-side pending broadcast lock details."""

    batch_id: str
    recipients_count: int | None = None


_pending_batch_locks: dict[int, PendingBatchLock] = {}


class AwaitingBroadcastConfirm(BaseFilter):
    """Pass only messages from users waiting broadcast confirmation."""

    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        pending = _pending_broadcasts.get(message.from_user.id)
        return bool(pending and pending.stage == "confirm")


def _is_pending_batch_error(exc: BotAdsError) -> bool:
    return "Previous broadcast batch is pending" in str(exc)


def _extract_pending_batch_id(exc: BotAdsError) -> str | None:
    if not isinstance(exc, BotAdsAPIError):
        return None
    payload = exc.payload
    if not isinstance(payload, dict):
        return None
    value = payload.get("pendingBatchId")
    if not value:
        return None
    return str(value)


def _extract_pending_recipients_count(exc: BotAdsError) -> int | None:
    if not isinstance(exc, BotAdsAPIError):
        return None
    payload = exc.payload
    if not isinstance(payload, dict):
        return None
    value = payload.get("recipientsCount")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_pending_batch_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Відмінити стару", callback_data=BROADCAST_CANCEL_PENDING_CALLBACK),
                InlineKeyboardButton(text="Залишити", callback_data=BROADCAST_KEEP_PENDING_CALLBACK),
            ]
        ]
    )


def _build_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Start broadcast", callback_data=BROADCAST_START_CALLBACK)]
        ]
    )


def _build_confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Yes"), KeyboardButton(text="No")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _chunk_text(text: str, max_size: int = 3800) -> list[str]:
    if len(text) <= max_size:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_size:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, max_size)
        if split_at <= 0:
            split_at = max_size
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


def _readable(value: Any, *, empty: str = "not set") -> str:
    if value is None:
        return empty
    if isinstance(value, str) and not value.strip():
        return empty
    return str(value)


def _to_number(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _status_icon(status: Any) -> str:
    value = str(status or "").strip().lower()
    if value in {"active", "approved", "completed"}:
        return "✅"
    if value in {"pending", "processing"}:
        return "⏳"
    if value in {"rejected", "inactive", "paused", "expired", "failed", "disabled"}:
        return "❌"
    return "ℹ️"


def _campaign_status_ua(status: Any) -> str:
    value = str(status or "").strip().lower()
    mapping = {
        "active": "активна",
        "inactive": "неактивна",
        "paused": "на паузі",
        "draft": "чернетка",
        "completed": "завершена",
    }
    return mapping.get(value, value or "невідомо")


def _format_money(value: Any) -> str:
    if value is None or value == "":
        return "$0"
    try:
        amount = Decimal(str(value))
        amount = amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        text = f"{amount:.6f}".rstrip("0").rstrip(".")
        if text in {"", "-0"}:
            text = "0"
        return f"${text}"
    except (InvalidOperation, ValueError):
        return f"${value}"


def _format_pricing(pricing: dict[str, Any]) -> str:
    if pricing.get("cpm") not in (None, ""):
        return f"CPM ${pricing.get('cpm')}"
    if pricing.get("cpc") not in (None, ""):
        return f"CPC ${pricing.get('cpc')}"
    return "CPM $0"


def _render_compact_campaign(campaign: dict[str, Any]) -> str:
    name = html.escape(_readable(campaign.get("name"), empty="Unnamed campaign"))
    status = campaign.get("status")
    status_icon = _status_icon(status)
    status_text = _campaign_status_ua(status)

    ads = campaign.get("ads") if isinstance(campaign.get("ads"), list) else []
    lines = [f"📣 <b>{name}</b> — {status_icon} {status_text} ({len(ads)} оголошення)"]

    for ad in ads:
        if not isinstance(ad, dict):
            continue
        ad_status_icon = _status_icon(ad.get("status"))
        ad_name = html.escape(_readable(ad.get("name"), empty="ad"))
        stats = ad.get("stats") if isinstance(ad.get("stats"), dict) else {}
        pricing = ad.get("pricing") if isinstance(ad.get("pricing"), dict) else {}
        approval = ad.get("botApproval") if isinstance(ad.get("botApproval"), dict) else {}
        bot_icon = _status_icon(approval.get("status"))

        impressions = _to_number(stats.get("impressions"), default=0)
        spent = _format_money(stats.get("spent"))
        pricing_text = _format_pricing(pricing)

        lines.append(
            f"{ad_status_icon} <b>{ad_name}</b>: "
            f"{impressions}👁️ / {spent} • {pricing_text} • bot {bot_icon}"
        )

    return "\n".join(lines)


def _nested_get(data: dict[str, Any], path: tuple[str, ...]) -> Any | None:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _pick_first(data: dict[str, Any], paths: list[tuple[str, ...]]) -> Any | None:
    for path in paths:
        value = _nested_get(data, path)
        if value is not None and value != "":
            return value
    return None


def _format_price(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, dict):
        amount = value.get("amount") or value.get("value")
        currency = value.get("currency") or ""
        if amount is not None:
            return f"{amount} {currency}".strip()
    return str(value)


def _telegram_base_language(language_code: str | None, *, default: str = settings.default_language) -> str:
    if not language_code:
        return default
    normalized = language_code.strip().lower().replace("_", "-")
    base = normalized.split("-", 1)[0]
    if len(base) == 2 and base.isalpha():
        return base
    return default


def _extract_campaign_name(ad_item: AdItem) -> str:
    raw = ad_item.raw
    value = _pick_first(
        raw,
        [
            ("campaignName",),
            ("campaignTitle",),
            ("campaign", "name"),
            ("campaign", "title"),
            ("campaignMeta", "name"),
            ("campaignMeta", "title"),
        ],
    )
    if value is not None:
        return str(value)

    campaign_id = _pick_first(
        raw,
        [
            ("campaignId",),
            ("campaign", "_id"),
            ("campaign", "id"),
        ],
    )
    if campaign_id is not None:
        return f"ID: {campaign_id}"

    return "N/A"


def _extract_campaign_key(ad_item: AdItem) -> str:
    raw = ad_item.raw
    value = _pick_first(
        raw,
        [
            ("campaignId",),
            ("campaign", "_id"),
            ("campaign", "id"),
            ("campaignName",),
            ("campaignTitle",),
            ("campaign", "name"),
            ("campaign", "title"),
        ],
    )
    if value is None:
        return BROADCAST_PLACEMENT

    campaign_key = str(value).strip()
    return campaign_key or BROADCAST_PLACEMENT


def _extract_campaign_price(ad_item: AdItem) -> str:
    raw = ad_item.raw
    value = _pick_first(
        raw,
        [
            ("price",),
            ("campaignPrice",),
            ("cpm",),
            ("cpc",),
            ("cost",),
            ("campaign", "price"),
            ("campaign", "cpm"),
            ("campaign", "cpc"),
        ],
    )
    return _format_price(value)


def _extract_planned_count(ad_item: AdItem) -> str:
    if ad_item.broadcast_batch and ad_item.broadcast_batch.recipients_count:
        return str(ad_item.broadcast_batch.recipients_count)

    raw = ad_item.raw
    value = _pick_first(
        raw,
        [
            ("plannedRecipientsCount",),
            ("targetRecipientsCount",),
            ("campaign", "plannedRecipientsCount"),
            ("campaign", "targetRecipientsCount"),
        ],
    )
    return str(value) if value is not None else "N/A"


def _count_custom_emoji_entities(ad_item: AdItem) -> int:
    total = 0
    for entity in [*ad_item.creative.entities, *ad_item.creative.caption_entities]:
        if not isinstance(entity, dict):
            continue
        entity_type = str(entity.get("type") or "").strip().lower().replace("_", "")
        if entity_type == "customemoji":
            total += 1
    return total


@router.message(Command("ads"))
async def handle_ads(message: Message, broadcast_service: BroadcastService) -> None:
    """Show available campaigns."""
    try:
        campaigns = await broadcast_service.get_campaigns(include_inactive=False)
    except BotAdsError as exc:
        logger.error(f"Campaigns request failed: {exc}")
        await message.answer("Ads are temporarily unavailable.")
        return

    if not campaigns:
        await message.answer(
            "Немає активних кампаній для вашого бота.\n\n"
            "<b>запуск розсилки</b> - /broadcast",
            parse_mode="HTML",
        )
        return

    blocks = [_render_compact_campaign(campaign) for campaign in campaigns if isinstance(campaign, dict)]
    text = "\n\n".join(blocks)

    for chunk in _chunk_text(text):
        await message.answer(chunk, parse_mode="HTML")
    await message.answer("<b>запуск розсилки</b> - /broadcast", parse_mode="HTML")


@router.message(Command("broadcast"))
async def handle_broadcast(message: Message, broadcast_service: BroadcastService) -> None:
    """Prepare broadcast campaign preview and ask for confirmation."""
    if not message.from_user:
        return

    existing_pending = _pending_broadcasts.get(message.from_user.id)
    if existing_pending:
        if existing_pending.stage == "confirm":
            await message.answer(
                "You already have a pending broadcast confirmation. Reply Yes/No.",
                reply_markup=_build_confirm_keyboard(),
            )
            return
        await message.answer(
            "You already have a prepared broadcast preview. Press Start broadcast.",
            reply_markup=_build_start_keyboard(),
        )
        return

    recipients = [message.from_user.id]
    request_language = _telegram_base_language(message.from_user.language_code)
    try:
        ad_item, normalized_recipients = await broadcast_service.get_broadcast_ad(
            recipient_telegram_ids=recipients,
            placement=BROADCAST_PLACEMENT,
            language=request_language,
        )
    except BotAdsError as exc:
        logger.error(f"Broadcast ad request failed: {exc}")
        if _is_pending_batch_error(exc):
            pending_batch_id = _extract_pending_batch_id(exc)
            if pending_batch_id:
                pending_recipients_count = _extract_pending_recipients_count(exc)
                if pending_recipients_count is None:
                    # Fallback to request size (for preview flow we request exactly one recipient).
                    pending_recipients_count = len(recipients)
                _pending_batch_locks[message.from_user.id] = PendingBatchLock(
                    batch_id=pending_batch_id,
                    recipients_count=pending_recipients_count,
                )
                await message.answer(
                    "Є незавершена попередня розсилка.\n"
                    "Відмінити стару розсилку і розблокувати нову?",
                    reply_markup=_build_pending_batch_keyboard(),
                )
                return
            await message.answer(
                "Broadcast is locked by a pending batch. Finish previous /broadcast confirmation "
                "(Yes/No) or wait until the batch is released on API side."
            )
            return
        await message.answer("Broadcast is temporarily unavailable.")
        return

    if not ad_item:
        await message.answer("No available campaigns for your bot...")
        return

    _pending_batch_locks.pop(message.from_user.id, None)
    campaign_key = _extract_campaign_key(ad_item)

    try:
        db_recipient_count = await broadcast_service.get_db_broadcast_recipient_count(campaign_key)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Failed to calculate DB recipient count: {exc}")
        db_recipient_count = int(_extract_planned_count(ad_item)) if _extract_planned_count(ad_item).isdigit() else 0

    _pending_broadcasts[message.from_user.id] = PendingBroadcast(
        ad_item=ad_item,
        recipients=normalized_recipients,
        campaign_key=campaign_key,
        stage="ready",
    )

    try:
        await broadcast_service.send_ad_to_chat(chat_id=message.chat.id, ad_item=ad_item)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Failed to send broadcast preview: {exc}")
        await message.answer("Broadcast preview is temporarily unavailable.")
        return

    await message.answer(
        "Broadcast campaign found:\n"
        f"Campaign: {_extract_campaign_name(ad_item)}\n"
        f"Price: {_extract_campaign_price(ad_item)}\n"
        f"Planned recipients: {db_recipient_count}\n"
        f"Custom emoji entities: {_count_custom_emoji_entities(ad_item)}\n"
        f"Requested language: {request_language}\n"
        f"Applied language: {ad_item.language or 'base'}\n"
        "Press the button to continue.",
        reply_markup=_build_start_keyboard(),
    )


@router.callback_query(F.data == BROADCAST_START_CALLBACK)
async def handle_broadcast_start_callback(callback: CallbackQuery) -> None:
    """Move broadcast flow to final Yes/No confirmation."""
    await callback.answer()
    if not callback.from_user:
        return

    pending = _pending_broadcasts.get(callback.from_user.id)
    if pending is None:
        if callback.message:
            await callback.message.answer("No pending broadcast. Use /broadcast first.")
        return

    pending.stage = "confirm"
    if callback.message:
        await callback.message.answer(
            "Are you sure you want to start this broadcast?",
            reply_markup=_build_confirm_keyboard(),
        )


@router.callback_query(F.data == BROADCAST_CANCEL_PENDING_CALLBACK)
async def handle_broadcast_cancel_pending(
    callback: CallbackQuery,
    broadcast_service: BroadcastService,
) -> None:
    """Finalize stale pending batch and unlock new /broadcast requests."""
    await callback.answer()
    if not callback.from_user:
        return

    lock = _pending_batch_locks.get(callback.from_user.id)
    if not lock:
        if callback.message:
            await callback.message.answer("Немає pending розсилки для скасування.")
        return

    pending_batch_id = lock.batch_id
    known_failed_recipient_ids: list[str] | None = None
    pending = _pending_broadcasts.get(callback.from_user.id)
    if pending and pending.ad_item.broadcast_batch and pending.ad_item.broadcast_batch.batch_id == pending_batch_id:
        known_failed_recipient_ids = [str(value) for value in pending.recipients]
    elif lock.recipients_count == 1:
        known_failed_recipient_ids = [str(callback.from_user.id)]

    finalize_attempts: list[list[str]] = []
    if known_failed_recipient_ids is not None:
        finalize_attempts.append(known_failed_recipient_ids)
    if not finalize_attempts or finalize_attempts[-1]:
        # Fallback: some API deployments allow releasing pending batch by batchId only.
        finalize_attempts.append([])

    last_finalize_error: BotAdsError | None = None
    for failed_recipient_ids in finalize_attempts:
        try:
            await broadcast_service.finalize_broadcast_batch_by_id(
                batch_id=pending_batch_id,
                failed_recipient_telegram_ids=failed_recipient_ids,
            )
            last_finalize_error = None
            break
        except BotAdsError as exc:
            last_finalize_error = exc
            logger.error(f"Pending broadcast finalize failed for batch {pending_batch_id}: {exc}")

    if last_finalize_error is not None:
        if callback.message:
            if known_failed_recipient_ids is None:
                await callback.message.answer(
                    "Не вдалося скасувати попередню розсилку: API не прийняв batchId без списку одержувачів. "
                    "Спробуйте ще раз через кілька секунд."
                )
            else:
                await callback.message.answer(
                    "Не вдалося скасувати попередню розсилку. Спробуйте ще раз через кілька секунд."
                )
        return

    _pending_batch_locks.pop(callback.from_user.id, None)
    _pending_broadcasts.pop(callback.from_user.id, None)

    if callback.message:
        await callback.message.answer("Попередню розсилку скасовано. Надішліть /broadcast ще раз.")


@router.callback_query(F.data == BROADCAST_KEEP_PENDING_CALLBACK)
async def handle_broadcast_keep_pending(callback: CallbackQuery) -> None:
    """Keep pending batch and exit unlock flow."""
    await callback.answer()
    if callback.message:
        await callback.message.answer("Ок, залишаємо поточну pending розсилку.")


@router.message(AwaitingBroadcastConfirm(), F.text)
async def handle_broadcast_confirmation(message: Message, broadcast_service: BroadcastService) -> None:
    """Start broadcast after explicit Yes confirmation."""
    if not message.from_user or not message.text:
        return

    pending = _pending_broadcasts.get(message.from_user.id)
    if pending is None or pending.stage != "confirm":
        return

    decision = message.text.strip().lower()
    if decision not in {"yes", "no"}:
        await message.answer(
            "Please use the confirmation buttons: Yes or No.",
            reply_markup=_build_confirm_keyboard(),
        )
        return

    if decision == "no":
        try:
            await broadcast_service.finalize_broadcast_batch(
                ad_item=pending.ad_item,
                failed_recipient_telegram_ids=pending.recipients,
            )
        except BotAdsError as exc:
            logger.error(f"Broadcast cancel finalize failed: {exc}")
        _pending_broadcasts.pop(message.from_user.id, None)
        await message.answer("Broadcast canceled.", reply_markup=ReplyKeyboardRemove())
        return

    _pending_broadcasts.pop(message.from_user.id, None)
    await message.answer("Starting broadcast...", reply_markup=ReplyKeyboardRemove())

    try:
        await broadcast_service.finalize_broadcast_batch(
            ad_item=pending.ad_item,
            failed_recipient_telegram_ids=pending.recipients,
        )
    except BotAdsError as exc:
        logger.error(f"Broadcast preview finalize failed: {exc}")
        await message.answer("Не вдалося завершити прев'ю розсилки. Спробуйте /broadcast ще раз.")
        return

    await broadcast_service.start_broadcast(
        admin_user_id=message.from_user.id,
        campaign_key=pending.campaign_key,
        placement=BROADCAST_PLACEMENT,
    )
