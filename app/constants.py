"""Application constants."""

DRAW_BATCH_SIZE = 100
DRAW_DELAY_SECONDS = 0.1

# Broadcast settings (BotAds)
BROADCAST_BATCH_SIZE = 100
BROADCAST_DELAY_BETWEEN_BATCHES = 0.5
BROADCAST_MAX_MESSAGES_PER_SECOND = 20
BROADCAST_SEND_DELAY_SECONDS = 1 / BROADCAST_MAX_MESSAGES_PER_SECOND
BROADCAST_RETRY_AFTER_BUFFER_SECONDS = 0.2
BROADCAST_MAX_RETRY_AFTER_ATTEMPTS = 3
DB_BATCH_SIZE = 10000

# Download qualities offered to users. Values are sent as-is to YT-API
# manager_download, which maps 360p/480p/720p/1080p to video_low/mid/high/extra_high.
QUALITY_AUDIO = "audio"
VIDEO_QUALITIES = ("360p", "480p", "720p", "1080p")
DOWNLOAD_QUALITIES = (*VIDEO_QUALITIES, QUALITY_AUDIO)
# Offered only when YouTube lists the format; others also when meta is unavailable.
OPTIONAL_QUALITIES = frozenset({"1080p"})

# Local Telegram Bot API server upload limit.
TELEGRAM_MAX_UPLOAD_BYTES = 2000 * 1024 * 1024

# Quality picker button icons.
CACHED_ICON = "⚡"
DOWNLOAD_ICON = "📥"

# Premium. Locked picker buttons get this icon instead of ⚡/📥.
LOCKED_ICON = "🔒"
PREMIUM_QUALITIES = frozenset({"1080p"})
# Picker action for the transcript button; also used in invoice payloads.
ACTION_TRANSCRIPT = "transcript"
TRANSCRIPT_ICON = "📝"
# Telegram Stars subscriptions only support a 30-day period.
STARS_SUBSCRIPTION_PERIOD_SECONDS = 30 * 24 * 60 * 60
