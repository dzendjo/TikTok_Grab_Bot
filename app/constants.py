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

# What a request asks YT-API for. "video" returns the photos when the post is
# a photo post (the worker decides), "audio" is the post's sound.
QUALITY_VIDEO = "video"
QUALITY_AUDIO = "audio"
# Media type (and cache key) of a photo post.
QUALITY_PHOTOS = "photos"
DOWNLOAD_QUALITIES = (QUALITY_VIDEO, QUALITY_AUDIO)

# Button under a delivered video / album that downloads the post's sound.
AUDIO_BUTTON_ICON = "🎵"

# Telegram Stars subscriptions only support a 30-day period.
STARS_SUBSCRIPTION_PERIOD_SECONDS = 30 * 24 * 60 * 60
