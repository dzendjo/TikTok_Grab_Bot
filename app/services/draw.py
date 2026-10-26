"""Draw (broadcast) workflow for legacy advert API."""
from __future__ import annotations

import asyncio
import datetime
from itertools import islice

from aiogram import Bot

from app.constants import DRAW_BATCH_SIZE, DRAW_DELAY_SECONDS
from app.database import Advert, User
from app.locales import localization
from app.services.adverts import send_draw_adverts
from app.utils import get_logger

logger = get_logger(__name__)


def _iter_chunks(values: list[int], chunk_size: int):
    iterator = iter(values)
    while True:
        chunk = list(islice(iterator, chunk_size))
        if not chunk:
            break
        yield chunk


async def start_draw(bot: Bot, admin_user_id: int, draw_name: str, language: str) -> None:
    """Start legacy draw campaign and send final report to admin."""
    campaign = await Advert.find_one(Advert.name == draw_name)
    if not campaign:
        campaign = Advert(name=draw_name, admin_user_id=admin_user_id, type="draw")
        await campaign.save()

    done_users = set(campaign.done_users)
    # Premium users don't get ad campaigns.
    now = datetime.datetime.now(datetime.UTC)
    active_users = await User.find({"is_active": True, "premium_until": {"$not": {"$gt": now}}}).to_list()
    pending_user_ids = [user.id for user in active_users if user.id not in done_users]

    total_success = 0
    total_fail = 0

    for batch in _iter_chunks(pending_user_ids, DRAW_BATCH_SIZE):
        results = await send_draw_adverts(bot, batch)
        total_success += len(results["success"])
        total_fail += len(results["fail"])

        campaign.done_users.extend(batch)
        await campaign.save()

        for failed_user_id in results["fail"]:
            user = await User.get(failed_user_id)
            if not user:
                continue
            await user.set({User.is_active: False})

        await asyncio.sleep(DRAW_DELAY_SECONDS)

    mt = localization.get_text(language, "draw/end_draw_mt")
    report = f"{mt}\n\n✅ {total_success}\n❌ {total_fail}"
    await bot.send_message(admin_user_id, report)
    logger.info(f"Draw '{draw_name}' finished: success={total_success}, fail={total_fail}")
