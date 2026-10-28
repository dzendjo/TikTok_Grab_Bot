"""Premium: /premium, /paysupport, Telegram Stars checkout and subscription management."""
from __future__ import annotations

from collections.abc import Callable

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message, PreCheckoutQuery
from nats.aio.client import Client as NatsClient
from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.database import Payment, User
from app.handlers.user import start_action_after_payment
from app.services import ad_funnel, premium
from app.utils import get_logger

router = Router(name="payments")
logger = get_logger(__name__)

RENEW_CALLBACK_PREFIX = "prem:renew:"
RENEW_OFF = "off"
RENEW_ON = "on"


async def _build_status_text(user: User, _: Callable[..., str]) -> str:
    lines: list[str] = []
    if user.is_premium:
        lines.append(_(
            "premium/status_active_mt",
            until=premium.format_date(user.premium_until),
            auto_renew=user.premium_auto_renew,
            price=settings.premium_month_stars,
        ).strip())
    else:
        lines.append(premium.build_benefits_text(_))
        if settings.premium_enabled and user.id is not None:
            quota = await premium.get_quota_status(user.id)
            lines.append("")
            lines.append(_("premium/status_free_mt", used=quota.used, limit=quota.limit).strip())
    if user.single_credits:
        lines.append(_("premium/credits_balance_mt", count=user.single_credits).strip())
    return "\n".join(lines)


async def _build_status_keyboard(bot: Bot, user: User, language: str, _: Callable[..., str]):
    keyboard = await premium.build_buy_keyboard(bot, language, user=user)
    if user.is_premium and user.premium_subscription_charge_id:
        mode = RENEW_OFF if user.premium_auto_renew else RENEW_ON
        keyboard.inline_keyboard.append([InlineKeyboardButton(
            text=_(f"premium/btn_renew_{mode}_mt").strip(),
            callback_data=f"{RENEW_CALLBACK_PREFIX}{mode}",
        )])
    return keyboard


@router.message(Command("premium"))
async def premium_handler(message: Message, language: str, _: Callable[..., str], user: User | None = None) -> None:
    """Premium status, benefits and plan buttons."""
    if not message.from_user:
        return
    db_user = user or await User.get(message.from_user.id)
    if db_user is None:
        return
    text = await _build_status_text(db_user, _)
    try:
        keyboard = await _build_status_keyboard(message.bot, db_user, language, _)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to create invoice links: {exc}")
        keyboard = None
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


@router.message(Command("paysupport"))
async def paysupport_handler(message: Message, _: Callable[..., str]) -> None:
    """Payment support contact (required by Telegram for bots selling with Stars)."""
    await message.answer(_("premium/paysupport_mt"), disable_web_page_preview=True)


@router.callback_query(F.data.startswith(RENEW_CALLBACK_PREFIX))
async def renew_toggle_handler(callback: CallbackQuery, _: Callable[..., str], user: User | None = None) -> None:
    """Turn the Stars subscription auto-renewal off or back on."""
    mode = (callback.data or "").removeprefix(RENEW_CALLBACK_PREFIX)
    db_user = user or await User.get(callback.from_user.id)
    if db_user is None or not db_user.premium_subscription_charge_id or mode not in (RENEW_OFF, RENEW_ON):
        await callback.answer(_("premium/renew_failed_mt").strip(), show_alert=True)
        return
    try:
        await callback.bot.edit_user_star_subscription(
            user_id=callback.from_user.id,
            telegram_payment_charge_id=db_user.premium_subscription_charge_id,
            is_canceled=mode == RENEW_OFF,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"edit_user_star_subscription failed for {callback.from_user.id}: {exc}")
        await callback.answer(_("premium/renew_failed_mt").strip(), show_alert=True)
        return
    await premium.set_subscription(callback.from_user.id, auto_renew=mode == RENEW_ON)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(_(
            f"premium/renew_{mode}_done_mt",
            until=premium.format_date(db_user.premium_until),
        ))


@router.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery, _: Callable[..., str]) -> None:
    """Accept any well-formed Stars invoice of ours; must answer within 10 s."""
    ok = query.currency == premium.STARS_CURRENCY and premium.parse_payload(query.invoice_payload) is not None
    if ok:
        await query.answer(ok=True)
    else:
        logger.warning(f"Rejected pre-checkout: payload={query.invoice_payload!r} currency={query.currency}")
        await query.answer(ok=False, error_message=_("premium/checkout_invalid_mt").strip())


@router.message(F.successful_payment)
async def successful_payment_handler(
    message: Message,
    nats_client: NatsClient,
    language: str,
    _: Callable[..., str],
) -> None:
    """Grant what was paid for; renewals of the subscription also land here."""
    payment = message.successful_payment
    if payment is None or message.from_user is None:
        return
    user_id = message.from_user.id
    parsed = premium.parse_payload(payment.invoice_payload)
    if parsed is None:
        logger.error(f"Successful payment with unknown payload {payment.invoice_payload!r} from {user_id}")
        return

    days = premium.PRODUCT_DAYS.get(parsed.product, 0)
    record = Payment(
        user_id=user_id,
        product=parsed.product,
        payload=payment.invoice_payload,
        amount=payment.total_amount,
        currency=payment.currency,
        charge_id=payment.telegram_payment_charge_id,
        is_recurring=bool(payment.is_recurring),
        is_first_recurring=bool(payment.is_first_recurring),
        subscription_expiration_date=premium.from_unix(payment.subscription_expiration_date),
        granted_days=days,
        granted_credits=1 if parsed.product == premium.PRODUCT_SINGLE else 0,
    )
    try:
        await record.insert()
    except DuplicateKeyError:
        logger.warning(f"Duplicate successful_payment {payment.telegram_payment_charge_id}, ignored")
        return
    logger.info(
        f"Payment: user_id={user_id} product={parsed.product} amount={payment.total_amount} "
        f"recurring={payment.is_recurring} charge_id={payment.telegram_payment_charge_id}"
    )

    if parsed.product == premium.PRODUCT_SINGLE:
        await premium.add_credits(user_id, 1)
        await message.answer(_("premium/single_paid_mt", what=premium.action_label(parsed.action, language)))
        # Drop the paywall's pending action so a later plan purchase doesn't repeat it.
        pending = premium.pop_pending_action(user_id)
        await start_action_after_payment(
            bot=message.bot,
            nats_client=nats_client,
            user_id=user_id,
            action=parsed.action,
            video_id=parsed.video_id,
            title=pending.title if pending and pending.video_id == parsed.video_id else "",
            language=language,
            _=_,
            use_credit=True,
        )
        return

    until = await premium.grant_premium_days(user_id, days)
    ad_funnel.track_premium(user_id)
    renewal = bool(payment.is_recurring and not payment.is_first_recurring)
    if parsed.product == premium.PRODUCT_MONTH:
        # Keep the charge id of the subscription's first payment for edit_user_star_subscription.
        await premium.set_subscription(
            user_id,
            charge_id=None if renewal else payment.telegram_payment_charge_id,
            auto_renew=True,
        )
    await message.answer(_(
        "premium/renewed_mt" if renewal else "premium/activated_mt",
        until=premium.format_date(until),
    ))
    if renewal:
        return

    pending = premium.pop_pending_action(user_id)
    if pending:
        await start_action_after_payment(
            bot=message.bot,
            nats_client=nats_client,
            user_id=user_id,
            action=pending.action,
            video_id=pending.video_id,
            title=pending.title,
            language=language,
            _=_,
            use_credit=False,
        )
