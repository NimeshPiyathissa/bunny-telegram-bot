from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Manage Users", callback_data="admin_users")],
        [InlineKeyboardButton("📥 Active Downloads", callback_data="admin_downloads")],
        [InlineKeyboardButton("⚙️ System Settings", callback_data="admin_settings")],
        [InlineKeyboardButton("🧹 Cleanup", callback_data="admin_cleanup")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
    ])


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{action}"),
            InlineKeyboardButton("❌ Cancel", callback_data="admin_panel"),
        ],
    ])
