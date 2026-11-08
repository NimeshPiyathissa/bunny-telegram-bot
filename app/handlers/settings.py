import logging
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery

from app.keyboards.main import back_keyboard

logger = logging.getLogger(__name__)


def register(app: Client):
    @app.on_callback_query(filters.regex("^settings$"))
    async def settings_callback(client: Client, callback: CallbackQuery):
        config = client.config  # type: ignore

        text = (
            "⚙️ <b>SETTINGS</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📥 Max concurrent downloads: <b>{config.MAX_CONCURRENT_DOWNLOADS}</b>\n"
            f"👤 Max downloads per user: <b>{config.MAX_DOWNLOADS_PER_USER}</b>\n"
            f"📦 Max file size: <b>{config.MAX_FILE_SIZE_MB} MB</b>\n"
            f"⏱️ Download timeout: <b>{config.DOWNLOAD_TIMEOUT_SECONDS}s</b>\n"
            f"🧹 Auto cleanup: <b>{'Enabled' if config.AUTO_CLEANUP else 'Disabled'}</b>\n\n"
            "<i>Settings can be changed in the .env file.</i>"
        )

        await callback.message.edit_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
