"""Broadcast service for advertisements via BotAdsLib."""
from __future__ import annotations

import asyncio
import datetime
import time
from collections.abc import Iterable, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from botadslib import (
    BotAdsAPIError,
    BotAdsClient,
    BotAdsConfigurationError,
    BotAdsError,
    BotAdsNetworkError,
    BroadcastBatchSubmitError,
    CacheChannelAccessError,
)
from botadslib.models import AdItem

from app.config import settings
from app.constants import (
    BROADCAST_BATCH_SIZE,
    BROADCAST_DELAY_BETWEEN_BATCHES,
    BROADCAST_MAX_RETRY_AFTER_ATTEMPTS,
    BROADCAST_RETRY_AFTER_BUFFER_SECONDS,
    BROADCAST_SEND_DELAY_SECONDS,
    DB_BATCH_SIZE,
)
from app.database import Advert, User
from app.utils import get_logger

logger = get_logger(__name__)


class BroadcastService:
    """Service for handling BotAds actions and broadcasts."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self._ads_client: BotAdsClient | None = None
        self._ads_disabled_logged = False
        self._broadcast_send_lock = asyncio.Lock()
        self._next_broadcast_send_at = 0.0

    async def close(self) -> None:
        """Close BotAds client resources."""
        if self._ads_client:
            await self._ads_client.close()

    def _get_ads_client(self) -> BotAdsClient | None:
        """Build BotAds client lazily from settings."""
        if self._ads_client is not None:
            return self._ads_client

        missing: list[str] = []
        if not settings.ad_api_base_url:
            missing.append("AD_API_BASE_URL")
        if not settings.ad_bot_hash:
            missing.append("AD_BOT_HASH")
        if settings.ad_cache_channel_id is None:
            missing.append("AD_CACHE_CHANNEL_ID")

        if missing:
            if not self._ads_disabled_logged:
                logger.warning("Ads are disabled, missing settings: " + ", ".join(missing))
                self._ads_disabled_logged = True
            return None

        api_base_url = settings.ad_api_base_url
        bot_hash = settings.ad_bot_hash
        cache_channel_id = settings.ad_cache_channel_id
        assert api_base_url is not None
        assert bot_hash is not None
        assert cache_channel_id is not None

        self._ads_client = BotAdsClient(
            bot=self.bot,
            api_base_url=api_base_url,
            bot_hash=bot_hash,
            cache_channel_id=cache_channel_id,
            media_cache_path=settings.ad_media_cache_path or None,
        )
        return self._ads_client

    def _require_ads_client(self) -> BotAdsClient:
        """Return configured client or raise detailed configuration error."""
        ads_client = self._get_ads_client()
        if not ads_client:
            raise BotAdsConfigurationError(
                "Ads are not configured. Set AD_API_BASE_URL, AD_BOT_HASH and AD_CACHE_CHANNEL_ID."
            )
        return ads_client

    async def get_campaigns(self, *, include_inactive: bool = False) -> list[dict]:
        """Fetch campaigns from BotAds API."""
        ads_client = self._require_ads_client()
        return await ads_client.get_campaigns(include_inactive=include_inactive)

    async def get_db_broadcast_recipient_count(self, campaign_key: str) -> int:
        """Return real target recipients count from DB for a broadcast campaign key."""
        all_recipients = await self._fetch_active_recipients_with_language()
        campaign = await Advert.find_one(Advert.name == campaign_key)
        if not campaign:
            return len(all_recipients)

        done_users_set = set(campaign.done_users)
        return sum(1 for user_id, _ in all_recipients if user_id not in done_users_set)

    async def get_broadcast_ad(
        self,
        *,
        recipient_telegram_ids: Sequence[int | str],
        placement: str,
        language: str | None = None,
    ) -> tuple[AdItem | None, list[str]]:
        """Fetch broadcast ad and normalized recipients."""
        ads_client = self._require_ads_client()
        return await ads_client.get_broadcast_ad(
            recipient_telegram_ids=recipient_telegram_ids,
            placement=placement,
            language=language,
        )

    async def send_ad_to_chat(self, *, chat_id: int | str, ad_item: AdItem) -> None:
        """Send a concrete ad item to chat."""
        ads_client = self._require_ads_client()
        await ads_client.send_ad_to_chat(chat_id=chat_id, ad_item=ad_item)

    async def send_manual_broadcast(
        self,
        *,
        ad_item: AdItem,
        recipient_telegram_ids: Sequence[int | str],
        delay_seconds: float = 0.0,
    ) -> tuple[list[str], list[str], dict[str, str]]:
        """Send one prepared broadcast ad to recipients and submit final status."""
        ads_client = self._require_ads_client()

        recipients = [str(value) for value in recipient_telegram_ids]
        delivered: list[str] = []
        failed: list[str] = []
        errors: dict[str, str] = {}

        for recipient in recipients:
            is_delivered, error = await self._send_broadcast_recipient(
                ads_client=ads_client,
                recipient=recipient,
                ad_item=ad_item,
            )
            if is_delivered:
                delivered.append(recipient)
            else:
                failed.append(recipient)
                errors[recipient] = error or "Unknown delivery error"

            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

        try:
            await self.finalize_broadcast_batch(
                ad_item=ad_item,
                delivered_recipient_telegram_ids=delivered,
                failed_recipient_telegram_ids=failed,
            )
        except Exception as exc:  # noqa: BLE001
            batch_id = (
                ad_item.broadcast_batch.batch_id
                if ad_item.broadcast_batch and ad_item.broadcast_batch.batch_id
                else "unknown"
            )
            raise BroadcastBatchSubmitError(
                batch_id=batch_id,
                delivered_recipient_ids=list(delivered),
                failed_recipient_ids=list(failed),
                reason=exc,
            ) from exc

        return delivered, failed, errors

    async def _send_broadcast_recipient(
        self,
        *,
        ads_client: BotAdsClient,
        recipient: str,
        ad_item: AdItem,
    ) -> tuple[bool, str | None]:
        """Send broadcast message to one recipient with rate-limit aware retries."""
        attempts_left = BROADCAST_MAX_RETRY_AFTER_ATTEMPTS
        while True:
            try:
                await self._throttle_broadcast_send()
                await ads_client.send_ad_to_chat(
                    chat_id=BotAdsClient.to_chat_id(recipient),
                    ad_item=ad_item,
                )
                return True, None
            except TelegramRetryAfter as exc:
                if attempts_left <= 0:
                    return False, str(exc)
                attempts_left -= 1
                retry_seconds = float(exc.retry_after) + BROADCAST_RETRY_AFTER_BUFFER_SECONDS
                logger.warning(
                    "Broadcast hit Telegram RetryAfter for recipient %s. Retrying in %.2fs.",
                    recipient,
                    retry_seconds,
                )
                await asyncio.sleep(retry_seconds)
            except TelegramAPIError as exc:
                return False, str(exc)
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)

    async def _throttle_broadcast_send(self) -> None:
        """Guarantee a safe per-bot broadcast send interval."""
        async with self._broadcast_send_lock:
            now = time.monotonic()
            wait_seconds = self._next_broadcast_send_at - now
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
                now = time.monotonic()
            self._next_broadcast_send_at = now + BROADCAST_SEND_DELAY_SECONDS

    async def finalize_broadcast_batch(
        self,
        *,
        ad_item: AdItem,
        delivered_recipient_telegram_ids: Sequence[int | str] = (),
        failed_recipient_telegram_ids: Sequence[int | str] = (),
    ) -> None:
        """Finalize reserved BotAds broadcast batch."""
        if not ad_item.broadcast_batch or not ad_item.broadcast_batch.batch_id:
            raise BotAdsError("broadcastBatch.batchId is missing in broadcast ad response")

        await self.finalize_broadcast_batch_by_id(
            batch_id=ad_item.broadcast_batch.batch_id,
            delivered_recipient_telegram_ids=delivered_recipient_telegram_ids,
            failed_recipient_telegram_ids=failed_recipient_telegram_ids,
        )

    async def finalize_broadcast_batch_by_id(
        self,
        *,
        batch_id: str,
        delivered_recipient_telegram_ids: Sequence[int | str] = (),
        failed_recipient_telegram_ids: Sequence[int | str] = (),
    ) -> None:
        """Finalize reserved BotAds broadcast batch by explicit batch ID."""
        ads_client = self._require_ads_client()
        delivered = [str(value) for value in delivered_recipient_telegram_ids]
        failed = [str(value) for value in failed_recipient_telegram_ids]

        await ads_client.submit_broadcast_result(
            batch_id=batch_id,
            delivered_recipient_telegram_ids=delivered,
            failed_recipient_telegram_ids=failed,
        )

    async def send_action_advert(self, user_id: int, placement: str = "after_result") -> bool:
        """Send action advert to a specific user."""
        user = await User.get(user_id)
        if not user:
            return False

        # Backward-compatible guard for projects where User has premium support.
        if bool(getattr(user, "is_premium", False)):
            return False

        ads_client = self._get_ads_client()
        if not ads_client:
            return False

        request_language = (
            self._normalize_language_code(getattr(user, "language", None) or user.language_code)
            or settings.default_language
        )

        try:
            result = await ads_client.send_action_advert(
                recipient_telegram_id=user_id,
                placement=placement,
                language=request_language,
            )
        except (BotAdsConfigurationError, CacheChannelAccessError) as exc:
            logger.error(f"BotAds configuration error: {exc}")
            return False
        except (BotAdsAPIError, BotAdsNetworkError, BotAdsError) as exc:
            logger.error(f"BotAds action delivery failed for {user_id}: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Unexpected action advert error for {user_id}: {exc}")
            return False

        if result.error:
            logger.warning(f"Action advert failed for {user_id}: {result.error}")
            return False

        return result.has_ad

    @staticmethod
    def _normalize_language_code(language: str | None) -> str | None:
        """Normalize locale to 2-letter language code."""
        if not language:
            return None
        normalized = language.strip().lower().replace("_", "-")
        base = normalized.split("-", 1)[0]
        if len(base) == 2 and base.isalpha():
            return base
        return None

    async def start_broadcast(
        self,
        admin_user_id: int,
        campaign_key: str,
        placement: str = "manual_broadcast",
    ) -> None:
        """Run full bot-wide broadcast campaign using BotAds."""
        ads_client = self._get_ads_client()
        if not ads_client:
            await self.bot.send_message(
                admin_user_id,
                "❌ Реклама не налаштована. Додайте AD_API_BASE_URL, AD_BOT_HASH і AD_CACHE_CHANNEL_ID.",
            )
            return

        campaign = await Advert.find_one(Advert.name == campaign_key)
        if not campaign:
            campaign = Advert(name=campaign_key, admin_user_id=admin_user_id, type="broadcast")
            await campaign.save()

        done_users_set = set(campaign.done_users)
        all_recipients = await self._fetch_active_recipients_with_language()
        target_recipients = [
            (user_id, language)
            for user_id, language in all_recipients
            if user_id not in done_users_set
        ]
        if not target_recipients:
            await self.bot.send_message(admin_user_id, "Немає активних користувачів для розсилки.")
            return

        recipient_segments = BotAdsClient.segment_broadcast_recipients_by_language(
            recipients=target_recipients,
            fallback_language=settings.default_language,
            max_chunk_size=BROADCAST_BATCH_SIZE,
        )

        total_success = 0
        total_failed = 0
        total_skipped = 0

        for language, recipient_chunk in recipient_segments:
            batch = self._extract_int_ids(recipient_chunk)
            if not batch:
                continue

            try:
                success, failed, skipped = await self._send_broadcast_batch(
                    ads_client=ads_client,
                    user_ids=batch,
                    placement=placement,
                    language=language,
                )
            except (BotAdsConfigurationError, CacheChannelAccessError) as exc:
                logger.error(f"BotAds configuration error: {exc}")
                await self.bot.send_message(admin_user_id, f"❌ Помилка конфігурації реклами: {exc}")
                return
            except (BotAdsAPIError, BotAdsNetworkError, BotAdsError) as exc:
                logger.error(f"BotAds broadcast error: {exc}")
                await self.bot.send_message(admin_user_id, f"❌ Помилка BotAds API: {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Unexpected broadcast error: {exc}")
                await self.bot.send_message(admin_user_id, "❌ Неочікувана помилка під час рекламної розсилки.")
                return

            total_success += len(success)
            total_failed += len(failed)
            total_skipped += len(skipped)

            processed_users = success + failed
            if processed_users:
                campaign.done_users.extend(processed_users)
                await campaign.save()

            await self._mark_users_inactive(failed)
            await asyncio.sleep(BROADCAST_DELAY_BETWEEN_BATCHES)

        message = (
            "✅ Розсилка завершена!\n\n"
            f"📊 Успішно: {total_success}\n"
            f"⚪️ Без доступної реклами: {total_skipped}\n"
            f"❌ Помилок: {total_failed}"
        )
        await self.bot.send_message(admin_user_id, message)

    async def _send_broadcast_batch(
        self,
        *,
        ads_client: BotAdsClient,
        user_ids: list[int],
        placement: str,
        language: str | None = None,
    ) -> tuple[list[int], list[int], list[int]]:
        """Send one BotAds batch and return success/failed/skipped IDs."""
        try:
            ad_item, recipients = await ads_client.get_broadcast_ad(
                recipient_telegram_ids=user_ids,
                placement=placement,
                language=language,
            )
            if not ad_item:
                return [], [], list(user_ids)

            delivered_ids, failed_ids, _ = await self.send_manual_broadcast(
                ad_item=ad_item,
                recipient_telegram_ids=recipients,
            )
        except BroadcastBatchSubmitError as exc:
            logger.error(f"Broadcast batch submit failed for batch {exc.batch_id}: {exc.reason}")
            success = self._extract_int_ids(exc.delivered_recipient_ids)
            failed = self._extract_int_ids(exc.failed_recipient_ids)
            success_set = set(success)
            failed_set = set(failed)
            skipped = [uid for uid in user_ids if uid not in success_set and uid not in failed_set]
            return success, failed, skipped

        success = self._extract_int_ids(delivered_ids)
        failed = self._extract_int_ids(failed_ids)

        success_set = set(success)
        failed_set = set(failed)
        skipped = [uid for uid in user_ids if uid not in success_set and uid not in failed_set]
        return success, failed, skipped

    @staticmethod
    def _extract_int_ids(values: Iterable[str | int]) -> list[int]:
        """Convert recipient IDs to integers and skip malformed IDs."""
        parsed: list[int] = []
        for value in values:
            try:
                parsed.append(int(str(value)))
            except (TypeError, ValueError):
                continue
        return parsed

    async def _fetch_active_recipients_with_language(self) -> list[tuple[int, str | None]]:
        """Fetch active users and their language for segment-aware delivery."""
        recipients: list[tuple[int, str | None]] = []
        user_collection = User.get_pymongo_collection()

        # Premium users don't get ad broadcasts.
        now = datetime.datetime.now(datetime.UTC)
        cursor = user_collection.find(
            {"is_active": True, "premium_until": {"$not": {"$gt": now}}},
            {"_id": 1, "language": 1, "language_code": 1},
        ).batch_size(DB_BATCH_SIZE)

        async for doc in cursor:
            user_id = doc.get("_id")
            if not isinstance(user_id, int):
                continue
            language = self._normalize_language_code(doc.get("language") or doc.get("language_code"))
            recipients.append((user_id, language))

        return recipients

    async def _mark_users_inactive(self, user_ids: list[int]) -> None:
        """Mark failed recipients as inactive."""
        if not user_ids:
            return
        user_collection = User.get_pymongo_collection()
        await user_collection.update_many(
            {"_id": {"$in": user_ids}},
            {"$set": {"is_active": False}},
        )
