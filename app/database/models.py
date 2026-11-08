import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, DateTime,
    Boolean, Float, Index, create_engine, text, event
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    total_downloads: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_active: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DownloadJob(Base):
    __tablename__ = "download_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)  # NOTE: Never store signed URLs long-term
    video_title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    selected_quality: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    download_progress: Mapped[float] = mapped_column(Float, default=0.0)
    upload_progress: Mapped[float] = mapped_column(Float, default=0.0)
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    telegram_file_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    workspace_dir: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index('ix_jobs_user_status', 'telegram_id', 'status'),
    )


class ErrorLog(Base):
    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    error_type: Mapped[str] = mapped_column(String(100), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# Async engine and session
_engine = None
_session_factory = None


async def init_db(database_url: str):
    """Initialize the database engine, create tables, and apply migrations."""
    global _engine, _session_factory

    # Ensure parent directory exists for SQLite
    if "sqlite" in database_url:
        import re
        from pathlib import Path
        match = re.search(r"sqlite(?:\+aiosqlite)?:///(.+)", database_url)
        if match:
            path_str = match.group(1)
            if path_str.startswith("/") and len(path_str) > 2 and path_str[2] == ":":
                path_str = path_str[1:]
            try:
                Path(path_str).parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

    _engine = create_async_engine(
        database_url,
        echo=False,
        pool_pre_ping=True,
    )

    if "sqlite" in database_url:
        @event.listens_for(_engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout = 10000;")
                cursor.execute("PRAGMA foreign_keys = ON;")
            except Exception as e:
                logger.debug(f"[DATABASE:PRAGMA] Error setting connection pragmas: {e}")
            finally:
                cursor.close()

    _session_factory = async_sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False
    )

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Configure WAL mode and busy_timeout for SQLite if supported by filesystem
        if "sqlite" in database_url:
            if ":memory:" not in database_url and "mode=memory" not in database_url:
                try:
                    res = await conn.execute(text("PRAGMA journal_mode=WAL;"))
                    mode = res.scalar()
                    if mode and mode.lower() == "wal":
                        await conn.execute(text("PRAGMA synchronous=NORMAL;"))
                        logger.info("[DATABASE:WAL] Safe WAL journal mode and busy_timeout=10000 active.")
                    else:
                        logger.warning(
                            f"[DATABASE:WAL] SQLite journal_mode '{mode}' active "
                            f"(WAL unsupported or not permitted by filesystem)."
                        )
                except Exception as wal_err:
                    logger.warning(
                        f"[DATABASE:WAL] Could not enable WAL mode: {wal_err}. "
                        f"Continuing in standard journal mode."
                    )

        # Lightweight schema migration for existing SQLite databases
        if "sqlite" in database_url:
            res = await conn.execute(text("PRAGMA table_info(download_jobs);"))
            columns = {row[1] for row in res.fetchall()}
            if columns:
                if "telegram_message_id" not in columns:
                    await conn.execute(text("ALTER TABLE download_jobs ADD COLUMN telegram_message_id BIGINT;"))
                if "telegram_file_id" not in columns:
                    await conn.execute(text("ALTER TABLE download_jobs ADD COLUMN telegram_file_id VARCHAR(255);"))
                if "workspace_dir" not in columns:
                    await conn.execute(text("ALTER TABLE download_jobs ADD COLUMN workspace_dir VARCHAR(500);"))
                if "retry_count" not in columns:
                    await conn.execute(text("ALTER TABLE download_jobs ADD COLUMN retry_count INTEGER DEFAULT 0;"))


def get_session() -> AsyncSession:
    """Get a new async session."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory()


async def close_db():
    """Dispose of the database engine."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
