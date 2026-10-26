"""Admin filters."""
from aiogram.filters import Filter
from aiogram.types import Message, CallbackQuery

from app.config import settings


class IsAdminFilter(Filter):
    """Filter to check if user is admin."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        """Check if user is in admin list.

        Args:
            event: Message or CallbackQuery event

        Returns:
            True if user is admin, False otherwise
        """
        user_id = event.from_user.id if event.from_user else None
        return user_id in settings.admin_ids
