import logging
import shutil
from typing import Optional, List

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message, CallbackQuery

from app.keyboards.admin import admin_keyboard, confirm_keyboard
from app.keyboards.main import back_keyboard
from app.database.repository import UserRepository, JobRepository
from app.utils.validators import get_system_stats
from app.utils.formatters import format_size

logger = logging.getLogger(__name__)


def _sanitize_admin_error(err: Optional[str]) -> str:
    if not err:
        return "Unknown error"
    import re
    cleaned = re.sub(r'([?&][a-zA-Z0-9_-]+)=[^&\s]+', r'\1=[redacted]', err)
    cleaned = re.sub(r'token=[^&\s]+', 'token=[redacted]', cleaned)
    return cleaned[:100].strip()


def register(app: Client):
    def is_admin(user_id: int) -> bool:
        config = app.config  # type: ignore
        return config.is_admin(user_id)

    @app.on_message(filters.command("admin") & filters.private)
    async def admin_command(client: Client, message: Message):
        if not is_admin(message.from_user.id):
            await message.reply("🚫 Unauthorized.")
            return

        await _show_admin_panel(client, message)

    @app.on_callback_query(filters.regex("^admin_panel$"))
    async def admin_panel_callback(client: Client, callback: CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("Unauthorized", show_alert=True)
            return

        await _show_admin_panel_edit(client, callback)
        await callback.answer()

    async def _show_admin_panel(client, message):
        stats = get_system_stats()
        total_users = await UserRepository.get_total_count()
        total_downloads = await JobRepository.get_total_count()
        active_jobs = await JobRepository.get_active_count()
        queued_jobs = await JobRepository.get_queued_count()

        text = (
            "🛡️ <b>ADMIN CONTROL CENTER</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Total Users: <b>{total_users}</b>\n"
            f"📥 Total Downloads: <b>{total_downloads}</b>\n"
            f"⚡ Active Jobs: <b>{active_jobs}</b>\n"
            f"⏳ Queued Jobs: <b>{queued_jobs}</b>\n"
            f"💾 Disk: <b>{stats.get('disk_used_percent', 'N/A')}%</b>\n"
            f"💻 CPU: <b>{stats.get('cpu_percent', 'N/A')}%</b>\n"
            f"🧠 Memory: <b>{stats.get('memory_used_percent', 'N/A')}%</b>"
        )

        await message.reply(
            text,
            reply_markup=admin_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    async def _show_admin_panel_edit(client, callback):
        stats = get_system_stats()
        total_users = await UserRepository.get_total_count()
        total_downloads = await JobRepository.get_total_count()
        active_jobs = await JobRepository.get_active_count()
        queued_jobs = await JobRepository.get_queued_count()

        text = (
            "🛡️ <b>ADMIN CONTROL CENTER</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Total Users: <b>{total_users}</b>\n"
            f"📥 Total Downloads: <b>{total_downloads}</b>\n"
            f"⚡ Active Jobs: <b>{active_jobs}</b>\n"
            f"⏳ Queued Jobs: <b>{queued_jobs}</b>\n"
            f"💾 Disk: <b>{stats.get('disk_used_percent', 'N/A')}%</b>"
        )

        await callback.message.edit_text(
            text,
            reply_markup=admin_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @app.on_callback_query(filters.regex("^admin_stats$"))
    async def admin_stats(client: Client, callback: CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("Unauthorized", show_alert=True)
            return

        stats = get_system_stats()
        text = (
            "📊 <b>SYSTEM STATISTICS</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"💻 CPU Usage: <b>{stats.get('cpu_percent', 'N/A')}%</b>\n"
            f"🧠 Memory Usage: <b>{stats.get('memory_used_percent', 'N/A')}%</b>\n"
            f"🧠 Available RAM: <b>{stats.get('memory_available_mb', 'N/A')} MB</b>\n"
            f"💾 Disk Usage: <b>{stats.get('disk_used_percent', 'N/A')}%</b>\n"
            f"💾 Free Disk: <b>{stats.get('disk_free_mb', 'N/A')} MB</b>"
        )

        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
            ]),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()

    @app.on_callback_query(filters.regex("^admin_cleanup$"))
    async def admin_cleanup(client: Client, callback: CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("Unauthorized", show_alert=True)
            return

        await callback.message.edit_text(
            "🧹 <b>CLEANUP</b>\n\n"
            "Are you sure you want to clean up all temporary files?",
            reply_markup=confirm_keyboard("cleanup"),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()

    @app.on_callback_query(filters.regex(r"^confirm:cleanup$"))
    async def confirm_cleanup(client: Client, callback: CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("Unauthorized", show_alert=True)
            return

        cleanup_service = client.cleanup_service  # type: ignore
        await cleanup_service.cleanup_old_files()

        await callback.message.edit_text(
            "✅ <b>Cleanup complete!</b>",
            reply_markup=admin_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer("Cleaned up!")

    @app.on_message(filters.command("stats") & filters.private)
    async def stats_command(client: Client, message: Message):
        if not is_admin(message.from_user.id):
            await message.reply("🚫 Unauthorized.")
            return

        stats = get_system_stats()
        total_users = await UserRepository.get_total_count()
        total_downloads = await JobRepository.get_total_count()

        await message.reply(
            f"📊 <b>Stats</b>\n\n"
            f"👥 Users: {total_users}\n"
            f"📥 Downloads: {total_downloads}\n"
            f"💻 CPU: {stats.get('cpu_percent', 'N/A')}%\n"
            f"🧠 RAM: {stats.get('memory_used_percent', 'N/A')}%",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.command("ban") & filters.private)
    async def ban_command(client: Client, message: Message):
        if not is_admin(message.from_user.id):
            return

        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("Usage: /ban <user_id>")
            return

        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("Invalid user ID.")
            return

        config = client.config  # type: ignore
        if config.is_owner(target_id):
            await message.reply("Cannot ban the owner.")
            return

        await UserRepository.set_banned(target_id, True)
        await message.reply(f"✅ User {target_id} has been banned.")

    @app.on_message(filters.command("unban") & filters.private)
    async def unban_command(client: Client, message: Message):
        if not is_admin(message.from_user.id):
            return

        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("Usage: /unban <user_id>")
            return

        try:
            target_id = int(parts[1])
        except ValueError:
            await message.reply("Invalid user ID.")
            return

        await UserRepository.set_banned(target_id, False)
        await message.reply(f"✅ User {target_id} has been unbanned.")

    @app.on_message(filters.command("broadcast") & filters.private)
    async def broadcast_command(client: Client, message: Message):
        config = client.config  # type: ignore
        if not config.is_owner(message.from_user.id):
            await message.reply("🚫 Only the owner can broadcast.")
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("Usage: /broadcast <message>")
            return

        broadcast_text = parts[1]
        user_ids = await UserRepository.get_all_ids()

        sent = 0
        failed = 0
        for uid in user_ids:
            try:
                await client.send_message(uid, broadcast_text)
                sent += 1
            except Exception:
                failed += 1

        await message.reply(
            f"📢 Broadcast complete.\n"
            f"✅ Sent: {sent}\n"
            f"❌ Failed: {failed}"
        )

    @app.on_message(filters.command("users") & filters.private)
    async def users_command(client: Client, message: Message):
        if not is_admin(message.from_user.id):
            await message.reply("🚫 Unauthorized.")
            return

        total = await UserRepository.get_total_count()
        recent = await UserRepository.get_recent_users(limit=10)

        lines = [f"👥 <b>TOTAL USERS:</b> {total}\n\n<b>Recent Users:</b>"]
        for u in recent:
            banned_flag = " 🚫 (Banned)" if u.is_banned else ""
            uname = f"@{u.username}" if u.username else f"ID: {u.telegram_id}"
            lines.append(f"• {uname} (ID: <code>{u.telegram_id}</code>) — {u.total_downloads} dls{banned_flag}")

        await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("jobs") & filters.private)
    async def jobs_command(client: Client, message: Message):
        if not is_admin(message.from_user.id):
            await message.reply("🚫 Unauthorized.")
            return

        queue_mgr = client.queue_manager  # type: ignore
        queue_mgr.ensure_workers_alive()

        active_db_jobs = await JobRepository.get_all_active_jobs(limit=10)
        failed_db_jobs = await JobRepository.get_failed_jobs(limit=5)

        text = (
            f"📥 <b>JOB QUEUE & STATUS MONITOR</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"👷 Workers: <b>{queue_mgr.alive_workers_count}/{queue_mgr.max_concurrent} online</b>\n"
            f"⚡ Active processing: <b>{queue_mgr.active_count}</b>\n"
            f"⏳ In queue waiting: <b>{queue_mgr.queued_count}</b> (buffer: {queue_mgr.queue_size})\n"
            f"💾 Persisted active in DB: <b>{len(active_db_jobs)}</b>\n\n"
        )

        # Discrepancy warning
        if queue_mgr.queued_count > 0 and len(active_db_jobs) == 0:
            text += (
                "⚠️ <i>Warning: Queue reports waiting jobs but database has none. "
                "Run /recover_jobs to synchronize.</i>\n\n"
            )

        # Active & Queued Jobs
        text += "<b>⚡ Active & Queued Jobs:</b>\n"
        if not active_db_jobs and queue_mgr.queued_count == 0 and queue_mgr.active_count == 0:
            text += "<i>No active or queued jobs.</i>\n\n"
        else:
            if active_db_jobs:
                for j in active_db_jobs:
                    q = j.selected_quality or "Best"
                    text += f"• <code>{j.job_id[:8]}</code> | User <code>{j.telegram_id}</code> | <b>{j.status}</b> ({q})\n"
            elif queue_mgr._active_jobs:
                for j_id, j in queue_mgr._active_jobs.items():
                    text += f"• <code>{j_id[:8]}</code> [Memory] | User <code>{j.telegram_id}</code> | <b>{j.status}</b> ({j.quality})\n"
            text += "\n"

        # Failed Jobs
        text += "<b>❌ Recent Failed Jobs:</b>\n"
        if not failed_db_jobs:
            text += "<i>No failed jobs recorded.</i>\n"
        else:
            for j in failed_db_jobs:
                sanitized_msg = _sanitize_admin_error(j.error_message)
                q = j.selected_quality or "Best"
                text += f"• <code>{j.job_id[:8]}</code> | User <code>{j.telegram_id}</code> | {sanitized_msg}\n"

        await message.reply(text, parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("recover_jobs") & filters.private)
    async def recover_jobs_command(client: Client, message: Message):
        if not is_admin(message.from_user.id):
            await message.reply("🚫 Unauthorized.")
            return

        queue_mgr = client.queue_manager  # type: ignore
        res = await queue_mgr.reconcile_stale_jobs()

        if isinstance(res, int):
            orphaned_memory = 0
            reconciled_db = res
            active_cnt = queue_mgr.active_count
            queued_cnt = queue_mgr.queued_count
            q_size = queue_mgr.queue_size
            alive_w = queue_mgr.alive_workers_count
        else:
            orphaned_memory = getattr(res, "orphaned_memory_jobs", 0)
            reconciled_db = getattr(res, "reconciled_db_jobs", 0)
            active_cnt = getattr(res, "active_count", queue_mgr.active_count)
            queued_cnt = getattr(res, "queued_count", queue_mgr.queued_count)
            q_size = getattr(res, "queue_size", queue_mgr.queue_size)
            alive_w = getattr(res, "alive_workers", queue_mgr.alive_workers_count)

        text = (
            f"🛠️ <b>JOB RECOVERY COMPLETED</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"🧹 Orphaned memory jobs cleared: <b>{orphaned_memory}</b>\n"
            f"🔄 Stale DB records reconciled: <b>{reconciled_db}</b>\n"
            f"👷 Worker tasks online: <b>{alive_w}/{queue_mgr.max_concurrent}</b>\n\n"
            f"📊 <b>Current Queue State:</b>\n"
            f"• Active processing: <b>{active_cnt}</b>\n"
            f"• In queue waiting: <b>{queued_cnt}</b>\n"
            f"• Queue buffer size: <b>{q_size}</b>\n\n"
            f"<i>Queue and database states are now fully synchronized.</i>"
        )
        await message.reply(text, parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("restart") & filters.private)
    async def restart_command(client: Client, message: Message):
        config = client.config  # type: ignore
        if not config.is_owner(message.from_user.id):
            await message.reply("🚫 Only the owner can restart the bot.")
            return

        await message.reply(
            "⚠️ <b>RESTART CONFIRMATION</b>\n\n"
            "Are you sure you want to restart the bot service?",
            reply_markup=confirm_keyboard("restart"),
            parse_mode=ParseMode.HTML
        )

    @app.on_callback_query(filters.regex(r"^confirm:restart$"))
    async def confirm_restart(client: Client, callback: CallbackQuery):
        config = client.config  # type: ignore
        if not config.is_owner(callback.from_user.id):
            await callback.answer("Unauthorized: Owner only", show_alert=True)
            return

        await callback.message.edit_text(
            "🔄 <b>Restarting bot...</b>\n\n<i>Shutting down current process cleanly. Systemd / Docker will restart it.</i>",
            parse_mode=ParseMode.HTML
        )
        await callback.answer("Restarting...")
        import os, sys
        # Clean exit to allow service manager (systemd/docker) to restart
        asyncio.create_task(_graceful_exit())

    async def _graceful_exit():
        await asyncio.sleep(1)
        import os, sys
        os._exit(0)

    @app.on_callback_query(filters.regex("^admin_users$"))
    async def admin_users(client: Client, callback: CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("Unauthorized", show_alert=True)
            return

        total = await UserRepository.get_total_count()

        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        await callback.message.edit_text(
            f"👥 <b>USER MANAGEMENT</b>\n\n"
            f"Total users: <b>{total}</b>\n\n"
            f"<b>Commands:</b>\n"
            f"/ban &lt;user_id&gt; - Ban a user\n"
            f"/unban &lt;user_id&gt; - Unban a user\n"
            f"/broadcast &lt;message&gt; - Send to all users",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
            ]),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()

    @app.on_callback_query(filters.regex("^admin_downloads$"))
    async def admin_downloads(client: Client, callback: CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("Unauthorized", show_alert=True)
            return

        queue_manager = client.queue_manager  # type: ignore
        active = queue_manager.active_count
        queued = queue_manager.queued_count

        text = (
            f"📥 <b>ACTIVE DOWNLOADS</b>\n\n"
            f"⚡ Processing: <b>{active}</b>\n"
            f"⏳ Queued: <b>{queued}</b>\n"
        )

        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
            ]),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()

    @app.on_callback_query(filters.regex("^admin_settings$"))
    async def admin_settings(client: Client, callback: CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("Unauthorized", show_alert=True)
            return

        config = client.config  # type: ignore
        text = (
            f"⚙️ <b>SYSTEM SETTINGS</b>\n\n"
            f"Max concurrent: <b>{config.MAX_CONCURRENT_DOWNLOADS}</b>\n"
            f"Max per user: <b>{config.MAX_DOWNLOADS_PER_USER}</b>\n"
            f"Max file size: <b>{config.MAX_FILE_SIZE_MB} MB</b>\n"
            f"Timeout: <b>{config.DOWNLOAD_TIMEOUT_SECONDS}s</b>\n"
            f"Auto cleanup: <b>{'Yes' if config.AUTO_CLEANUP else 'No'}</b>\n"
            f"User session: <b>{'Yes' if config.ENABLE_USER_SESSION else 'No'}</b>"
        )

        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
            ]),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
