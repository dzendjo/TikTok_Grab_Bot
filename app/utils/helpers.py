"""Helper utilities."""
import re
from typing import TypeVar
from collections.abc import Sequence

from aiogram.types import Update

T = TypeVar("T")


def get_user_id_from_update(update: Update) -> int | None:
    """Extract user_id from any Update type.

    Args:
        update: Telegram Update object

    Returns:
        User ID or None if not found
    """
    if update.message and update.message.from_user:
        return update.message.from_user.id
    if update.callback_query and update.callback_query.from_user:
        return update.callback_query.from_user.id
    if update.inline_query and update.inline_query.from_user:
        return update.inline_query.from_user.id
    if update.chosen_inline_result and update.chosen_inline_result.from_user:
        return update.chosen_inline_result.from_user.id
    if update.shipping_query and update.shipping_query.from_user:
        return update.shipping_query.from_user.id
    if update.pre_checkout_query and update.pre_checkout_query.from_user:
        return update.pre_checkout_query.from_user.id
    if update.poll_answer and update.poll_answer.user:
        return update.poll_answer.user.id
    if update.my_chat_member and update.my_chat_member.from_user:
        return update.my_chat_member.from_user.id
    if update.chat_member and update.chat_member.from_user:
        return update.chat_member.from_user.id
    if update.chat_join_request and update.chat_join_request.from_user:
        return update.chat_join_request.from_user.id
    if update.purchased_paid_media and update.purchased_paid_media.from_user:
        return update.purchased_paid_media.from_user.id
    return None


def split_list(base_list: Sequence[T], chunk_size: int) -> list[list[T]]:
    """Split list into chunks of specified size.

    Args:
        base_list: List to split
        chunk_size: Size of each chunk

    Returns:
        List of chunks
    """
    return [list(base_list[i:i + chunk_size]) for i in range(0, len(base_list), chunk_size)]


def sanitize_text(user_input: str) -> str:
    """Sanitize user input by removing HTML tags and leading @ symbols.

    Args:
        user_input: Raw user input

    Returns:
        Sanitized text
    """
    # Remove HTML tags
    no_html = re.sub(r"<.*?>", "", user_input)
    # Remove leading @ symbols
    no_at_start = re.sub(r"^@+", "", no_html)
    # Strip whitespace
    return no_at_start.strip()


def split_text_by_paragraphs(text: str, max_length: int = 4096) -> list[str]:
    """Split long text into chunks by paragraphs, respecting Telegram's message limit.

    Args:
        text: Text to split
        max_length: Maximum length per chunk (default: 4096 - Telegram's limit)

    Returns:
        List of text chunks
    """
    paragraphs = text.split('\n')
    chunks = []
    current_chunk = ''

    for paragraph in paragraphs:
        # Check if adding this paragraph would exceed max length
        if len(current_chunk) + len(paragraph) + 2 > max_length:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = paragraph
        else:
            if current_chunk:
                current_chunk += '\n' + paragraph
            else:
                current_chunk = paragraph

    # Add the last chunk if it's not empty
    if current_chunk:
        chunks.append(current_chunk)

    return chunks
