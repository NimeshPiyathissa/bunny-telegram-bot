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
from app.database.models import init_db
from app.services.queue_manager import QueueManager
from app.services.cleanup_service import CleanupService

logger = logging.getLogger(__name__)


def create_app() -> Client:
    """Create and configure the Pyrogram client."""
    app = Client(
        name="bunny_bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        workdir=str(Path(__file__).parent / "data"),
        parse_mode=ParseMode.HTML,
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
