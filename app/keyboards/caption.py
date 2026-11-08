from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def caption_input_keyboard() -> InlineKeyboardMarkup:
    """Generate keyboard for custom caption input step."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Skip Caption", callback_data="caption_skip")],
        [
            InlineKeyboardButton("🔙 Back to Quality", callback_data="caption_back_quality"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_download"),
        ],
    ])


def caption_confirm_keyboard() -> InlineKeyboardMarkup:
    """Generate confirmation keyboard before starting download."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start Download", callback_data="start_download_confirmed")],
        [InlineKeyboardButton("✏️ Edit Caption", callback_data="caption_edit")],
        [
            InlineKeyboardButton("🔙 Back", callback_data="caption_back_quality"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_download"),
        ],
    ])
