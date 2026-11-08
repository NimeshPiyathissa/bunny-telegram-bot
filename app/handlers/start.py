import logging
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message, CallbackQuery

from app.keyboards.main import main_keyboard, back_keyboard
from app.database.repository import UserRepository

logger = logging.getLogger(__name__)

WELCOME_TEXT = """
🎬 <b>BUNNY STREAM DOWNLOADER</b>

<i>Your Premium Video Download Assistant</i>

━━━━━━━━━━━━━━━━━━

📥 Download authorized videos
🎞️ High-quality MP4 processing
🎧 Video + Audio merging
⚡ Fast asynchronous processing
☁️ Direct Telegram delivery

━━━━━━━━━━━━━━━━━━

📊 System: <b>Online</b>
🟢 Downloader: <b>Ready</b>

<i>Select an option below.</i>
"""

HELP_TEXT = """
❓ <b>HELP & INFORMATION</b>

━━━━━━━━━━━━━━━━━━

<b>How to download:</b>
1️⃣ Tap <b>📥 Download Video</b>
2️⃣ Send a Bunny Stream embed URL
3️⃣ Select the desired quality
4️⃣ Wait for download & upload

<b>Supported URLs:</b>
• Bunny Stream embed URLs
• Direct MP4 URLs from Bunny CDN
• HLS (.m3u8) URLs from Bunny CDN

<b>URL Format:</b>
<code>https://iframe.mediadelivery.net/embed/LIBRARY_ID/VIDEO_GUID</code>

<b>Important:</b>
• Only authorized, downloadable videos are supported
• DRM-protected content cannot be downloaded
• Maximum file size depends on Telegram limits

<b>Commands:</b>
/start - Main menu
/download - Start a download
/status - Check bot status
/help - Show this help
/cancel - Cancel active download

━━━━━━━━━━━━━━━━━━
"""


def register(app: Client):
    @app.on_message(filters.command("start") & filters.private)
    async def start_command(client: Client, message: Message):
        # Register user
        await UserRepository.get_or_create(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )

        # Check if banned
        if await UserRepository.is_banned(message.from_user.id):
            await message.reply("🚫 You have been banned from using this bot.")
            return

        await message.reply(
            WELCOME_TEXT,
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("help") & filters.private)
    async def help_command(client: Client, message: Message):
        await message.reply(
            HELP_TEXT,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @app.on_callback_query(filters.regex("^back_to_menu$"))
    async def back_to_menu(client: Client, callback: CallbackQuery):
        await callback.message.edit_text(
            WELCOME_TEXT,
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()

    @app.on_callback_query(filters.regex("^help$"))
    async def help_callback(client: Client, callback: CallbackQuery):
        await callback.message.edit_text(
            HELP_TEXT,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
