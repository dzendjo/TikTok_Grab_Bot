"""YouTube URL parsing helpers."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

YOUTUBE_VIDEO_PATH_PREFIXES = {"shorts", "live", "embed", "v"}


def normalize_host(host: str) -> str:
    """Normalize hostname by lowercasing and removing an optional port."""
    return host.split(":", 1)[0].strip().lower()


def is_youtube_host(host: str) -> bool:
    """Return True for youtube.com / youtu.be hosts (including subdomains)."""
    normalized_host = normalize_host(host)
    return (
        normalized_host == "youtube.com"
        or normalized_host.endswith(".youtube.com")
        or normalized_host == "youtu.be"
        or normalized_host.endswith(".youtu.be")
    )


def extract_youtube_video_id(url: str) -> str | None:
    """Extract YouTube video ID from common URL formats."""
    parsed = urlparse(url)
    host = normalize_host(parsed.netloc)
    path_parts = [part for part in parsed.path.split("/") if part]

    if host == "youtu.be" or host.endswith(".youtu.be"):
        if path_parts:
            return path_parts[0]
        return None

    if host == "youtube.com" or host.endswith(".youtube.com"):
        video_id = parse_qs(parsed.query).get("v", [None])[0]
        if video_id:
            return video_id

        if len(path_parts) >= 2 and path_parts[0].lower() in YOUTUBE_VIDEO_PATH_PREFIXES:
            return path_parts[1]

    return None
