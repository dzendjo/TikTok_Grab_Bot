"""Database initialization and utilities."""
import logging
from typing import Any

from beanie import init_beanie
from pymongo import ASCENDING, AsyncMongoClient
from pymongo.errors import OperationFailure

from .models import DownloadRequest, Advert, Payment, Track, User, DOWNLOAD_REQUESTS_TTL_SECONDS

logger = logging.getLogger(__name__)
DOWNLOAD_REQUESTS_TTL_INDEX_NAME = "created_at_ttl_30d"


async def _ensure_download_requests_ttl_index(database: Any) -> None:
    """Ensure 30-day TTL index for download requests by created_at."""
    collection = database[DownloadRequest.Settings.name]
    target_key = [("created_at", ASCENDING)]

    existing_created_at_indexes: list[dict[str, Any]] = []
    try:
        indexes_cursor = await collection.list_indexes()
        async for index in indexes_cursor:
            key_items = list(index.get("key", {}).items())
            if key_items == target_key:
                existing_created_at_indexes.append(index)
    except OperationFailure as exc:
        # NamespaceNotFound is expected before first document/collection creation.
        if exc.code != 26:
            raise

    has_target_index = any(
        index.get("name") == DOWNLOAD_REQUESTS_TTL_INDEX_NAME
        and index.get("expireAfterSeconds") == DOWNLOAD_REQUESTS_TTL_SECONDS
        for index in existing_created_at_indexes
    )
    if has_target_index and len(existing_created_at_indexes) == 1:
        return

    for index in existing_created_at_indexes:
        index_name = index.get("name")
        if index_name:
            await collection.drop_index(index_name)

    await collection.create_index(
        target_key,
        name=DOWNLOAD_REQUESTS_TTL_INDEX_NAME,
        expireAfterSeconds=DOWNLOAD_REQUESTS_TTL_SECONDS,
    )
    logger.info(
        "Ensured TTL index for %s: %s=%s seconds",
        DownloadRequest.Settings.name,
        DOWNLOAD_REQUESTS_TTL_INDEX_NAME,
        DOWNLOAD_REQUESTS_TTL_SECONDS,
    )


async def init_db(db_url: str, db_name: str) -> None:
    """Initialize database connection and Beanie ODM.

    Args:
        db_url: MongoDB connection URL
        db_name: Database name
    """
    client = AsyncMongoClient(db_url)
    database = client[db_name]
    await _ensure_download_requests_ttl_index(database)
    await init_beanie(
        database=database,
        document_models=[User, Advert, Track, DownloadRequest, Payment],
    )


__all__ = ["init_db", "User", "Advert", "Track", "DownloadRequest", "Payment"]
