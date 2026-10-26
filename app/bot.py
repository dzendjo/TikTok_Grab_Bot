"""Bot initialization and startup."""
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from nats.aio.client import Client as NatsClient
from redis.asyncio import Redis

from app.config import settings
from app.database import init_db
from app.handlers import setup_routers
from app.middlewares import (
    BroadcastMiddleware,
    DatabaseMiddleware,
    ErrorHandlerMiddleware,
    LocalizationMiddleware,
    LoggingMiddleware,
    NatsMiddleware,
    error_handler,
)
from app.services import BroadcastService
from app.services.nats_meta import (
    close_nats_client,
    create_nats_client,
    subscribe_download_status,
    unsubscribe_nats_subscription,
)
from app.utils import get_logger, setup_logger

logger = get_logger(__name__)


async def setup_bot() -> tuple[Bot, Dispatcher, NatsClient, BroadcastService]:
    """Setup bot and dispatcher with all dependencies."""
    setup_logger(settings.log_level)
    await init_db(settings.db_url, settings.db_name)
    logger.info("Database initialized")
    logger.info(f"Allowed source channels configured: {len(settings.allowed_source_channel_ids)}")
    nats_client = await create_nats_client()
    logger.info("NATS client initialized")

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    if settings.redis_enabled:
        redis = Redis.from_url(settings.redis_url)
        storage = RedisStorage(redis)
        logger.info("Using Redis for FSM storage")
    else:
        storage = MemoryStorage()
        logger.info("Using Memory for FSM storage")

    dp = Dispatcher(storage=storage)
    broadcast_service = BroadcastService(bot)

    dp.update.middleware(ErrorHandlerMiddleware())
    dp.update.middleware(LoggingMiddleware())
    dp.update.middleware(DatabaseMiddleware())
    dp.update.middleware(NatsMiddleware(nats_client))
    dp.update.middleware(LocalizationMiddleware())
    dp.update.middleware(BroadcastMiddleware(broadcast_service))

    dp.errors.register(error_handler)
    dp.include_router(setup_routers())

    logger.info("Bot setup completed")
    return bot, dp, nats_client, broadcast_service


async def on_startup(bot: Bot) -> None:
    """Actions to perform on startup."""
    bot_info = await bot.get_me()
    logger.info(f"Bot started: @{bot_info.username} (ID: {bot_info.id})")

    if settings.webhook_enabled and settings.webhook_url:
        webhook_url = f"{settings.webhook_url.rstrip('/')}{settings.webhook_path}"
        await bot.set_webhook(webhook_url)
        webhook_info = await bot.get_webhook_info()
        logger.info(f"Webhook set: {webhook_info.url}")


async def on_shutdown(bot: Bot) -> None:
    """Actions to perform on shutdown."""
    if settings.webhook_enabled:
        await bot.delete_webhook()
    logger.info("Bot stopped")


async def main() -> None:
    """Main entry point for the bot."""
    bot, dp, nats_client, broadcast_service = await setup_bot()
    download_status_subscription = None

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    try:
        download_status_subscription = await subscribe_download_status(nats_client, bot)
        if settings.webhook_enabled:
            from aiohttp import web
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

            app = web.Application()
            SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=settings.webhook_path)
            setup_application(app, dp, bot=bot)

            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, settings.webhook_host, settings.webhook_port)
            await site.start()

            logger.info(f"Webhook server started on {settings.webhook_host}:{settings.webhook_port}")

            import asyncio

            while True:
                await asyncio.sleep(3600)
        else:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Starting polling...")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Error in main: {exc}")
        raise
    finally:
        await unsubscribe_nats_subscription(download_status_subscription)
        await close_nats_client(nats_client)
        await broadcast_service.close()
        await bot.session.close()
