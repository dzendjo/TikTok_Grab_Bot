"""Logging middleware for incoming updates."""
from typing import Any, Callable, Dict, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Update
from app.utils.logger import get_logger

logger = get_logger(__name__)


class LoggingMiddleware(BaseMiddleware):
    """Middleware for logging incoming updates."""

    async def __call__(
        self,
        handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        user_info = f"User(id={user.id}, username={user.username})" if user else "Unknown User"
        
        update_type = event.event_type
        
        # Log basic info about the update
        if event.message:
            payload = f"text='{event.message.text}'" if event.message.text else "media/other"
            logger.info(f"Incoming message from {user_info}: {payload}")
        elif event.callback_query:
            logger.info(f"Incoming callback query from {user_info}: data='{event.callback_query.data}'")
        else:
            logger.info(f"Incoming update type: {update_type} from {user_info}")

        return await handler(event, data)
