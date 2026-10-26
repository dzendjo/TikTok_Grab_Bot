"""Mongo cache of YT-API meta for the quality picker.

Videos older than FRESH_VIDEO_AGE keep their meta forever: their formats and
sizes don't change. Younger videos expire after FRESH_VIDEO_TTL because YouTube
is still processing them (1080p shows up later). Any failed download deletes
the entry, so the next link fetches fresh meta.
"""
from __future__ import annotations

import datetime
from typing import Any

from app.database import VideoMeta
from app.utils import get_logger
from app.utils.youtube import extract_youtube_video_id

logger = get_logger(__name__)

FRESH_VIDEO_AGE = datetime.timedelta(days=2)
FRESH_VIDEO_TTL = datetime.timedelta(hours=4)
UPLOAD_DATE_FORMATS = ("%d.%m.%Y", "%Y%m%d")  # YT-API sends DD.MM.YYYY


def _parse_upload_date(value: str) -> datetime.datetime | None:
    for fmt in UPLOAD_DATE_FORMATS:
        try:
            return datetime.datetime.strptime(value.strip(), fmt).replace(tzinfo=datetime.UTC)
        except ValueError:
            continue
    return None


def _expires_at(upload_date: str, now: datetime.datetime) -> datetime.datetime | None:
    """None (keep forever) for old videos; unknown dates count as fresh."""
    uploaded = _parse_upload_date(upload_date) if upload_date else None
    if uploaded is not None and now - uploaded >= FRESH_VIDEO_AGE:
        return None
    return now + FRESH_VIDEO_TTL


async def get_cached_meta(video_id: str) -> dict[str, Any] | None:
    """Meta in the YT-API response shape, or None on a miss."""
    try:
        doc = await VideoMeta.get(video_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Meta cache read failed for {video_id}: {exc}")
        return None
    if doc is None:
        return None
    expires_at = doc.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.UTC)
        # Mongo's TTL monitor runs about once a minute.
        if expires_at <= datetime.datetime.now(datetime.UTC):
            return None
    return {
        "result": "ok",
        "title": doc.title,
        "duration": doc.duration,
        "upload_date": doc.upload_date,
        "download_urls": doc.formats,
    }


async def save_meta(video_id: str, meta: dict[str, Any] | None) -> None:
    """Store a successful meta response; errors (live, tab links, timeouts) are not cached."""
    if not meta or meta.get("result") != "ok" or not meta.get("download_urls"):
        return
    now = datetime.datetime.now(datetime.UTC)
    upload_date = str(meta.get("upload_date") or "")
    formats = [
        {
            "format_id": str(item.get("format_id") or ""),
            "file_size": int(item.get("file_size") or 0),
            "file_size_approx": int(item.get("file_size_approx") or 0),
        }
        for item in meta["download_urls"]
        if isinstance(item, dict) and item.get("format_id")
    ]
    try:
        await VideoMeta(
            id=video_id,
            title=str(meta.get("title") or ""),
            duration=int(meta.get("duration") or 0),
            upload_date=upload_date,
            formats=formats,
            fetched_at=now,
            expires_at=_expires_at(upload_date, now),
        ).save()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Meta cache write failed for {video_id}: {exc}")


async def invalidate_meta_for_url(url: str) -> None:
    """Drop cached meta after a failed download so the next link re-fetches it."""
    video_id = extract_youtube_video_id(url)
    if not video_id:
        return
    try:
        result = await VideoMeta.get_pymongo_collection().delete_one({"_id": video_id})
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Meta cache delete failed for {video_id}: {exc}")
        return
    if result.deleted_count:
        logger.info(f"Dropped cached meta for {video_id} after a failed download")
