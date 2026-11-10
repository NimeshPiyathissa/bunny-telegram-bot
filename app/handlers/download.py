import asyncio
import html
import logging
import time
from typing import Dict, Optional, List, Any

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message, CallbackQuery

from app.keyboards.main import cancel_keyboard, back_keyboard, workflow_conflict_keyboard
from app.keyboards.quality import quality_keyboard
from app.keyboards.caption import caption_input_keyboard, caption_confirm_keyboard
from app.handlers.state import (
    FlowState, PendingJob, get_pending_job, set_pending_job, clear_pending_job
)
from app.services.bunny_extractor import BunnyExtractor, ExtractionResult
from app.database.repository import UserRepository, JobRepository
from app.utils.security import (
    validate_url, is_bunny_url, rate_limiter,
    extract_urls, sanitize_url_for_logging, sanitize_url_for_display
)
from app.utils.formatters import format_duration, format_size

logger = logging.getLogger(__name__)

# Store pending extractions per user (kept for backward compatibility)
_pending_extractions: Dict[int, ExtractionResult] = {}
_waiting_for_url: Dict[int, bool] = {}


async def _start_extraction_workflow(
    client: Client,
    url: str,
    user_id: int,
    message: Optional[Message] = None,
    target_msg: Optional[Message] = None,
    is_direct: bool = True,
    chat_id: Optional[int] = None,
) -> None:
    """Core extraction workflow shared by Mode A (Classic), Mode B (Smart Direct-Link), and Replace."""
    logger.info(f"Starting extraction for user {user_id}: {sanitize_url_for_logging(url)}")

    if is_direct:
        init_text = (
            "🔗 <b>Video Link Detected!</b>\n"
            "🔍 <i>Analyzing video...</i>\n"
            "🎬 <i>Fetching available qualities...</i>"
        )
    else:
        init_text = (
            "🔍 <b>ANALYZING VIDEO</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🔐 Checking access...\n"
            "⏳ Please wait..."
        )

    if target_msg is not None:
        status_msg = target_msg
        await status_msg.edit_text(init_text, parse_mode=ParseMode.HTML)
    elif message is not None:
        status_msg = await message.reply(init_text, parse_mode=ParseMode.HTML)
    else:
        return

    effective_chat_id = chat_id or (message.chat.id if message and message.chat else None)

    # Extract video info safely
    extractor = BunnyExtractor()
    result = await extractor.extract(url)

    if not result.success:
        error_text = result.error or "Unknown error occurred."
        await status_msg.edit_text(
            f"❌ <b>EXTRACTION FAILED</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"{html.escape(error_text)}\n\n"
            f"<i>Please verify that your link is active, authorized, and not expired.</i>",
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
        chat_id=effective_chat_id,
    ))

    # Build quality selection message
    duration_str = format_duration(result.duration) if result.duration else "Unknown"
    safe_title = html.escape(result.title)

    quality_text = (
        f"🎬 <b>VIDEO DETECTED</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📺 <b>Title:</b> {safe_title}\n"
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


async def _handle_workflow_conflict(message: Message, pending: PendingJob, new_url: str) -> None:
    """Prompt user when a new link is sent during an unfinished workflow."""
    pending.candidate_url = new_url

    status_desc = {
        FlowState.SELECTING_QUALITY: "Selecting quality",
        FlowState.WAITING_FOR_CAPTION: "Waiting for custom caption",
        FlowState.CONFIRMING_DOWNLOAD: "Ready to download (confirmation)",
    }.get(pending.state, "In progress")

    existing_title = html.escape(pending.extraction.title) if pending.extraction else "Current Video"
    preview_link = sanitize_url_for_display(new_url)

    conflict_text = (
        "⚠️ <b>Unfinished Download in Progress</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"You already have an unfinished download setup:\n"
        f"🎬 <b>Current:</b> {existing_title}\n"
        f"📌 <b>Step:</b> {status_desc}\n\n"
        f"🔗 <b>New Link Detected:</b>\n"
        f"<code>{html.escape(preview_link)}</code>\n\n"
        "Would you like to continue configuring your current video or replace it with the new link?"
    )

    await message.reply(
        conflict_text,
        reply_markup=workflow_conflict_keyboard(),
        parse_mode=ParseMode.HTML,
    )


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
        max_per_user = getattr(client.config, "MAX_DOWNLOADS_PER_USER", 1)  # type: ignore
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

        # Extract all URLs safely from plain text, surrounding text, and Telegram entities
        detected_urls = extract_urls(message.text, getattr(message, "entities", None))
        bunny_urls = [u for u in detected_urls if is_bunny_url(u)]
        has_bunny_url = len(bunny_urls) > 0

        # 1. Check if user is submitting a custom video caption
        if pending and pending.state == FlowState.WAITING_FOR_CAPTION:
            # If user sent a new supported Bunny video link instead of caption, offer conflict choice
            if has_bunny_url:
                await _handle_workflow_conflict(message, pending, bunny_urls[0])
                return

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
            pending.caption_entities = getattr(message, "entities", None)
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

        # 2. Check if user has an unfinished workflow (SELECTING_QUALITY or CONFIRMING_DOWNLOAD)
        if pending and pending.extraction and pending.state in (FlowState.SELECTING_QUALITY, FlowState.CONFIRMING_DOWNLOAD):
            if has_bunny_url:
                await _handle_workflow_conflict(message, pending, bunny_urls[0])
                return
            elif detected_urls:
                # User sent an unsupported external link while in an unfinished workflow
                await message.reply(
                    "⚠️ <b>Unsupported URL</b>\n\n"
                    "This URL is not from a recognized Bunny Stream domain.\n\n"
                    "<b>Supported domains:</b>\n"
                    "• iframe.mediadelivery.net\n"
                    "• video.bunnycdn.com\n"
                    "• *.b-cdn.net\n\n"
                    "<i>Please select an option from the buttons above or send /cancel to cancel.</i>",
                    reply_markup=back_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
                return
            else:
                # Plain chatter while configuring a video
                if pending.state == FlowState.SELECTING_QUALITY:
                    await message.reply(
                        "ℹ️ Please select a quality option from the buttons above, or send /cancel to cancel.",
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await message.reply(
                        "ℹ️ Please confirm your download using the buttons above, or send /cancel to cancel.",
                        parse_mode=ParseMode.HTML,
                    )
                return

        # 3. Check if user is in classic URL-waiting state or if incoming message contains a URL
        is_classic_waiting = _waiting_for_url.get(user_id, False) or (pending and pending.state == FlowState.WAITING_FOR_URL)
        if not is_classic_waiting and not detected_urls:
            # Idle user sent regular text with no URL - return silently
            return

        # 4. User registration (No /start command required for Mode B)
        from_user = message.from_user
        if from_user:
            await UserRepository.get_or_create(
                telegram_id=getattr(from_user, "id", user_id),
                username=getattr(from_user, "username", None),
                first_name=getattr(from_user, "first_name", None),
                last_name=getattr(from_user, "last_name", None),
            )

        # 5. Check if user is banned
        if await UserRepository.is_banned(user_id):
            await message.reply("🚫 You have been banned from using this bot.")
            return

        # 6. Rate limiting check
        if not rate_limiter.is_allowed(user_id):
            await message.reply(
                "⏳ <b>Too Many Requests</b>\n\n"
                "Please wait a moment before sending another link.",
                parse_mode=ParseMode.HTML,
            )
            return

        # 7. Check per-user active download limits (prevent accidental duplicate download jobs)
        active_jobs = await JobRepository.get_active_jobs(user_id)
        max_per_user = getattr(client.config, "MAX_DOWNLOADS_PER_USER", 1)  # type: ignore
        queue_mgr = getattr(client, "queue_manager", None)
        is_busy = (queue_mgr and queue_mgr.is_user_busy(user_id)) or len(active_jobs) >= max_per_user
        if is_busy:
            await message.reply(
                f"⚠️ <b>Active Download In Progress</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"You already have {len(active_jobs) or 1} active download in progress.\n"
                f"Maximum allowed: <b>{max_per_user}</b>.\n\n"
                f"Please wait for your current video to finish, or send /cancel to stop it.",
                reply_markup=back_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        # 8. Process URL
        if has_bunny_url:
            target_url = bunny_urls[0]
            is_valid, error_msg = validate_url(target_url)
            if not is_valid:
                await message.reply(
                    f"❌ <b>Invalid URL</b>\n\n{error_msg}\n\n"
                    f"<i>Please send a valid Bunny Stream video URL.</i>",
                    reply_markup=back_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
                return

            is_direct = not is_classic_waiting
            _waiting_for_url[user_id] = False

            await _start_extraction_workflow(
                client=client,
                url=target_url,
                user_id=user_id,
                message=message,
                is_direct=is_direct,
                chat_id=message.chat.id if message.chat else None,
            )
            return

        elif detected_urls:
            # URLs were found, but none are supported Bunny Stream domains
            # Do NOT automatically fetch arbitrary or untrusted domains!
            await message.reply(
                "⚠️ <b>Unsupported URL</b>\n\n"
                "This URL is not from a recognized Bunny Stream domain.\n\n"
                "<b>Supported domains:</b>\n"
                "• iframe.mediadelivery.net\n"
                "• video.bunnycdn.com\n"
                "• *.b-cdn.net\n\n"
                "<i>Arbitrary, third-party, or untrusted video domains are not accepted.</i>",
                reply_markup=back_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        elif is_classic_waiting:
            # User was prompted in Mode A to send a URL, but sent non-URL text
            _waiting_for_url[user_id] = False
            raw_url = message.text.strip() if message.text else ""
            is_valid, error_msg = validate_url(raw_url)
            await message.reply(
                f"❌ <b>Invalid URL</b>\n\n{error_msg}",
                reply_markup=back_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

    @app.on_callback_query(filters.regex("^workflow_continue$"))
    async def workflow_continue(client: Client, callback: CallbackQuery):
        """Restore active workflow screen when user chooses to keep current setup."""
        user_id = callback.from_user.id
        pending = get_pending_job(user_id)
        if not pending or not pending.extraction:
            await callback.answer("⚠️ Session expired. Please send a video link.", show_alert=True)
            return

        pending.candidate_url = None

        if pending.state == FlowState.SELECTING_QUALITY:
            duration_str = format_duration(pending.extraction.duration) if pending.extraction.duration else "Unknown"
            safe_title = html.escape(pending.extraction.title)
            quality_text = (
                f"🎬 <b>VIDEO DETECTED</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"📺 <b>Title:</b> {safe_title}\n"
                f"⏱️ <b>Duration:</b> {duration_str}\n"
            )
            if pending.extraction.needs_merge:
                quality_text += "🔀 <b>Note:</b> Video and audio will be merged\n"
            quality_text += (
                f"\n<b>Available qualities:</b>\n"
                f"Select the quality you want to download."
            )
            await callback.message.edit_text(
                quality_text,
                reply_markup=quality_keyboard(pending.extraction.video_formats),
                parse_mode=ParseMode.HTML,
            )
        elif pending.state == FlowState.WAITING_FOR_CAPTION:
            safe_title = html.escape(pending.extraction.title)
            quality_label = pending.quality_label or "Default"
            caption_prompt_text = (
                "✍️ <b>CUSTOM VIDEO CAPTION</b>\n\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"🎬 <b>Video:</b> {safe_title}\n"
                f"📺 <b>Selected Quality:</b> {quality_label}\n\n"
                "📝 Send the caption you want to attach\n"
                "to your video.\n\n"
                "✨ <b>Supported:</b>\n"
                "• Plain text\n"
                "• Emojis\n"
                "• Multiple lines\n"
                "• Telegram-supported formatting\n\n"
                "👇 Type your caption below."
            )
            await callback.message.edit_text(
                caption_prompt_text,
                reply_markup=caption_input_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        elif pending.state == FlowState.CONFIRMING_DOWNLOAD:
            safe_title = html.escape(pending.extraction.title)
            quality_label = pending.quality_label or "Default"
            caption_preview = html.escape(pending.caption or "")
            if len(caption_preview) > 600:
                caption_preview = caption_preview[:600] + "..."
            confirm_text = (
                "✅ <b>VIDEO READY</b>\n\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"🎬 <b>Video:</b> {safe_title}\n"
                f"📺 <b>Quality:</b> {quality_label}\n"
                f"📝 <b>Caption:</b>\n{caption_preview}\n\n"
                "Ready to download?"
            )
            await callback.message.edit_text(
                confirm_text,
                reply_markup=caption_confirm_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        else:
            await callback.message.edit_text(
                "ℹ️ Continuing current workflow.",
                reply_markup=back_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        await callback.answer("Continuing current download.")

    @app.on_callback_query(filters.regex("^workflow_replace$"))
    async def workflow_replace(client: Client, callback: CallbackQuery):
        """Replace active workflow with candidate link."""
        user_id = callback.from_user.id
        pending = get_pending_job(user_id)
        new_url = pending.candidate_url if pending else None
        if not new_url:
            await callback.answer("⚠️ No replacement link found. Please send a new link.", show_alert=True)
            return

        clear_pending_job(user_id)
        _pending_extractions.pop(user_id, None)

        await _start_extraction_workflow(
            client=client,
            url=new_url,
            user_id=user_id,
            target_msg=callback.message,
            is_direct=True,
            chat_id=callback.message.chat.id if callback.message else None,
        )
        await callback.answer("Switched to new video link.")

    @app.on_message(filters.command("download") & filters.private)
    async def download_command(client: Client, message: Message):
        user_id = message.from_user.id

        await UserRepository.get_or_create(
            telegram_id=user_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )

        if await UserRepository.is_banned(user_id):
            await message.reply("🚫 You are banned.")
            return

        # Check if URL was provided with command
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            # URL provided directly with command
            _waiting_for_url[user_id] = True
            message.text = parts[1]
            await handle_url_message(client, message)
        else:
            _waiting_for_url[user_id] = True
            set_pending_job(user_id, PendingJob(
                user_id=user_id,
                state=FlowState.WAITING_FOR_URL,
                chat_id=message.chat.id if message.chat else None,
            ))
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
        queue_manager = getattr(client, "queue_manager", None)
        if queue_manager:
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

        queue_manager = getattr(client, "queue_manager", None)
        cancelled = 0
        if queue_manager:
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
        queue_manager = getattr(client, "queue_manager", None)

        total_users = await UserRepository.get_total_count()
        total_downloads = await JobRepository.get_total_count()
        active_jobs = queue_manager.active_count if queue_manager else 0
        queued_jobs = queue_manager.queued_count if queue_manager else 0

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
