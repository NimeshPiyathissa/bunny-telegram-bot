from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Download Video", callback_data="download_video")],
        [
            InlineKeyboardButton("📂 My Downloads", callback_data="my_downloads"),
            InlineKeyboardButton("📊 Status", callback_data="bot_status"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
            InlineKeyboardButton("❓ Help", callback_data="help"),
        ],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_download")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
    ])


def workflow_conflict_keyboard() -> InlineKeyboardMarkup:
    """Keyboard offered when user sends a new link during an unfinished workflow."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Continue Current", callback_data="workflow_continue"),
            InlineKeyboardButton("🔄 Replace with New Link", callback_data="workflow_replace"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_download")],
    ])
