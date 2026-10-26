"""Admin handlers for service and mailing workflows."""
from __future__ import annotations

import asyncio
import datetime
import html
import re
from collections.abc import Callable

import orjson
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, Message
from nats.aio.client import Client as NatsClient
from nats.errors import NoRespondersError, TimeoutError

from app.constants import DOWNLOAD_QUALITIES
from app.database import Payment, User
from app.filters import IsAdminFilter
from app.services import premium
from app.services import fetch_available_campaigns, request_media_download, start_draw

router = Router(name="admin")
router.message.filter(IsAdminFilter())

TEST_NATS_URL = "https://www.tiktok.com/@khaby.lame/video/7694335322922044703"
TEST_DOWNLOAD_DEFAULT_QUALITY = "video"


class AdminStates(StatesGroup):
    waiting_cleanbase_confirmation = State()
    waiting_draw_confirmation = State()


@router.message(Command("admin"))
async def admin_help_handler(message: Message, _: Callable[..., str]) -> None:
    """Show available admin service commands."""
    await message.answer(_("admin/help_mt"))


@router.message(Command("base"))
async def base_stats_handler(message: Message, _: Callable[..., str]) -> None:
    """Show users count."""
    total_count = await User.count()
    await message.answer(_("admin/base_count_mt", total_count=total_count))


@router.message(Command("test1"))
async def test_download_handler(
    message: Message,
    command: CommandObject,
    nats_client: NatsClient,
    language: str,
    _: Callable[..., str],
) -> None:
    """Request download via NATS for hardcoded test URL: /test1 [video|audio]."""
    quality = (command.args or TEST_DOWNLOAD_DEFAULT_QUALITY).strip().lower()
    if quality not in DOWNLOAD_QUALITIES:
        await message.answer(_("admin/test_download_bad_quality_mt", qualities=", ".join(DOWNLOAD_QUALITIES)))
        return

    user_id = message.from_user.id if message.from_user else 0
    message_id = message.message_id
    await message.answer(_("admin/test_download_started_mt", quality=quality))
    try:
        result = await request_media_download(
            TEST_NATS_URL,
            nats_client,
            user_id=user_id,
            message_id=message_id,
            quality=quality,
            user_language=language,
            language=language,
        )
    except (TimeoutError, NoRespondersError):
        await message.answer(_("admin/nats_download_timeout_mt"))
        return
    except Exception as exc:  # noqa: BLE001
        await message.answer(_("admin/nats_download_failed_mt", error=exc))
        return

    await message.answer(
        _("admin/nats_download_done_mt", status=result.get("status"), result=result.get("result"))
    )


def _cleanbase_query() -> dict:
    """Inactive users, except ones with paid premium still running."""
    return {"is_active": False, "premium_until": {"$not": {"$gt": datetime.datetime.now(datetime.UTC)}}}


@router.message(Command("cleanbase"))
async def clean_base_handler(message: Message, state: FSMContext, _: Callable[..., str]) -> None:
    """Ask admin confirmation for inactive users deletion."""
    num_non_active = await User.find(_cleanbase_query()).count()
    await state.set_state(AdminStates.waiting_cleanbase_confirmation)
    await message.answer(_("admin/cleanbase_confirm_mt", num_non_active=num_non_active))


@router.message(AdminStates.waiting_cleanbase_confirmation)
async def clean_base_confirmation(message: Message, state: FSMContext, _: Callable[..., str]) -> None:
    """Process cleanbase confirmation."""
    if (message.text or "").strip().lower() == "yes":
        result = await User.find(_cleanbase_query()).delete()
        await message.answer(_("admin/cleanbase_removed_mt", deleted_count=result.deleted_count))
    else:
        await message.answer(_("admin/cancelled_mt"))
    await state.clear()


@router.message(Command("getbase"))
async def get_base_handler(message: Message, _: Callable[..., str]) -> None:
    """Export user IDs as JSON document."""
    loading_message = await message.answer(_("admin/getbase_loading_mt"))

    users = await User.find_all().to_list()
    ids = [user.id for user in users]

    payload = BufferedInputFile(
        file=orjson.dumps(ids),
        filename=f"{message.bot.id}.json",
    )
    await message.answer_document(payload)
    await loading_message.delete()


@router.message(Command("draw"))
async def draw_handler(message: Message, state: FSMContext, _: Callable[..., str], language: str) -> None:
    """Prepare draw campaign and ask confirmation."""
    response = await fetch_available_campaigns()
    if not response.get("result"):
        await message.answer(_("admin/draw_error_mt", error=response.get("error", _("admin/unknown_error_mt"))))
        return

    available_campaigns = response.get("draw") or {}
    if not available_campaigns:
        await message.answer(_("draw/no_campaigns_mt"))
        return

    await message.answer(_("draw/mt", campaigns=available_campaigns))
    await state.set_state(AdminStates.waiting_draw_confirmation)

    raw_campaign_name = "-".join(available_campaigns.keys())
    campaign_name = re.sub(r"\W+", "-", raw_campaign_name).strip("-") or "campaign"
    await state.update_data(campaign_name=campaign_name, language=language)


@router.message(AdminStates.waiting_draw_confirmation)
async def draw_confirmation_handler(message: Message, state: FSMContext, _: Callable[..., str]) -> None:
    """Confirm and start draw."""
    data = await state.get_data()
    await state.clear()

    if (message.text or "").strip().lower() == "start":
        campaign_name = data.get("campaign_name", "campaign")
        language = data.get("language", "en")
        asyncio.create_task(start_draw(message.bot, message.from_user.id, campaign_name, language))
        await message.answer(_("draw/start_draw_mt"))
    else:
        await message.answer(_("draw/not_start_draw_mt"))


@router.message(Command("givepremium"))
async def give_premium_handler(message: Message, command: CommandObject, _: Callable[..., str]) -> None:
    """/givepremium <user_id> <days>: add days (stacking); 0 days removes premium."""
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].lstrip("-").isdigit() or not args[1].lstrip("-").isdigit():
        await message.answer(_("admin/givepremium_usage_mt"))
        return
    user_id, days = int(args[0]), int(args[1])
    if await User.get(user_id) is None:
        await message.answer(_("admin/user_not_found_mt", user_id=user_id))
        return
    if days <= 0:
        await premium.revoke_premium(user_id)
        await message.answer(_("admin/premium_revoked_mt", user_id=user_id))
        return
    until = await premium.grant_premium_days(user_id, days)
    await message.answer(_("admin/premium_granted_mt", user_id=user_id, until=premium.format_date(until)))


@router.message(Command("refund"))
async def refund_handler(message: Message, command: CommandObject, _: Callable[..., str]) -> None:
    """/refund <charge_id>: return the Stars and take back what the payment granted."""
    charge_id = (command.args or "").strip()
    payment = await Payment.find_one(Payment.charge_id == charge_id) if charge_id else None
    if payment is None:
        await message.answer(_("admin/refund_usage_mt"))
        return
    if payment.refunded:
        await message.answer(_("admin/refund_already_mt", charge_id=charge_id))
        return
    try:
        await message.bot.refund_star_payment(user_id=payment.user_id, telegram_payment_charge_id=charge_id)
    except Exception as exc:  # noqa: BLE001
        await message.answer(_("admin/refund_failed_mt", error=html.escape(str(exc), quote=False)))
        return

    await Payment.get_pymongo_collection().update_one({"_id": payment.id}, {"$set": {"refunded": True}})
    if payment.granted_days:
        await premium.shift_premium_days(payment.user_id, -payment.granted_days)
    if payment.granted_credits:
        await premium.add_credits(payment.user_id, -payment.granted_credits)
    if payment.product == premium.PRODUCT_MONTH:
        user = await User.get(payment.user_id)
        if user and user.premium_subscription_charge_id:
            try:
                await message.bot.edit_user_star_subscription(
                    user_id=payment.user_id,
                    telegram_payment_charge_id=user.premium_subscription_charge_id,
                    is_canceled=True,
                )
            except Exception as exc:  # noqa: BLE001
                await message.answer(_("admin/refund_cancel_failed_mt", error=html.escape(str(exc), quote=False)))
        await premium.set_subscription(payment.user_id, auto_renew=False)
    await message.answer(_(
        "admin/refund_done_mt",
        user_id=payment.user_id,
        amount=payment.amount,
        days=payment.granted_days,
        credits=payment.granted_credits,
    ))


@router.message(Command("premiumstats"))
async def premium_stats_handler(message: Message, _: Callable[..., str]) -> None:
    """Active premium users and Stars revenue."""
    now = datetime.datetime.now(datetime.UTC)
    users = User.get_pymongo_collection()
    active = await users.count_documents({"premium_until": {"$gt": now}})
    renewing = await users.count_documents({"premium_until": {"$gt": now}, "premium_auto_renew": True})

    async def _sum(match: dict) -> tuple[int, int]:
        cursor = await Payment.get_pymongo_collection().aggregate([
            {"$match": {"refunded": {"$ne": True}, **match}},
            {"$group": {"_id": None, "count": {"$sum": 1}, "stars": {"$sum": "$amount"}}},
        ])
        rows = await cursor.to_list(1)
        return (rows[0]["count"], rows[0]["stars"]) if rows else (0, 0)

    count_30d, stars_30d = await _sum({"created_at": {"$gte": now - datetime.timedelta(days=30)}})
    count_all, stars_all = await _sum({})
    recent = [
        {
            "date": premium.format_date(payment.created_at),
            "user_id": payment.user_id,
            "product": payment.product,
            "amount": payment.amount,
            "refunded": payment.refunded,
            "charge_id": html.escape(payment.charge_id),
        }
        for payment in await Payment.find_all().sort("-created_at").limit(5).to_list()
    ]
    await message.answer(_(
        "admin/premium_stats_mt",
        active=active,
        renewing=renewing,
        count_30d=count_30d,
        stars_30d=stars_30d,
        count_all=count_all,
        stars_all=stars_all,
        recent=recent,
    ))
