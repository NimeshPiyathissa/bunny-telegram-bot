import logging
import os
import shutil
from pathlib import Path
from typing import Optional, Tuple

import psutil

logger = logging.getLogger(__name__)


def check_disk_space(path: str, required_mb: int = 500) -> Tuple[bool, str]:
    """Check if sufficient disk space is available."""
    try:
        usage = shutil.disk_usage(path)
        free_mb = usage.free // (1024 * 1024)
        if free_mb < required_mb:
            return False, f"Insufficient disk space. Available: {free_mb} MB, Required: {required_mb} MB"
        return True, f"Disk space OK: {free_mb} MB available"
    except Exception as e:
        return False, f"Could not check disk space: {e}"


def check_file_size(file_path: str, max_size_bytes: int) -> Tuple[bool, str]:
    """Check if a file is within the size limit."""
    try:
        size = os.path.getsize(file_path)
        if size > max_size_bytes:
            return False, f"File size ({size} bytes) exceeds maximum ({max_size_bytes} bytes)"
        if size == 0:
            return False, "File is empty"
        return True, f"File size OK: {size} bytes"
    except FileNotFoundError:
        return False, "File not found"
    except Exception as e:
        return False, f"Could not check file: {e}"


def validate_output_file(file_path: str) -> Tuple[bool, str]:
    """Validate that an output file exists and is a valid MP4."""
    path = Path(file_path)
    if not path.exists():
        return False, "Output file does not exist"
    if path.stat().st_size == 0:
        return False, "Output file is empty"
    # Check for MP4 magic bytes (ftyp)
    try:
        with open(file_path, 'rb') as f:
            header = f.read(12)
            if b'ftyp' not in header:
                return False, "File does not appear to be a valid MP4"
    except Exception as e:
        return False, f"Could not validate file: {e}"
    return True, "Output file is valid"


def get_system_stats() -> dict:
    """Get current system statistics."""
    try:
        cpu_percent = psutil.cpu_percent(interval=0.1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('.')
        return {
            "cpu_percent": cpu_percent,
            "memory_used_percent": memory.percent,
            "memory_available_mb": memory.available // (1024 * 1024),
            "disk_used_percent": disk.percent,
            "disk_free_mb": disk.free // (1024 * 1024),
        }
    except Exception:
        return {}
