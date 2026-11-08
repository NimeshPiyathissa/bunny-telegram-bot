import asyncio
import html
import logging
import time
from typing import Dict

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message, CallbackQuery

from app.keyboards.main import cancel_keyboard, back_keyboard
from app.keyboards.quality import quality_keyboard
from app.keyboards.caption import caption_input_keyboard, caption_confirm_keyboard
from app.handlers.state import (
    FlowState, PendingJob, get_pending_job, set_pending_job, clear_pending_job
)
from app.services.bunny_extractor import BunnyExtractor, ExtractionResult
from app.database.repository import UserRepository, JobRepository
from app.utils.security import validate_url, is_bunny_url, rate_limiter
from app.utils.formatters import format_duration, format_size

logger = logging.getLogger(__name__)

# Store pending extractions per user (kept for backward compatibility)
_pending_extractions: Dict[int, ExtractionResult] = {}
_waiting_for_url: Dict[int, bool] = {}


def register(app: Client):
    @app.on_callback_query(filters.regex("^download_video$"))
    async def download_video_callback(client: Client, callback: CallbackQuery):
        user_id = callback.from_user.id

        if await UserRepository.is_banned(user_id):
            await callback.answer("🚫 You are banned.", show_alert=True)
            return

        # Rate limiting
        if not rate_limiter.is_allowed(user_id):
            await callback.answer(
                "⏳ Too many requests. Please wait a moment.",
                show_alert=True
            )
            return

        # Check active jobs
        active_jobs = await JobRepository.get_active_jobs(user_id)
        max_per_user = client.config.MAX_DOWNLOADS_PER_USER  # type: ignore
        if len(active_jobs) >= max_per_user:
            await callback.answer(
                f"⚠️ You already have {len(active_jobs)} active download(s). "
                f"Maximum allowed: {max_per_user}",
                show_alert=True
            )
            return

        _waiting_for_url[user_id] = True
        set_pending_job(user_id, PendingJob(
            user_id=user_id,
            state=FlowState.WAITING_FOR_URL,
            chat_id=callback.message.chat.id if callback.message else None,
        ))

        await callback.message.edit_text(
            "🔗 <b>SEND VIDEO URL</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Send me a Bunny Stream embed URL.\n\n"
            "<b>Supported formats:</b>\n"
            "• <code>https://iframe.mediadelivery.net/embed/...</code>\n"
            "• Direct Bunny CDN MP4 URLs\n"
            "• Bunny CDN HLS (.m3u8) URLs\n\n"
            "<i>Only authorized, downloadable videos are supported.</i>",
            reply_markup=cancel_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()

    @app.on_message(filters.text & filters.private & ~filters.command(["start", "help", "admin", "stats", "cancel", "download", "status", "users", "jobs", "recover_jobs", "restart", "ban", "unban", "broadcast"]))
    async def handle_url_message(client: Client, message: Message):
        user_id = message.from_user.id
        pending = get_pending_job(user_id)

        # 1. Check if user is submitting a custom video caption
        if pending and pending.state == FlowState.WAITING_FOR_CAPTION:
            caption_text = message.text

            # Validate caption length (Telegram standard limit <= 1024 characters)
            if len(caption_text) > 1024:
                await message.reply(
                    f"⚠️ <b>Caption Too Long</b>\n\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                    f"Your caption is <b>{len(caption_text)}</b> characters.\n"
                    f"Telegram video captions cannot exceed <b>1024</b> characters.\n\n"
                    f"👇 Please send a shorter caption:",
                    reply_markup=caption_input_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
                return

            # Save valid caption and formatting entities
            pending.caption = caption_text
            pending.caption_entities = message.entities
            pending.state = FlowState.CONFIRMING_DOWNLOAD

            # Safe HTML escape for preview in confirmation message
            safe_preview = html.escape(caption_text)
            if len(safe_preview) > 600:
                safe_preview = safe_preview[:600] + "..."

            video_title = html.escape(pending.extraction.title) if pending.extraction else "Video"
            quality_label = pending.quality_label or "Default"

            confirm_text = (
                "✅ <b>VIDEO READY</b>\n\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"🎬 <b>Video:</b> {video_title}\n"
                f"📺 <b>Quality:</b> {quality_label}\n"
                f"📝 <b>Caption:</b>\n{safe_preview}\n\n"
                "Ready to download?"
            )
            await message.reply(
                confirm_text,
                reply_markup=caption_confirm_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        # 2. Check if user is in URL-waiting state
        if not _waiting_for_url.get(user_id, False) and not (pending and pending.state == FlowState.WAITING_FOR_URL):
            return

        _waiting_for_url[user_id] = False
        url = message.text.strip()

        # Validate URL
        is_valid, error_msg = validate_url(url)
        if not is_valid:
            await message.reply(
                f"❌ <b>Invalid URL</b>\n\n{error_msg}",
                reply_markup=back_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        # Check if it's a Bunny URL
        if not is_bunny_url(url):
            await message.reply(
                "⚠️ <b>Unsupported URL</b>\n\n"
                "This URL is not from a recognized Bunny Stream domain.\n\n"
                "<b>Supported domains:</b>\n"
                "• iframe.mediadelivery.net\n"
                "• video.bunnycdn.com\n"
                "• *.b-cdn.net",
                reply_markup=back_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        # Show extraction progress
        status_msg = await message.reply(
            "🔍 <b>ANALYZING VIDEO</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🔐 Checking access...\n"
            "⏳ Please wait...",
            parse_mode=ParseMode.HTML,
        )

        # Extract video info
        extractor = BunnyExtractor()
        result = await extractor.extract(url)

        if not result.success:
            error_text = result.error or "Unknown error occurred."
            await status_msg.edit_text(
                f"❌ <b>EXTRACTION FAILED</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"{error_text}",
                reply_markup=back_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        # Store extraction result and state
        _pending_extractions[user_id] = result
        set_pending_job(user_id, PendingJob(
            user_id=user_id,
            state=FlowState.SELECTING_QUALITY,
            extraction=result,
            chat_id=message.chat.id,
        ))

        # Build quality selection message
        duration_str = format_duration(result.duration) if result.duration else "Unknown"

        quality_text = (
            f"🎬 <b>VIDEO DETECTED</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📺 <b>Title:</b> {result.title}\n"
            f"⏱️ <b>Duration:</b> {duration_str}\n"
        )

        if result.needs_merge:
            quality_text += "🔀 <b>Note:</b> Video and audio will be merged\n"

        quality_text += (
            f"\n<b>Available qualities:</b>\n"
            f"Select the quality you want to download."
        )

        await status_msg.edit_text(
            quality_text,
            reply_markup=quality_keyboard(result.video_formats),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("download") & filters.private)
    async def download_command(client: Client, message: Message):
        user_id = message.from_user.id

        if await UserRepository.is_banned(user_id):
            await message.reply("🚫 You are banned.")
            return

        # Check if URL was provided with command
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            # URL provided directly
            _waiting_for_url[user_id] = True
            message.text = parts[1]  # Set message text to URL
            await handle_url_message(client, message)
        else:
            _waiting_for_url[user_id] = True
            await message.reply(
                "🔗 <b>SEND VIDEO URL</b>\n\n"
                "Send me a Bunny Stream embed URL.",
                reply_markup=cancel_keyboard(),
                parse_mode=ParseMode.HTML,
            )

    @app.on_callback_query(filters.regex("^cancel_download$"))
    async def cancel_download(client: Client, callback: CallbackQuery):
        user_id = callback.from_user.id
        _waiting_for_url.pop(user_id, None)
        _pending_extractions.pop(user_id, None)
        clear_pending_job(user_id)

        # Cancel active jobs
        queue_manager = client.queue_manager  # type: ignore
        for job_id, job in list(queue_manager._active_jobs.items()):
            if job.telegram_id == user_id:
                await queue_manager.cancel_job(job_id)

        await callback.message.edit_text(
            "❌ <b>Download cancelled.</b>",
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer("Cancelled")

    @app.on_message(filters.command("cancel") & filters.private)
    async def cancel_command(client: Client, message: Message):
        user_id = message.from_user.id
        _waiting_for_url.pop(user_id, None)
        _pending_extractions.pop(user_id, None)
        clear_pending_job(user_id)

        queue_manager = client.queue_manager  # type: ignore
        cancelled = 0
        for job_id, job in list(queue_manager._active_jobs.items()):
            if job.telegram_id == user_id:
                await queue_manager.cancel_job(job_id)
                cancelled += 1

        if cancelled:
            await message.reply(f"❌ Cancelled {cancelled} active download(s).")
        else:
            await message.reply("ℹ️ No active downloads to cancel.")

    @app.on_callback_query(filters.regex("^my_downloads$"))
    async def my_downloads(client: Client, callback: CallbackQuery):
        user_id = callback.from_user.id
        history = await JobRepository.get_user_history(user_id, limit=10)

        if not history:
            text = (
                "📂 <b>MY DOWNLOADS</b>\n\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "<i>No downloads yet.</i>"
            )
        else:
            text = "📂 <b>MY DOWNLOADS</b>\n\n━━━━━━━━━━━━━━━━━━\n\n"
            for job in history:
                status_icon = {
                    "COMPLETED": "✅",
                    "FAILED": "❌",
                    "CANCELLED": "🚫",
                    "DOWNLOADING": "⬇️",
                    "UPLOADING": "📤",
                    "MERGING": "🎞️",
                }.get(job.status, "⏳")

                title = job.video_title or "Unknown"
                text += f"{status_icon} {title}\n"
                text += f"   Quality: {job.selected_quality or 'N/A'} | Status: {job.status}\n\n"

        await callback.message.edit_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()

    @app.on_callback_query(filters.regex("^bot_status$"))
    async def bot_status(client: Client, callback: CallbackQuery):
        from app.utils.validators import get_system_stats

        stats = get_system_stats()
        queue_manager = client.queue_manager  # type: ignore

        total_users = await UserRepository.get_total_count()
        total_downloads = await JobRepository.get_total_count()
        active_jobs = queue_manager.active_count
        queued_jobs = queue_manager.queued_count

        text = (
            "📊 <b>BOT STATUS</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Users: <b>{total_users}</b>\n"
            f"📥 Total Downloads: <b>{total_downloads}</b>\n"
            f"⚡ Active Jobs: <b>{active_jobs}</b>\n"
            f"⏳ Queued: <b>{queued_jobs}</b>\n\n"
            f"💻 CPU: <b>{stats.get('cpu_percent', 'N/A')}%</b>\n"
            f"🧠 Memory: <b>{stats.get('memory_used_percent', 'N/A')}%</b>\n"
            f"💾 Disk: <b>{stats.get('disk_used_percent', 'N/A')}%</b>\n\n"
            "🟢 Status: <b>Online</b>"
        )

        await callback.message.edit_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
