import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler


class SecretFilter(logging.Filter):
    """Filter to redact sensitive information from logs."""
    SENSITIVE_PATTERNS = [
        "bot_token", "api_hash", "session_string",
        "token=", "authorization:", "password",
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, 'msg') and isinstance(record.msg, str):
            msg_lower = record.msg.lower()
            for pattern in self.SENSITIVE_PATTERNS:
                if pattern in msg_lower:
                    record.msg = "[REDACTED - sensitive content]"
                    break
        return True


def setup_logging(level: str = "INFO", log_dir: str = "./logs"):
    """Setup application logging with file and console handlers."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s",
        datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_fmt)
    console_handler.addFilter(SecretFilter())

    # File handler
    file_handler = RotatingFileHandler(
        log_path / "bot.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "%(asctime)s │ %(levelname)-8s │ %(name)s:%(lineno)d │ %(message)s"
    )
    file_handler.setFormatter(file_fmt)
    file_handler.addFilter(SecretFilter())

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Reduce noise from libraries
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
