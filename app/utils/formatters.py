import re
from datetime import timedelta
from typing import Optional


def format_size(size_bytes: int) -> str:
    """Format bytes to human-readable size."""
    if size_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0
    size = float(size_bytes)
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.2f} {units[unit_index]}"


def format_duration(seconds: int) -> str:
    """Format seconds to HH:MM:SS."""
    if seconds <= 0:
        return "00:00"
    td = timedelta(seconds=int(seconds))
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_speed(bytes_per_sec: float) -> str:
    """Format download speed."""
    if bytes_per_sec <= 0:
        return "0 B/s"
    return f"{format_size(int(bytes_per_sec))}/s"


def format_eta(seconds: Optional[float]) -> str:
    """Format ETA."""
    if seconds is None or seconds <= 0:
        return "--:--"
    return format_duration(int(seconds))


def make_progress_bar(progress: float, length: int = 16) -> str:
    """Create a text progress bar."""
    progress = max(0.0, min(1.0, progress))
    filled = int(length * progress)
    bar = "█" * filled + "░" * (length - filled)
    return bar


def sanitize_filename(filename: str, max_length: int = 200) -> str:
    """Sanitize a filename for safe filesystem use."""
    # Remove/replace dangerous characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    filename = re.sub(r'[\x00-\x1f\x7f]', '', filename)
    filename = filename.strip('. ')
    # Limit length
    if len(filename) > max_length:
        name_part = filename[:max_length - 10]
        ext_match = re.search(r'\.[a-zA-Z0-9]{1,6}$', filename)
        ext = ext_match.group(0) if ext_match else ''
        filename = name_part + ext
    return filename or "download"


def truncate_text(text: str, max_length: int = 100) -> str:
    """Truncate text with ellipsis."""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."
