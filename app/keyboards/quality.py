from typing import List
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.services.bunny_extractor import VideoFormat


def quality_keyboard(formats: List[VideoFormat], job_id: str = "") -> InlineKeyboardMarkup:
    """Generate quality selection keyboard from detected formats."""
    buttons = []
    row = []

    # Sort by height
    sorted_formats = sorted(formats, key=lambda f: f.height)

    for fmt in sorted_formats:
        label = fmt.quality
        if fmt.filesize:
            size_mb = fmt.filesize / (1024 * 1024)
            label += f" ({size_mb:.0f}MB)"

        callback = f"quality:{fmt.format_id}:{fmt.height}"
        row.append(InlineKeyboardButton(label, callback_data=callback))

        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    # Best available button
    buttons.append([
        InlineKeyboardButton("⭐ Best Available", callback_data="quality:best:0")
    ])

    # Cancel button
    buttons.append([
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_download")
    ])

    return InlineKeyboardMarkup(buttons)
