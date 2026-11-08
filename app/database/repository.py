import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, update, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User, DownloadJob, ErrorLog, get_session

logger = logging.getLogger(__name__)


class UserRepository:
    @staticmethod
    async def get_or_create(
        telegram_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> User:
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == telegram_id)
            )
            user = result.scalar_one_or_none()
            if user is None:
                user = User(
                    telegram_id=telegram_id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)
            else:
                user.username = username
                user.first_name = first_name
                user.last_name = last_name
                user.last_active = datetime.now(timezone.utc)
                await session.commit()
                await session.refresh(user)
            return user

    @staticmethod
    async def is_banned(telegram_id: int) -> bool:
        async with get_session() as session:
            result = await session.execute(
                select(User.is_banned).where(User.telegram_id == telegram_id)
            )
            banned = result.scalar_one_or_none()
            return banned is True

    @staticmethod
    async def set_banned(telegram_id: int, banned: bool) -> None:
        async with get_session() as session:
            await session.execute(
                update(User).where(User.telegram_id == telegram_id).values(is_banned=banned)
            )
            await session.commit()

    @staticmethod
    async def get_total_count() -> int:
        async with get_session() as session:
            result = await session.execute(select(func.count(User.id)))
            return result.scalar_one()

    @staticmethod
    async def get_recent_users(limit: int = 10) -> List[User]:
        async with get_session() as session:
            result = await session.execute(
                select(User).order_by(User.last_active.desc()).limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def get_all_ids() -> List[int]:
        async with get_session() as session:
            result = await session.execute(select(User.telegram_id))
            return [row[0] for row in result.all()]

    @staticmethod
    async def increment_downloads(telegram_id: int) -> None:
        async with get_session() as session:
            await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(total_downloads=User.total_downloads + 1)
            )
            await session.commit()


class JobRepository:
    @staticmethod
    async def create(
        job_id: str,
        telegram_id: int,
        url: str,
        video_title: Optional[str] = None,
        quality: Optional[str] = None,
    ) -> DownloadJob:
        async with get_session() as session:
            job = DownloadJob(
                job_id=job_id,
                telegram_id=telegram_id,
                url="[redacted]",  # Don't store full URLs with tokens
                video_title=video_title,
                selected_quality=quality,
                status="QUEUED",
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return job

    @staticmethod
    async def get_by_id(job_id: str) -> Optional[DownloadJob]:
        async with get_session() as session:
            result = await session.execute(
                select(DownloadJob).where(DownloadJob.job_id == job_id)
            )
            return result.scalar_one_or_none()

    @staticmethod
    async def update_status(job_id: str, status: str, error: Optional[str] = None) -> None:
        async with get_session() as session:
            values = {"status": status}
            if error:
                values["error_message"] = error
            if status in ("COMPLETED", "FAILED", "CANCELLED"):
                values["completed_at"] = datetime.now(timezone.utc)
            await session.execute(
                update(DownloadJob).where(DownloadJob.job_id == job_id).values(**values)
            )
            await session.commit()

    @staticmethod
    async def complete_job(
        job_id: str,
        telegram_message_id: Optional[int] = None,
        telegram_file_id: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> None:
        """Mark job as COMPLETED, recording Telegram delivery metadata."""
        async with get_session() as session:
            values = {
                "status": "COMPLETED",
                "completed_at": datetime.now(timezone.utc),
                "download_progress": 100.0,
                "upload_progress": 100.0,
            }
            if telegram_message_id is not None:
                values["telegram_message_id"] = telegram_message_id
            if telegram_file_id is not None:
                values["telegram_file_id"] = telegram_file_id
            if file_size is not None:
                values["file_size"] = file_size
            await session.execute(
                update(DownloadJob).where(DownloadJob.job_id == job_id).values(**values)
            )
            await session.commit()

    @staticmethod
    async def set_workspace(job_id: str, workspace_dir: str) -> None:
        """Associate an isolated workspace directory with a job."""
        async with get_session() as session:
            await session.execute(
                update(DownloadJob)
                .where(DownloadJob.job_id == job_id)
                .values(workspace_dir=workspace_dir)
            )
            await session.commit()

    @staticmethod
    async def increment_retry(job_id: str) -> int:
        """Increment and return retry counter for a job."""
        async with get_session() as session:
            result = await session.execute(
                select(DownloadJob.retry_count).where(DownloadJob.job_id == job_id)
            )
            current = result.scalar_one_or_none() or 0
            new_count = current + 1
            await session.execute(
                update(DownloadJob)
                .where(DownloadJob.job_id == job_id)
                .values(retry_count=new_count)
            )
            await session.commit()
            return new_count

    @staticmethod
    async def mark_recovery(job_id: str, reason: str) -> None:
        """Mark job in RECOVERY state (e.g. uncertain upload status)."""
        async with get_session() as session:
            await session.execute(
                update(DownloadJob)
                .where(DownloadJob.job_id == job_id)
                .values(
                    status="RECOVERY",
                    error_message=reason,
                )
            )
            await session.commit()

    @staticmethod
    async def update_progress(
        job_id: str,
        download_progress: Optional[float] = None,
        upload_progress: Optional[float] = None,
        file_size: Optional[int] = None,
    ) -> None:
        async with get_session() as session:
            values = {}
            if download_progress is not None:
                values["download_progress"] = download_progress
            if upload_progress is not None:
                values["upload_progress"] = upload_progress
            if file_size is not None:
                values["file_size"] = file_size
            if values:
                await session.execute(
                    update(DownloadJob).where(DownloadJob.job_id == job_id).values(**values)
                )
                await session.commit()

    @staticmethod
    async def get_active_jobs(telegram_id: int) -> List[DownloadJob]:
        async with get_session() as session:
            result = await session.execute(
                select(DownloadJob).where(
                    DownloadJob.telegram_id == telegram_id,
                    DownloadJob.status.in_([
                        "QUEUED", "VALIDATING", "EXTRACTING",
                        "DOWNLOADING", "MERGING", "UPLOADING"
                    ])
                )
            )
            return list(result.scalars().all())

    @staticmethod
    async def get_user_history(telegram_id: int, limit: int = 10) -> List[DownloadJob]:
        async with get_session() as session:
            result = await session.execute(
                select(DownloadJob)
                .where(DownloadJob.telegram_id == telegram_id)
                .order_by(DownloadJob.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def get_total_count() -> int:
        async with get_session() as session:
            result = await session.execute(select(func.count(DownloadJob.id)))
            return result.scalar_one()

    @staticmethod
    async def get_active_count() -> int:
        async with get_session() as session:
            result = await session.execute(
                select(func.count(DownloadJob.id)).where(
                    DownloadJob.status.in_([
                        "QUEUED", "VALIDATING", "EXTRACTING",
                        "DOWNLOADING", "MERGING", "UPLOADING"
                    ])
                )
            )
            return result.scalar_one()

    @staticmethod
    async def get_all_active_jobs(limit: int = 20) -> List[DownloadJob]:
        async with get_session() as session:
            result = await session.execute(
                select(DownloadJob).where(
                    DownloadJob.status.in_([
                        "QUEUED", "VALIDATING", "EXTRACTING",
                        "DOWNLOADING", "MERGING", "UPLOADING"
                    ])
                ).order_by(DownloadJob.created_at.desc()).limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def get_queued_count() -> int:
        async with get_session() as session:
            result = await session.execute(
                select(func.count(DownloadJob.id)).where(DownloadJob.status == "QUEUED")
            )
            return result.scalar_one()

    @staticmethod
    async def get_failed_jobs(limit: int = 10) -> List[DownloadJob]:
        async with get_session() as session:
            result = await session.execute(
                select(DownloadJob)
                .where(DownloadJob.status == "FAILED")
                .order_by(DownloadJob.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def reconcile_stale_jobs() -> int:
        """Mark any uncompleted jobs from previous sessions as FAILED."""
        async with get_session() as session:
            result = await session.execute(
                update(DownloadJob)
                .where(
                    DownloadJob.status.in_([
                        "QUEUED", "VALIDATING", "EXTRACTING",
                        "DOWNLOADING", "MERGING", "UPLOADING"
                    ])
                )
                .values(
                    status="FAILED",
                    error_message="Interrupted by bot restart",
                    completed_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()
            return result.rowcount

    @staticmethod
    async def cancel_job(job_id: str) -> None:
        await JobRepository.update_status(job_id, "CANCELLED")


class ErrorRepository:
    @staticmethod
    async def log_error(
        error_type: str,
        error_message: str,
        job_id: Optional[str] = None,
        telegram_id: Optional[int] = None,
    ) -> None:
        async with get_session() as session:
            error = ErrorLog(
                job_id=job_id,
                telegram_id=telegram_id,
                error_type=error_type,
                error_message=error_message[:2000],  # Truncate long messages
            )
            session.add(error)
            await session.commit()
