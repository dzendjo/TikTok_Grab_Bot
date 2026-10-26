"""Error handling middleware."""
from typing import Any
from collections.abc import Callable, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, ErrorEvent
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.utils import get_logger

logger = get_logger(__name__)


class ErrorHandlerMiddleware(BaseMiddleware):
    """Middleware for global error handling."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any]
    ) -> Any:
        """Handle errors in update processing.

        Args:
            handler: Next handler
            event: Telegram event
            data: Handler data

        Returns:
            Handler result or None on error
        """
        try:
            return await handler(event, data)
        except TelegramForbiddenError:
            # User blocked the bot
            if isinstance(event, Update):
                from app.utils import get_user_id_from_update
                from app.database import User

                user_id = get_user_id_from_update(event)
                if user_id:
                    user = await User.get(user_id)
                    if user:
                        await user.set({User.is_active: False})
                        logger.info(f"User {user_id} blocked the bot, marked as inactive")

        except TelegramBadRequest as e:
            logger.warning(f"Telegram bad request: {e}")

        except Exception as e:
            logger.exception(f"Unhandled error: {e}")

        return None


async def error_handler(event: ErrorEvent) -> None:
    """Global error handler for dispatcher.

    Args:
        event: Error event
    """
    logger.exception(
        f"Update {event.update.update_id if event.update else 'unknown'} "
        f"caused error: {event.exception}"
    )
