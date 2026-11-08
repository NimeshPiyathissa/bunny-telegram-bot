import logging
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message, CallbackQuery

from app.database.repository import JobRepository
from app.keyboards.main import back_keyboard
from app.utils.formatters import format_size, make_progress_bar

logger = logging.getLogger(__name__)


def register(app: Client):
    @app.on_message(filters.command("status") & filters.private)
    async def status_command(client: Client, message: Message):
        user_id = message.from_user.id
        active_jobs = await JobRepository.get_active_jobs(user_id)

        if not active_jobs:
            await message.reply(
                "📊 <b>STATUS</b>\n\n"
                "<i>No active downloads.</i>",
                reply_markup=back_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        text = "📊 <b>ACTIVE DOWNLOADS</b>\n\n━━━━━━━━━━━━━━━━━━\n\n"

        for job in active_jobs:
            status_icon = {
                "QUEUED": "⏳",
                "VALIDATING": "🔍",
                "EXTRACTING": "🔍",
                "DOWNLOADING": "⬇️",
                "MERGING": "🎞️",
                "UPLOADING": "📤",
            }.get(job.status, "⏳")

            bar = make_progress_bar(job.download_progress / 100)
            text += (
                f"{status_icon} <b>{job.video_title or 'Video'}</b>\n"
                f"   Quality: {job.selected_quality or 'N/A'}\n"
                f"   {bar} {job.download_progress:.0f}%\n"
                f"   Status: {job.status}\n\n"
            )

        await message.reply(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
