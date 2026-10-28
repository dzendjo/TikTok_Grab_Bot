"""Ad funnel (start -> first video -> premium) reported to the ad platform via BotAdsLib.

Every ad on the platform has its own /start value, so the platform can tell which ad
brought a user and compare ads by how far their users get. The track_* functions only
schedule background work: the user never waits for the ad API, and a failure is logged
without touching the user flow.
"""
from __future__ import annotations

import asyncio
import datetime
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from botadslib import BotAdsError

from app.database import User
from app.utils import get_logger

if TYPE_CHECKING:
    from app.services.broadcast import BroadcastService

logger = get_logger(__name__)

_broadcast_service: BroadcastService | None = None
# Strong references so pending tasks aren't garbage-collected mid-flight.
_background_tasks: set[asyncio.Task] = set()


def setup(broadcast_service: BroadcastService) -> None:
    """Use the bot's BotAds client for funnel reports."""
    global _broadcast_service
    _broadcast_service = broadcast_service


def _log_task_failure(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning(f"Ad funnel task failed: {task.exception()!r}")


def _run_in_background(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_log_task_failure)


async def _report(method: str, **kwargs: Any) -> None:
    ads_client = _broadcast_service.ads_client if _broadcast_service else None
    if ads_client is None:
        return
    try:
        await getattr(ads_client, method)(**kwargs)
    except BotAdsError as exc:
        logger.warning(f"Ad funnel {method} failed for user_id={kwargs.get('telegram_user_id')}: {exc}")


async def _mark_once(user_id: int, field: str) -> bool:
    """Set `field` the first time only; True if this call set it for a user who came from an ad."""
    doc = await User.get_pymongo_collection().find_one_and_update(
        {"_id": user_id, field: None},
        {"$set": {field: datetime.datetime.now(datetime.UTC)}},
        projection={"ad_start_param": 1},
    )
    return bool(doc and doc.get("ad_start_param"))


async def _track_start(user_id: int, start_param: str, is_new_user: bool) -> None:
    if is_new_user:
        await User.get_pymongo_collection().update_one(
            {"_id": user_id, "ad_start_param": None},
            {"$set": {"ad_start_param": start_param}},
        )
    await _report("report_start", telegram_user_id=user_id, start_param=start_param, is_new_user=is_new_user)


async def _track_step(user_id: int, field: str, method: str) -> None:
    if await _mark_once(user_id, field):
        await _report(method, telegram_user_id=user_id)


def track_start(user_id: int, start_param: str | None, *, is_new_user: bool) -> None:
    """/start <value>: remember a new user's first-touch source and report the start."""
    start_param = (start_param or "").strip()
    if start_param:
        _run_in_background(_track_start(user_id, start_param, is_new_user))


def track_first_video(user_id: int) -> None:
    """The user's first delivered post (video, photos or sound)."""
    _run_in_background(_track_step(user_id, "first_video_at", "report_first_video"))


def track_premium(user_id: int) -> None:
    """The user's first paid premium plan."""
    _run_in_background(_track_step(user_id, "first_premium_at", "report_premium"))
