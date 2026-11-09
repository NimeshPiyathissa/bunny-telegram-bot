import asyncio
import html
import logging
import os
import re
import time
from typing import Dict, Optional, Any

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

try:
    from pyrogram import StopTransmission
except ImportError:
    class StopTransmission(Exception):
        pass

from app.keyboards.main import back_keyboard, cancel_keyboard
from app.keyboards.quality import quality_keyboard
from app.keyboards.caption import caption_input_keyboard, caption_confirm_keyboard
from app.handlers.state import (
    FlowState, PendingJob, get_pending_job, set_pending_job, clear_pending_job
)
from app.services.bunny_extractor import ExtractionResult, VideoFormat
from app.services.download_engine import DownloadEngine, DownloadProgress, DownloadResult
from app.services.ffmpeg_service import FFmpegService
from app.services.upload_service import UploadService
from app.database.repository import JobRepository, UserRepository, ErrorRepository
from app.utils.formatters import (
    format_size, format_speed, format_eta, format_duration, make_progress_bar
)
from app.utils.validators import check_disk_space, check_file_size, validate_output_file

logger = logging.getLogger(__name__)


def sanitize_error(err: str) -> str:
    """Sanitize error messages to prevent leaking secrets, tokens, or private URLs."""
    if not err:
        return "Unknown error."
    # Redact query parameters (tokens, signatures, keys)
    cleaned = re.sub(r'([?&][a-zA-Z0-9_-]+)=[^&\s]+', r'\1=[redacted]', err)
    cleaned = re.sub(r'token=[^&\s]+', 'token=[redacted]', cleaned)
    return cleaned[:300]


def register(app: Client):
    @app.on_callback_query(filters.regex(r"^quality:"))
    async def quality_selected(client: Client, callback: CallbackQuery):
        user_id = callback.from_user.id
        config = client.config  # type: ignore
        queue_manager = client.queue_manager  # type: ignore

        # Parse quality selection
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer("Invalid selection.", show_alert=True)
            return

        format_id = parts[1]
        height = int(parts[2]) if parts[2].isdigit() else 0

        # Get pending job or fallback to extraction
        from app.handlers.download import _pending_extractions
        pending = get_pending_job(user_id)
        extraction = pending.extraction if pending else _pending_extractions.get(user_id)

        if not extraction:
            await callback.answer(
                "⚠️ Session expired. Please start a new download.",
                show_alert=True
            )
            return

        # Check per-user limit before proceeding
        if queue_manager.is_user_busy(user_id):
            await callback.answer(
                "⚠️ You already have an active download in progress. Please wait for it to complete.",
                show_alert=True
            )
            return

        # Find selected format
        selected_format = None
        if format_id == "best":
            if extraction.video_formats:
                selected_format = max(extraction.video_formats, key=lambda f: f.height)
        else:
            for fmt in extraction.video_formats:
                if fmt.format_id == format_id:
                    selected_format = fmt
                    break

        if not selected_format:
            await callback.answer("Format not found.", show_alert=True)
            return

        # Check disk space
        ok, msg = check_disk_space(config.TEMP_DIRECTORY, required_mb=500)
        if not ok:
            await callback.answer(f"⚠️ {msg}", show_alert=True)
            return

        quality_label = selected_format.quality or f"{height}p"

        # Update or create pending job in WAITING_FOR_CAPTION state
        if not pending:
            pending = PendingJob(
                user_id=user_id,
                extraction=extraction,
                chat_id=callback.message.chat.id if callback.message else None,
            )
        pending.selected_format = selected_format
        pending.quality_label = quality_label
        pending.state = FlowState.WAITING_FOR_CAPTION
        set_pending_job(user_id, pending)

        # Display Caption Input UI - DO NOT start download immediately!
        safe_title = html.escape(extraction.title)
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
        await callback.answer()

    @app.on_callback_query(filters.regex(r"^caption_skip$"))
    async def caption_skip(client: Client, callback: CallbackQuery):
        user_id = callback.from_user.id
        from app.handlers.download import _pending_extractions
        pending = get_pending_job(user_id)
        if not pending or not pending.extraction or not pending.selected_format:
            await callback.answer("⚠️ Session expired. Please start a new download.", show_alert=True)
            return

        # Default caption uses video title
        pending.caption = f"🎬 <b>{pending.extraction.title}</b>"
        pending.caption_entities = None
        pending.state = FlowState.CONFIRMING_DOWNLOAD

        safe_title = html.escape(pending.extraction.title)
        confirm_text = (
            "✅ <b>VIDEO READY</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"🎬 <b>Video:</b> {safe_title}\n"
            f"📺 <b>Quality:</b> {pending.quality_label}\n"
            f"📝 <b>Caption:</b>\n<i>(Default)</i> 🎬 {safe_title}\n\n"
            "Ready to download?"
        )
        await callback.message.edit_text(
            confirm_text,
            reply_markup=caption_confirm_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()

    @app.on_callback_query(filters.regex(r"^caption_back_quality$"))
    async def caption_back_quality(client: Client, callback: CallbackQuery):
        user_id = callback.from_user.id
        from app.handlers.download import _pending_extractions
        pending = get_pending_job(user_id)
        extraction = pending.extraction if pending else _pending_extractions.get(user_id)
        if not extraction:
            await callback.answer("⚠️ Session expired. Please start a new download.", show_alert=True)
            return

        if pending:
            pending.state = FlowState.SELECTING_QUALITY

        duration_str = format_duration(extraction.duration) if extraction.duration else "Unknown"
        safe_title = html.escape(extraction.title)
        quality_text = (
            f"🎬 <b>VIDEO DETECTED</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📺 <b>Title:</b> {safe_title}\n"
            f"⏱️ <b>Duration:</b> {duration_str}\n"
        )
        if extraction.needs_merge:
            quality_text += "🔀 <b>Note:</b> Video and audio will be merged\n"

        quality_text += (
            f"\n<b>Available qualities:</b>\n"
            f"Select the quality you want to download."
        )

        await callback.message.edit_text(
            quality_text,
            reply_markup=quality_keyboard(extraction.video_formats),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()

    @app.on_callback_query(filters.regex(r"^caption_edit$"))
    async def caption_edit(client: Client, callback: CallbackQuery):
        user_id = callback.from_user.id
        pending = get_pending_job(user_id)
        if not pending or not pending.extraction or not pending.selected_format:
            await callback.answer("⚠️ Session expired. Please start a new download.", show_alert=True)
            return

        pending.state = FlowState.WAITING_FOR_CAPTION
        safe_title = html.escape(pending.extraction.title)
        caption_prompt_text = (
            "✍️ <b>CUSTOM VIDEO CAPTION</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"🎬 <b>Video:</b> {safe_title}\n"
            f"📺 <b>Selected Quality:</b> {pending.quality_label}\n\n"
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
        await callback.answer()

    @app.on_callback_query(filters.regex(r"^start_download_confirmed$"))
    async def start_download_confirmed(client: Client, callback: CallbackQuery):
        user_id = callback.from_user.id
        config = client.config  # type: ignore
        queue_manager = client.queue_manager  # type: ignore

        from app.handlers.download import _pending_extractions
        pending = get_pending_job(user_id)
        if not pending or not pending.extraction or not pending.selected_format:
            await callback.answer("⚠️ Session expired. Please start a new download.", show_alert=True)
            return

        # Check per-user limit before proceeding
        if queue_manager.is_user_busy(user_id):
            await callback.answer(
                "⚠️ You already have an active download in progress. Please wait for it to complete.",
                show_alert=True
            )
            return

        # Check disk space
        ok, msg = check_disk_space(config.TEMP_DIRECTORY, required_mb=500)
        if not ok:
            await callback.answer(f"⚠️ {msg}", show_alert=True)
            return

        extraction = pending.extraction
        selected_format = pending.selected_format
        quality_label = pending.quality_label
        confirmed_caption = pending.caption
        confirmed_entities = pending.caption_entities
        target_message = callback.message

        # Worker callback executing the download pipeline inside a worker task
        async def pipeline_worker(job):
            await _process_download(
                client=client,
                target=target_message,
                extraction=extraction,
                selected_format=selected_format,
                config=config,
                queue_manager=queue_manager,
                job_id=job.job_id,
                user_id=user_id,
                caption=confirmed_caption,
                caption_entities=confirmed_entities,
            )

        # Add to queue manager with persistent DB write and worker callback
        job_id = await queue_manager.add_job(
            telegram_id=user_id,
            url=selected_format.url,
            quality=quality_label,
            video_title=extraction.title,
            callback=pipeline_worker,
        )

        if not job_id:
            await callback.message.edit_text(
                "⚠️ <b>Queue Full</b>\n\n"
                "You have reached the maximum number of concurrent downloads.\n"
                "Please wait for your current download to complete.",
                reply_markup=back_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            await callback.answer()
            return

        # Transition state and clean up flow
        pending.state = FlowState.PROCESSING
        clear_pending_job(user_id)
        _pending_extractions.pop(user_id, None)

        # Check queue position
        pos = queue_manager.get_queue_position(job_id)
        if pos > 1 or queue_manager.active_count >= queue_manager.max_concurrent:
            try:
                await callback.message.edit_text(
                    f"⏳ <b>IN QUEUE (Position #{pos})</b>\n\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                    f"🎬 <b>{extraction.title}</b>\n"
                    f"📺 Quality: <b>{quality_label}</b>\n\n"
                    f"<i>Your download will start automatically once a worker slot is ready.</i>",
                    reply_markup=cancel_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        else:
            try:
                await callback.message.edit_text(
                    f"⬇️ <b>STARTING DOWNLOAD</b>\n\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                    f"🎬 <b>{extraction.title}</b>\n"
                    f"📺 Quality: <b>{quality_label}</b>\n\n"
                    f"⏳ Preparing download...",
                    reply_markup=cancel_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        await callback.answer()


async def _process_download(
    client, target, extraction, selected_format, config, queue_manager, job_id: str,
    user_id: Optional[int] = None, caption: Optional[str] = None, caption_entities: Optional[list] = None
):
    """Process the full download → merge → validate → upload pipeline with guaranteed slot cleanup."""
    if hasattr(target, "message"):
        user_id = target.from_user.id
        message = target.message
    elif hasattr(target, "chat"):
        message = target
        user_id = user_id or (message.from_user.id if message.from_user else 0)
    else:
        message = target
        user_id = user_id or 0
    chat_id = message.chat.id

    # Find best audio format if needed
    audio_format = None
    if extraction.needs_merge and extraction.audio_formats:
        audio_format = extraction.audio_formats[0]  # Best quality

    # Create download engine with configured timeout, isolated workspace, and concurrent HLS
    workspace_dir = None
    engine = DownloadEngine(
        download_dir=config.DOWNLOAD_DIRECTORY,
        temp_dir=config.TEMP_DIRECTORY,
        timeout=config.DOWNLOAD_TIMEOUT_SECONDS,
        job_id=job_id,
        hls_concurrency=getattr(config, "HLS_CONCURRENT_SEGMENTS", 10),
    )
    workspace_dir = str(engine.workspace)
    try:
        await JobRepository.set_workspace(job_id, workspace_dir)
    except Exception:
        pass

    # Link engine to job for cancellation support
    job = queue_manager.get_job(job_id)
    if job:
        job.engine = engine

    # Progress tracking
    last_edit_time = [0.0]
    main_loop = asyncio.get_running_loop()

    def progress_callback(progress: DownloadProgress):
        # Throttle message edits (max once per 3 seconds)
        now = time.monotonic()
        if now - last_edit_time[0] < 3.0:
            return
        last_edit_time[0] = now

        if main_loop and not main_loop.is_closed():
            coro = _update_progress(message, extraction, progress)
            try:
                asyncio.run_coroutine_threadsafe(coro, main_loop)
            except Exception:
                coro.close()

    result: Optional[DownloadResult] = None
    final_status = "FAILED"
    error_to_record: Optional[str] = None
    output_path: Optional[str] = None

    pipeline_start_time = time.monotonic()
    dl_start_time = time.monotonic()
    dl_duration = 0.0
    dl_speed_mbs = 0.0

    try:
        # 1. Update status to DOWNLOADING
        await queue_manager.update_job_status(job_id, "DOWNLOADING")

        # Check if already cancelled
        if job and job.cancel_event.is_set():
            final_status = "CANCELLED"
            return

        # 2. Download authorized video & audio streams
        dl_start_time = time.monotonic()
        result = await engine.download(
            extraction=extraction,
            selected_format=selected_format,
            audio_format=audio_format,
            progress_callback=progress_callback,
        )
        dl_end_time = time.monotonic()
        dl_duration = max(0.001, dl_end_time - dl_start_time)
        res_size = getattr(result, "file_size", 0)
        if isinstance(res_size, (int, float)) and res_size > 0:
            dl_speed_mbs = (res_size / (1024 * 1024)) / dl_duration

        if not result.success:
            final_status = "FAILED"
            error_to_record = result.error or "Unknown download error"
            sanitized_err = sanitize_error(error_to_record)
            await ErrorRepository.log_error(
                "DOWNLOAD_FAILED", error_to_record,
                job_id=job_id, telegram_id=user_id
            )
            try:
                await message.edit_text(
                    f"❌ <b>DOWNLOAD FAILED</b>\n\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                    f"{sanitized_err}",
                    reply_markup=back_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        output_path = result.output_path

        # 3. Merge if needed
        if result.needs_merge and result.video_path and result.audio_path:
            await queue_manager.update_job_status(job_id, "MERGING")
            try:
                await message.edit_text(
                    f"🎞️ <b>MERGING VIDEO & AUDIO</b>\n\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                    f"🎬 {extraction.title}\n\n"
                    f"⏳ Processing with FFmpeg...",
                    reply_markup=cancel_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

            merged_path = str(
                engine.workspace / f"merged_{job_id[:8]}.mp4"
            )
            success, merge_msg = await FFmpegService.merge_video_audio(
                result.video_path, result.audio_path, merged_path,
                timeout=config.DOWNLOAD_TIMEOUT_SECONDS
            )

            if success:
                output_path = merged_path
                result.merged_path = merged_path
            else:
                final_status = "FAILED"
                error_to_record = f"Merge failed: {merge_msg}"
                sanitized_err = sanitize_error(merge_msg)
                await ErrorRepository.log_error(
                    "MERGE_FAILED", merge_msg,
                    job_id=job_id, telegram_id=user_id
                )
                try:
                    await message.edit_text(
                        f"❌ <b>MERGE FAILED</b>\n\n"
                        f"━━━━━━━━━━━━━━━━━━\n\n"
                        f"{sanitized_err}",
                        reply_markup=back_keyboard(),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                return

        if not output_path or not os.path.exists(output_path):
            final_status = "FAILED"
            error_to_record = "No output file produced."
            await ErrorRepository.log_error(
                "NO_OUTPUT_FILE", error_to_record,
                job_id=job_id, telegram_id=user_id
            )
            try:
                await message.edit_text(
                    "❌ <b>Error:</b> Output file is missing.",
                    reply_markup=back_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        # 4. Validate output file (existence, nonzero size, MP4 container signature)
        is_valid, val_msg = validate_output_file(output_path)
        if not is_valid:
            final_status = "FAILED"
            error_to_record = f"Media validation failed: {val_msg}"
            await ErrorRepository.log_error(
                "VALIDATION_FAILED", val_msg,
                job_id=job_id, telegram_id=user_id
            )
            try:
                await message.edit_text(
                    f"❌ <b>MEDIA VALIDATION FAILED</b>\n\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                    f"{val_msg}\n\n"
                    f"<i>The downloaded media could not be validated as a proper MP4 file.</i>",
                    reply_markup=back_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        # 5. Check file size against Telegram limits
        file_size = os.path.getsize(output_path)
        ok, size_msg = check_file_size(output_path, config.max_file_size_bytes)
        if not ok:
            final_status = "FAILED"
            error_to_record = size_msg
            try:
                await message.edit_text(
                    f"⚠️ <b>FILE TOO LARGE</b>\n\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                    f"File size: <b>{format_size(file_size)}</b>\n"
                    f"Maximum limit: <b>{format_size(config.max_file_size_bytes)}</b>\n\n"
                    f"<i>Try selecting a lower resolution quality.</i>",
                    reply_markup=back_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        # 6. Upload to Telegram
        await queue_manager.update_job_status(job_id, "UPLOADING")

        upload_start_time = time.monotonic()
        upload_last_edit = [0.0]
        upload_loop = asyncio.get_running_loop()

        def upload_progress(current, total):
            if job and job.cancel_event.is_set():
                raise StopTransmission

            now = time.monotonic()
            if now - upload_last_edit[0] < 2.5 and current < total:
                return
            upload_last_edit[0] = now

            if upload_loop and not upload_loop.is_closed():
                coro = _update_upload_progress(
                    message, extraction, current, total, upload_start_time, job_id
                )
                try:
                    asyncio.run_coroutine_threadsafe(coro, upload_loop)
                except Exception:
                    coro.close()

        upload_service = UploadService(
            config.max_file_size_bytes, timeout=config.UPLOAD_TIMEOUT_SECONDS
        )

        try:
            await message.edit_text(
                f"📤 <b>UPLOADING TO TELEGRAM</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"🎬 <b>{extraction.title}</b>\n\n"
                f"⏳ Initializing upload stream...",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        # Probe video metadata for exact dimensions, aspect ratio, and duration
        video_metadata = await FFmpegService.get_video_metadata(output_path)
        actual_width = video_metadata.display_width
        actual_height = video_metadata.display_height
        actual_duration = video_metadata.duration or extraction.duration

        # Bounded upload retry loop with exponential backoff & delivery confirmation
        max_upload_retries = 3
        uploaded_msg = None
        last_upload_error = None
        ul_duration = 0.0
        ul_speed_mbs = 0.0

        for attempt in range(max_upload_retries):
            # Check cancellation between retries
            if job and job.cancel_event.is_set():
                final_status = "CANCELLED"
                return

            try:
                uploaded_msg = await upload_service.upload_video(
                    client=client,
                    chat_id=chat_id,
                    file_path=output_path,
                    title=extraction.title,
                    duration=actual_duration,
                    width=actual_width,
                    height=actual_height,
                    progress_callback=upload_progress,
                    caption=caption,
                    caption_entities=caption_entities,
                )

                # Check for confirmed delivery by Telegram
                if uploaded_msg and hasattr(uploaded_msg, "id"):
                    telegram_msg_id = uploaded_msg.id
                    telegram_fid = (
                        getattr(uploaded_msg.video, "file_id", None)
                        if hasattr(uploaded_msg, "video") and uploaded_msg.video
                        else (
                            getattr(uploaded_msg.document, "file_id", None)
                            if hasattr(uploaded_msg, "document") and uploaded_msg.document
                            else None
                        )
                    )

                    ul_end_time = time.monotonic()
                    ul_duration = max(0.001, ul_end_time - upload_start_time)
                    if isinstance(file_size, (int, float)) and file_size > 0:
                        ul_speed_mbs = (file_size / (1024 * 1024)) / ul_duration

                    # Persist completed status and Telegram message ID / file ID
                    final_status = "COMPLETED"
                    try:
                        await JobRepository.complete_job(
                            job_id,
                            telegram_message_id=telegram_msg_id,
                            telegram_file_id=telegram_fid,
                            file_size=file_size,
                        )
                    except Exception as repo_err:
                        logger.warning(f"Error persisting completion metadata: {repo_err}")

                    await queue_manager.update_job_status(job_id, "COMPLETED")
                    await UserRepository.increment_downloads(user_id)
                    break
                else:
                    raise RuntimeError("Telegram upload completed without returning a valid message object.")

            except (StopTransmission, asyncio.CancelledError):
                final_status = "CANCELLED"
                error_to_record = "Cancelled by user"
                logger.info(f"Upload for job {job_id} cancelled by user.")
                break
            except Exception as upload_err:
                last_upload_error = upload_err
                err_str = str(upload_err).lower()
                is_timeout = isinstance(upload_err, (asyncio.TimeoutError, TimeoutError)) or "timed out" in err_str

                logger.warning(
                    f"[UPLOAD:RETRY] Upload attempt {attempt + 1}/{max_upload_retries} for job {job_id} "
                    f"failed: {upload_err}"
                )

                if attempt < max_upload_retries - 1:
                    # If timeout or uncertain connection drop on later attempts, do not blindly spam duplicate sends
                    if is_timeout and attempt > 0:
                        logger.warning(f"[UPLOAD:UNCERTAIN] Upload timeout on attempt {attempt + 1}. Marking for recovery.")
                        try:
                            await JobRepository.mark_recovery(job_id, f"Uncertain upload delivery state: {upload_err}")
                        except Exception:
                            pass
                        break

                    try:
                        await JobRepository.increment_retry(job_id)
                    except Exception:
                        pass

                    backoff = 2 ** attempt
                    await asyncio.sleep(backoff)
                else:
                    break

        if final_status == "COMPLETED":
            # SUCCESS!
            # 1. Automatic local VPS file cleanup: delete local MP4, thumbnails, downloaded segments
            if config.CLEANUP_AFTER_UPLOAD and config.AUTO_CLEANUP:
                cleanup_service = client.cleanup_service  # type: ignore
                if cleanup_service and workspace_dir:
                    await cleanup_service.cleanup_workspace(workspace_dir, job_id=job_id)

            # 2. Update completion message in Telegram
            formatted_duration = format_duration(actual_duration)
            formatted_size = format_size(file_size)
            quality_tag = selected_format.quality or "Default"
            safe_title = html.escape(extraction.title)
            total_pipeline_duration = max(0.001, time.monotonic() - pipeline_start_time)

            completion_text = (
                "✅ <b>DOWNLOAD COMPLETE</b>\n\n"
                f"🎬 {safe_title}\n"
                f"📺 Quality: <b>{quality_tag}</b>\n"
                f"📦 Size: <b>{formatted_size}</b>\n"
                f"⏱ Duration: <b>{formatted_duration}</b>\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <b>Download:</b> {dl_duration:.1f}s ({dl_speed_mbs:.2f} MB/s)\n"
                f"⚡ <b>Upload:</b> {ul_duration:.1f}s ({ul_speed_mbs:.2f} MB/s)\n"
                f"🕒 <b>Total Time:</b> {total_pipeline_duration:.1f}s\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "✨ <i>Delivered successfully to Telegram</i>"
            )
            try:
                await message.edit_text(
                    completion_text,
                    reply_markup=back_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        else:
            final_status = "FAILED"
            error_to_record = f"Telegram upload failed: {last_upload_error}"
            logger.error(f"Telegram upload failed for job {job_id}: {last_upload_error}", exc_info=True)
            sanitized_err = sanitize_error(str(last_upload_error))
            await ErrorRepository.log_error(
                "UPLOAD_FAILED", str(last_upload_error),
                job_id=job_id, telegram_id=user_id
            )
            try:
                await message.edit_text(
                    f"❌ <b>UPLOAD FAILED</b>\n\n"
                    f"━━━━━━━━━━━━━━━━━━\n\n"
                    f"Video was downloaded and processed successfully, but Telegram delivery failed.\n\n"
                    f"<b>Reason:</b> {sanitized_err}",
                    reply_markup=back_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    except asyncio.CancelledError:
        final_status = "CANCELLED"
        error_to_record = "Cancelled by user"
        logger.info(f"Job {job_id} cancelled.")
    except Exception as e:
        final_status = "FAILED"
        error_to_record = str(e)
        logger.error(f"Download pipeline exception for job {job_id}: {e}", exc_info=True)
        sanitized_err = sanitize_error(str(e))
        try:
            await message.edit_text(
                f"❌ <b>Error:</b> {sanitized_err}",
                reply_markup=back_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    finally:
        cleanup_service = client.cleanup_service  # type: ignore
        # If user explicitly cancelled, clean up workspace immediately
        if final_status == "CANCELLED" and cleanup_service and workspace_dir:
            await cleanup_service.cleanup_workspace(workspace_dir, job_id=job_id)
        elif final_status == "COMPLETED" and cleanup_service and workspace_dir:
            # Fallback idempotent workspace cleanup
            if config.CLEANUP_AFTER_UPLOAD and config.AUTO_CLEANUP:
                await cleanup_service.cleanup_workspace(workspace_dir, job_id=job_id)

        # GUARANTEED: Always release the concurrency slot!
        await queue_manager.release_job(job_id, status=final_status, error=error_to_record)


async def _update_progress(message, extraction, progress: DownloadProgress):
    """Update the download progress message in Telegram."""
    try:
        pct = progress.percent / 100
        bar = make_progress_bar(pct)

        text = (
            f"⬇️ <b>DOWNLOADING VIDEO</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"🎬 <b>{extraction.title}</b>\n\n"
            f"{bar} {progress.percent:.0f}%\n\n"
        )

        if progress.downloaded_bytes > 0:
            text += f"📦 <b>Downloaded:</b> {format_size(progress.downloaded_bytes)}"
            if progress.total_bytes > 0:
                text += f" / {format_size(progress.total_bytes)}"
            text += "\n"
        elif progress.total_bytes > 0:
            text += f"📊 <b>Total:</b> {format_size(progress.total_bytes)}\n"

        if progress.speed > 0:
            text += f"⚡ <b>Download Speed:</b> {format_speed(progress.speed)}\n"
        if progress.eta is not None and progress.eta > 0:
            text += f"⏳ <b>ETA:</b> {format_eta(progress.eta)}\n"

        text += "\n<i>Status: Downloading streams concurrently...</i>"

        await message.edit_text(
            text,
            reply_markup=cancel_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


async def _update_upload_progress(
    message, extraction, current: int, total: int, start_time: float, job_id: str
):
    """Update the upload progress message in Telegram asynchronously without blocking upload workers."""
    try:
        elapsed = max(0.001, time.monotonic() - start_time)
        speed = current / elapsed if elapsed > 0 else 0.0
        pct = (current / total * 100) if total > 0 else 0
        remaining = max(0, total - current)
        eta = (remaining / speed) if speed > 0 and total > 0 else None
        bar = make_progress_bar(pct / 100)

        safe_title = html.escape(extraction.title) if extraction and extraction.title else "Video"
        text = (
            f"📤 <b>UPLOADING TO TELEGRAM</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"🎬 <b>{safe_title}</b>\n\n"
            f"{bar} {pct:.0f}%\n\n"
            f"⚡ <b>Upload Speed:</b> {format_speed(speed)}\n"
        )
        if eta is not None and eta > 0:
            text += f"⏳ <b>ETA:</b> {format_eta(eta)}\n"
        text += (
            f"📦 <b>Transferred:</b> {format_size(current)} / {format_size(total)}\n\n"
            f"<i>Status: Uploading to Telegram...</i>"
        )

        await message.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass

    try:
        await JobRepository.update_progress(
            job_id, upload_progress=pct, file_size=total
        )
    except Exception:
        pass
