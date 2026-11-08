import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional
from dotenv import load_dotenv


def _get_env(key: str, default: str = "", required: bool = False) -> str:
    val = os.getenv(key, default)
    return val


@dataclass
class Config:
    # Telegram
    BOT_TOKEN: str = ""
    API_ID: int = 0
    API_HASH: str = ""

    # Admin
    OWNER_ID: int = 0
    ADMIN_IDS: List[int] = field(default_factory=list)

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/bot.db"

    # Download Settings
    MAX_CONCURRENT_DOWNLOADS: int = 2
    MAX_DOWNLOADS_PER_USER: int = 1
    MAX_FILE_SIZE_MB: int = 1900
    DOWNLOAD_TIMEOUT_SECONDS: int = 3600
    UPLOAD_TIMEOUT_SECONDS: int = 1800

    # Storage & Cleanup Lifecycle Settings
    AUTO_CLEANUP: bool = True
    CLEANUP_AFTER_UPLOAD: bool = True
    FAILED_JOB_RETENTION_HOURS: int = 24
    TEMP_FILE_RETENTION_HOURS: int = 6
    MIN_FREE_DISK_GB: int = 3

    # Directories
    DOWNLOAD_DIRECTORY: str = "./downloads"
    TEMP_DIRECTORY: str = "./temp"
    LOG_DIRECTORY: str = "./logs"
    DATA_DIRECTORY: str = "./data"
    BACKUP_DIRECTORY: str = "./backups"

    # Logging
    LOG_LEVEL: str = "INFO"

    # Optional user session
    ENABLE_USER_SESSION: bool = False
    SESSION_STRING: str = ""

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from environment variables."""
        # Find and load .env from project root
        env_path = Path(__file__).parent / ".env"
        load_dotenv(env_path)

        def _safe_int(key: str, default: str) -> int:
            val = _get_env(key, default).strip()
            try:
                return int(val)
            except ValueError:
                return int(default)

        config = cls(
            BOT_TOKEN=_get_env("BOT_TOKEN", required=True),
            API_ID=_safe_int("API_ID", "0"),
            API_HASH=_get_env("API_HASH", required=True),
            OWNER_ID=_safe_int("OWNER_ID", "0"),
            ADMIN_IDS=cls._parse_admin_ids(_get_env("ADMIN_IDS", "")),
            DATABASE_URL=_get_env("DATABASE_URL", "sqlite+aiosqlite:///./data/bot.db"),
            MAX_CONCURRENT_DOWNLOADS=_safe_int("MAX_CONCURRENT_DOWNLOADS", "2"),
            MAX_DOWNLOADS_PER_USER=_safe_int("MAX_DOWNLOADS_PER_USER", "1"),
            MAX_FILE_SIZE_MB=_safe_int("MAX_FILE_SIZE_MB", "1900"),
            DOWNLOAD_TIMEOUT_SECONDS=_safe_int("DOWNLOAD_TIMEOUT_SECONDS", "3600"),
            UPLOAD_TIMEOUT_SECONDS=_safe_int("UPLOAD_TIMEOUT_SECONDS", "1800"),
            AUTO_CLEANUP=_get_env("AUTO_CLEANUP", "true").lower() == "true",
            CLEANUP_AFTER_UPLOAD=_get_env("CLEANUP_AFTER_UPLOAD", "true").lower() == "true",
            FAILED_JOB_RETENTION_HOURS=_safe_int("FAILED_JOB_RETENTION_HOURS", "24"),
            TEMP_FILE_RETENTION_HOURS=_safe_int("TEMP_FILE_RETENTION_HOURS", "6"),
            MIN_FREE_DISK_GB=_safe_int("MIN_FREE_DISK_GB", "3"),
            DOWNLOAD_DIRECTORY=_get_env("DOWNLOAD_DIRECTORY", "./downloads"),
            TEMP_DIRECTORY=_get_env("TEMP_DIRECTORY", "./temp"),
            LOG_DIRECTORY=_get_env("LOG_DIRECTORY", "./logs"),
            DATA_DIRECTORY=_get_env("DATA_DIRECTORY", "./data"),
            BACKUP_DIRECTORY=_get_env("BACKUP_DIRECTORY", "./backups"),
            LOG_LEVEL=_get_env("LOG_LEVEL", "INFO"),
            ENABLE_USER_SESSION=_get_env("ENABLE_USER_SESSION", "false").lower() == "true",
            SESSION_STRING=_get_env("SESSION_STRING", ""),
        )
        config._ensure_directories()
        return config

    @staticmethod
    def _parse_admin_ids(raw: str) -> List[int]:
        if not raw.strip():
            return []
        ids = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
                ids.append(int(part))
        return ids

    def _ensure_directories(self):
        for d in [
            self.DOWNLOAD_DIRECTORY,
            self.TEMP_DIRECTORY,
            self.LOG_DIRECTORY,
            self.DATA_DIRECTORY,
            self.BACKUP_DIRECTORY,
        ]:
            Path(d).mkdir(parents=True, exist_ok=True)

    def is_admin(self, user_id: int) -> bool:
        return user_id == self.OWNER_ID or user_id in self.ADMIN_IDS

    def is_owner(self, user_id: int) -> bool:
        return user_id == self.OWNER_ID

    @property
    def max_file_size_bytes(self) -> int:
        return self.MAX_FILE_SIZE_MB * 1024 * 1024

    @property
    def min_free_disk_bytes(self) -> int:
        return self.MIN_FREE_DISK_GB * 1024 * 1024 * 1024

    def validate(self, exit_on_error: bool = True) -> List[str]:
        """Validate configuration, returning a list of configuration error messages."""
        errors = []
        if not self.BOT_TOKEN:
            errors.append("BOT_TOKEN is required")
        if not self.API_ID:
            errors.append("API_ID is required (must be integer)")
        if not self.API_HASH:
            errors.append("API_HASH is required")
        if not self.OWNER_ID:
            errors.append("OWNER_ID is required (must be integer)")
        if self.MAX_CONCURRENT_DOWNLOADS <= 0:
            errors.append("MAX_CONCURRENT_DOWNLOADS must be greater than 0")
        if self.MAX_DOWNLOADS_PER_USER <= 0:
            errors.append("MAX_DOWNLOADS_PER_USER must be greater than 0")
        if self.MIN_FREE_DISK_GB < 1:
            errors.append("MIN_FREE_DISK_GB must be at least 1 GB")
        if self.FAILED_JOB_RETENTION_HOURS <= 0:
            errors.append("FAILED_JOB_RETENTION_HOURS must be greater than 0")
        if self.TEMP_FILE_RETENTION_HOURS <= 0:
            errors.append("TEMP_FILE_RETENTION_HOURS must be greater than 0")
        if self.ENABLE_USER_SESSION and not self.SESSION_STRING:
            errors.append("SESSION_STRING is required when ENABLE_USER_SESSION is true")

        if exit_on_error and errors:
            for e in errors:
                print(f"[CONFIG ERROR] {e}")
            sys.exit(1)

        return errors

    def validate_or_exit(self):
        self.validate(exit_on_error=True)

    def __repr__(self):
        return "Config(BOT_TOKEN=***, API_HASH=***, ...)"


# Global config instance
config = Config.load()
