"""Build quality picker options from YT-API meta and the media cache."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.constants import (
    DOWNLOAD_QUALITIES,
    OPTIONAL_QUALITIES,
    QUALITY_AUDIO,
    TELEGRAM_MAX_UPLOAD_BYTES,
    VIDEO_QUALITIES,
)

# H.264 itags per quality in the order the YT-API worker downloads them
# (runtime/common/yt_ops.py _YT_H264_ITAGS). 60 fps videos — gaming, most
# Shorts — only have 298/299 above 480p.
VIDEO_ITAGS: dict[str, tuple[str, ...]] = {
    "360p": ("134",),
    "480p": ("135",),
    "720p": ("298", "136"),
    "1080p": ("299", "137"),
}
HIGH_FPS_ITAGS = frozenset({"298", "299"})
AUDIO_ITAGS = ("140",)


@dataclass(slots=True, frozen=True)
class QualityOption:
    quality: str
    label: str
    size_bytes: int | None
    cached: bool


def _pick_variant(variants: list[dict[str, Any]], itags: tuple[str, ...]) -> tuple[str, int] | None:
    """First itag YouTube lists for this video and its size (0 if unknown).

    Multi-audio videos expose 140-N per dub; the plain id is preferred.
    """
    for itag in itags:
        matches = [item for item in variants if str(item.get("format_id", "")).split("-", 1)[0] == itag]
        if not matches:
            continue
        matches.sort(key=lambda item: str(item.get("format_id", "")) != itag)
        best = matches[0]
        return itag, int(best.get("file_size") or best.get("file_size_approx") or 0)
    return None


def _label(quality: str, pick: tuple[str, int] | None) -> str:
    if pick and pick[0] in HIGH_FPS_ITAGS:
        return f"{quality}60"
    return quality


def build_quality_options(meta: dict[str, Any] | None, cached_sizes: dict[str, int]) -> list[QualityOption]:
    """Decide which qualities to offer, their labels and sizes.

    With usable meta a video quality is offered only if YouTube lists one of
    its H.264 itags and video+audio fit into the Telegram upload limit. Without
    meta (or when meta lists no video itags at all) the classic set is offered
    without sizes. Cached qualities are always offered.
    """
    variants = list((meta or {}).get("download_urls") or []) if (meta or {}).get("result") == "ok" else []
    picks = {quality: _pick_variant(variants, VIDEO_ITAGS[quality]) for quality in VIDEO_QUALITIES}
    has_video_meta = any(picks.values())
    audio_pick = _pick_variant(variants, AUDIO_ITAGS)
    audio_size = audio_pick[1] if audio_pick else 0

    options: list[QualityOption] = []
    for quality in DOWNLOAD_QUALITIES:
        pick = picks.get(quality)
        label = _label(quality, pick)
        if quality in cached_sizes:
            options.append(QualityOption(quality, label, cached_sizes[quality] or None, cached=True))
            continue

        if quality == QUALITY_AUDIO:
            options.append(QualityOption(quality, label, audio_size or None, cached=False))
            continue

        if not has_video_meta:
            if quality not in OPTIONAL_QUALITIES:
                options.append(QualityOption(quality, label, None, cached=False))
            continue

        if pick is None:
            continue
        size = pick[1] + audio_size if pick[1] else 0
        if size > TELEGRAM_MAX_UPLOAD_BYTES:
            continue
        options.append(QualityOption(quality, label, size or None, cached=False))

    return options


def format_size(size_bytes: int) -> str:
    """Human-readable size for buttons: 950 KB, 26.5 MB, 1.2 GB."""
    mb = size_bytes / (1024 * 1024)
    if mb < 1:
        return f"{max(1, round(size_bytes / 1024))} KB"
    if mb < 1024:
        return f"{mb:.1f} MB" if mb < 100 else f"{mb:.0f} MB"
    return f"{mb / 1024:.2f} GB"


def format_duration(seconds: int) -> str:
    """3:33 / 1:02:03."""
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
