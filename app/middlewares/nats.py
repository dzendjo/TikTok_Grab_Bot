"""NATS middleware."""
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from nats.aio.client import Client as NatsClient


class NatsMiddleware(BaseMiddleware):
    """Inject shared NATS client into handler data."""

    def __init__(self, client: NatsClient):
        self._client = client

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["nats_client"] = self._client
        return await handler(event, data)
