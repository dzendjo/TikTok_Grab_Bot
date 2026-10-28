"""Database middleware for user management."""
import datetime
from typing import Any
from collections.abc import Callable, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, User as TgUser

from app.database import User
from app.utils import get_user_id_from_update, get_logger

logger = get_logger(__name__)


class DatabaseMiddleware(BaseMiddleware):
    """Middleware to manage database user records."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any]
    ) -> Any:
        """Process update and inject user into data.

        Args:
            handler: Next handler
            event: Telegram event
            data: Handler data

        Returns:
            Handler result
        """
        # Only process if it's an Update with user
        if not isinstance(event, Update):
            return await handler(event, data)

        user_id = get_user_id_from_update(event)
        if not user_id:
            return await handler(event, data)

        # Get or create user
        db_user, is_new_user = await self._get_or_create_user(event, user_id)

        # Update activity. $set only: a full save() of this snapshot would roll
        # back premium fields written by a concurrent payment update.
        await db_user.set({
            User.visited: datetime.datetime.now(datetime.UTC),
            User.is_active: True,
        })

        # Inject user into handler data
        data['user'] = db_user
        data['is_new_user'] = is_new_user

        return await handler(event, data)

    async def _get_or_create_user(self, event: Update, user_id: int) -> tuple[User, bool]:
        """Get existing user or create new one.

        Args:
            event: Update object
            user_id: Telegram user ID

        Returns:
            User model and whether this update created it
        """
        db_user = await User.get(user_id)

        if db_user:
            # Ensure language value is always available
            if not db_user.language:
                await db_user.set({User.language: self._default_language_from_code(db_user.language_code)})
            return db_user, False

        # Create new user from telegram data
        tg_user = self._extract_telegram_user(event)
        if not tg_user:
            # Fallback: create minimal user
            db_user = User(_id=user_id)
            await db_user.save()
            return db_user, True

        db_user = User(
            _id=user_id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
            language_code=tg_user.language_code or 'en',
            language=self._default_language_from_code(tg_user.language_code),
        )
        await db_user.save()
        logger.info(f"Created new user: {user_id} ({db_user.full_name})")

        return db_user, True

    def _extract_telegram_user(self, event: Update) -> TgUser | None:
        """Extract Telegram User object from Update.

        Args:
            event: Update object

        Returns:
            Telegram User or None
        """
        if event.message and event.message.from_user:
            return event.message.from_user
        if event.callback_query and event.callback_query.from_user:
            return event.callback_query.from_user
        if event.inline_query and event.inline_query.from_user:
            return event.inline_query.from_user
        return None

    @staticmethod
    def _default_language_from_code(language_code: str | None) -> str:
        """Map telegram language code to supported bot language."""
        code = (language_code or "").lower()
        if code.startswith("uk"):
            return "uk"
        return "ru" if code.startswith("ru") else "en"
