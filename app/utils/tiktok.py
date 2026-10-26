"""TikTok link parsing and short-link resolution."""
from __future__ import annotations

import re

import aiohttp

from app.utils.logger import get_logger

logger = get_logger(__name__)

# /@user/video/<id>, /@user/photo/<id>, /share/video/<id>, m.tiktok.com/v/<id>
POST_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?tiktokv?\.com/"
    r"(?:@(?P<user>[\w.-]*)/(?:video|photo)/|share/video/|v/)(?P<id>\d{8,25})",
    re.IGNORECASE,
)
# vm.tiktok.com/<code>, vt.tiktok.com/<code>, tiktok.com/t/<code>
SHORT_URL_RE = re.compile(
    r"https?://(?:(?:vm|vt)\.tiktok\.com/|(?:www\.)?tiktok\.com/t/)[\w-]+/?",
    re.IGNORECASE,
)
POST_ID_RE = re.compile(r"^\d{8,25}$")
# TikTok answers short links with a plain redirect for this crawler UA.
_SHORT_URL_USER_AGENT = "facebookexternalhit/1.1"
_SHORT_URL_TIMEOUT = aiohttp.ClientTimeout(total=6)


def find_tiktok_url(text: str) -> str | None:
    """First TikTok post or short link in the text."""
    for pattern in (POST_URL_RE, SHORT_URL_RE):
        match = pattern.search(text or "")
        if match:
            return match.group(0)
    return None


def extract_post_id(url: str) -> str | None:
    match = POST_URL_RE.match(url or "")
    return match.group("id") if match else None


def is_short_url(url: str) -> bool:
    return bool(SHORT_URL_RE.match(url or ""))


def canonical_url(post_id: str) -> str:
    """Post URL without the author; TikTok and the YT-API worker accept it."""
    return f"https://www.tiktok.com/@/video/{post_id}"


async def resolve_post_id(url: str) -> str | None:
    """Post id of a link; short links are followed. None when it isn't a post."""
    post_id = extract_post_id(url)
    if post_id or not is_short_url(url):
        return post_id
    try:
        async with aiohttp.ClientSession(timeout=_SHORT_URL_TIMEOUT) as session:
            async with session.head(
                url,
                allow_redirects=True,
                headers={"User-Agent": _SHORT_URL_USER_AGENT},
            ) as response:
                final_url = str(response.url)
    except Exception as exc:  # noqa: BLE001
        # The worker follows the link itself; only the cache lookup is lost.
        logger.warning(f"Short link resolution failed for {url}: {exc!r}")
        return None
    return extract_post_id(final_url)
