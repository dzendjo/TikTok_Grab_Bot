"""Localization middleware."""
from typing import Any
from collections.abc import Callable, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from jinja2 import Environment

from app.config import settings
from app.locales import localization


class LocalizationMiddleware(BaseMiddleware):
    """Middleware to inject localization into handler data."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any]
    ) -> Any:
        """Process update and inject localization.

        Args:
            handler: Next handler
            event: Telegram event
            data: Handler data

        Returns:
            Handler result
        """
        # Get user from previous middleware
        user = data.get('user')

        # Determine language
        if user and hasattr(user, 'language') and user.language:
            language = user.language
        else:
            language = settings.default_language

        # Inject Jinja2 environment and helper function
        data['i18n'] = localization.get_environment(language)
        data['_'] = lambda path, **kwargs: localization.get_text(language, path, **kwargs)
        data['language'] = language

        return await handler(event, data)
