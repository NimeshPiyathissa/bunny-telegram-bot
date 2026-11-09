import asyncio
import logging
import signal
import sys
from pathlib import Path

from pyrogram import Client
from pyrogram.enums import ParseMode

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent))

from config import config
from app.utils.logger import setup_logging
from app.utils.crypto_patch import setup_crypto_patch
from app.utils.fast_upload import patch_pyrogram_uploader
from app.database.models import init_db
from app.services.queue_manager import QueueManager
from app.services.cleanup_service import CleanupService

logger = logging.getLogger(__name__)


def create_app() -> Client:
    """Create and configure the Pyrogram client with crypto and transmission acceleration."""
    # Ensure an active event loop exists in Python 3.12 environments before Client initialization
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    setup_crypto_patch()
    patch_pyrogram_uploader()
    app = Client(
        name="bunny_bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        workdir=str(Path(__file__).parent / "data"),
        parse_mode=ParseMode.HTML,
        max_concurrent_transmissions=config.MAX_CONCURRENT_TRANSMISSIONS,
    )
    return app


async def main():
    """Main entry point."""
    # Validate config
    config.validate()

    # Setup logging
    setup_logging(config.LOG_LEVEL, config.LOG_DIRECTORY)
    logger.info("Starting Bunny Stream Downloader Bot...")

    # Initialize database
    await init_db(config.DATABASE_URL)
    logger.info("Database initialized.")

    # Create bot
    app = create_app()

    # Initialize services
    queue_manager = QueueManager(config)
    cleanup_service = CleanupService(config)
    cleanup_service.set_queue_manager(queue_manager)

    # Reconcile stale jobs from any previous session
    reconciliation = await queue_manager.reconcile_stale_jobs()
    stale_count = int(reconciliation)
    if stale_count > 0:
        logger.info(f"Reconciled {stale_count} stale/orphaned job(s) on startup.")

    # Store services on app for handler access
    app.queue_manager = queue_manager  # type: ignore
    app.cleanup_service = cleanup_service  # type: ignore
    app.config = config  # type: ignore

    # Import handlers (they register themselves via decorators)
    from app.handlers import register_handlers
    register_handlers(app)

    logger.info("All handlers registered.")

    # Start services
    asyncio.create_task(queue_manager.start())
    asyncio.create_task(cleanup_service.start())

    logger.info("Bot is starting...")

    try:
        await app.start()
        logger.info("Bot started successfully!")
        # Keep running
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Received shutdown signal.")
    finally:
        logger.info("Shutting down...")
        await queue_manager.stop()
        await cleanup_service.stop()
        await app.stop()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
