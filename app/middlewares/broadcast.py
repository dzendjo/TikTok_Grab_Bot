"""Broadcast middleware to inject BroadcastService into handlers."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.services.broadcast import BroadcastService


class BroadcastMiddleware(BaseMiddleware):
    """Inject shared broadcast service to handler dependencies."""

    def __init__(self, broadcast_service: BroadcastService):
        self.broadcast_service = broadcast_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["broadcast_service"] = self.broadcast_service
        return await handler(event, data)
