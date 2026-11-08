import asyncio
import gc
import logging
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Optional, Set, Tuple

from app.utils.formatters import format_size

logger = logging.getLogger(__name__)


class CleanupService:
    """Robust, idempotent temporary-file and storage lifecycle management service.

    Ensures safe deletion on Windows (with lock retries and gc), Ubuntu, and Docker,
    prevents path traversal outside configured workspaces, strictly protects active jobs,
    databases, sessions, and configuration, and logs reclaimed storage space.
    """

    def __init__(self, config, max_age_seconds: Optional[int] = None):
        self.config = config
        self.max_age_seconds = max_age_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._queue_manager = None

    def set_queue_manager(self, queue_manager):
        """Set queue manager reference to inspect active jobs and protect them from cleanup."""
        self._queue_manager = queue_manager

    async def start(self):
        """Start the background scheduled cleanup worker."""
        self._running = True
        if self.config.AUTO_CLEANUP:
            # Run an initial startup orphan cleanup to recover space from prior runs
            asyncio.create_task(self.startup_recovery())
            self._task = asyncio.create_task(self._cleanup_loop())
            logger.info(
                f"[STORAGE:INIT] Cleanup service started (interval=10m, "
                f"temp_retention={self.config.TEMP_FILE_RETENTION_HOURS}h, "
                f"failed_retention={self.config.FAILED_JOB_RETENTION_HOURS}h, "
                f"min_disk={self.config.MIN_FREE_DISK_GB}GB)."
            )

    async def stop(self):
        """Stop the background cleanup loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("[STORAGE:STOP] Cleanup service stopped.")

    async def _cleanup_loop(self):
        """Periodically run orphan file cleanup and disk space monitoring every 10 minutes."""
        while self._running:
            try:
                await asyncio.sleep(600)  # 10 minutes
                await self.cleanup_old_files()
                await self.check_disk_space_threshold()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[STORAGE:ERROR] Cleanup loop error: {e}", exc_info=True)

    async def startup_recovery(self) -> Tuple[int, int]:
        """Safe startup recovery: clean orphaned temporary workspaces from prior crashed runs."""
        try:
            logger.info("[STORAGE:STARTUP] Performing startup storage recovery check...")
            return await self.cleanup_old_files(force_startup=True)
        except Exception as e:
            logger.warning(f"[STORAGE:STARTUP] Startup recovery error: {e}")
            return (0, 0)

    def _is_safe_path(self, target_path: Path) -> bool:
        """Verify that target_path is strictly within configured temporary or download directories.

        Strictly prevents path traversal and protects root, data, database, session,
        and configuration files from accidental deletion.
        """
        try:
            canonical_target = target_path.resolve()

            # Root protection: never delete root directories or drive roots
            if canonical_target == canonical_target.parent:
                return False

            # Protected names check: never delete config, db, wal, or session files
            protected_patterns = {
                ".env", "bot.db", "bot.db-wal", "bot.db-shm", "bot.db-journal",
                "bunny_bot.session", ".git", ".venv", "config.py", "bot.py"
            }
            if any(p in canonical_target.name for p in protected_patterns):
                return False

            # Canonical allowed bases
            allowed_bases = []
            for d in [self.config.TEMP_DIRECTORY, self.config.DOWNLOAD_DIRECTORY]:
                if d:
                    try:
                        allowed_bases.append(Path(d).resolve())
                    except Exception:
                        pass

            # Target must be strictly a child of an allowed base (not the base directory itself!)
            for base in allowed_bases:
                if canonical_target == base:
                    return False  # Never delete the base directory itself
                try:
                    canonical_target.relative_to(base)
                    return True
                except ValueError:
                    continue

            return False
        except Exception as e:
            logger.error(f"[SECURITY:PATH] Path safety check rejected path '{target_path}': {e}")
            return False

    async def safe_unlink(self, file_path: Path, max_retries: int = 3, delay: float = 0.25) -> Tuple[bool, int]:
        """Safely remove a file, handling Windows file locks, read-only flags, and gc gracefully."""
        if not file_path.exists():
            return (True, 0)

        # Don't delete .gitkeep
        if file_path.name == ".gitkeep":
            return (False, 0)

        if not self._is_safe_path(file_path):
            logger.error(f"[SECURITY:BLOCKED] Refused to unlink unsafe path outside workspace: {file_path}")
            return (False, 0)

        file_size = 0
        try:
            file_size = file_path.stat().st_size
        except Exception:
            pass

        for attempt in range(max_retries):
            try:
                # Clear read-only flag on Windows if set
                if os.name == 'nt':
                    try:
                        os.chmod(file_path, stat.S_IWRITE)
                    except Exception:
                        pass
                file_path.unlink(missing_ok=True)
                return (True, file_size)
            except (PermissionError, OSError) as e:
                # Handle Windows file lock (WinError 32: The process cannot access the file because it is being used)
                gc.collect()
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay * (2 ** attempt))
                else:
                    logger.warning(f"[STORAGE:LOCK] Could not unlink locked file {file_path} after {max_retries} attempts: {e}")
                    return (False, 0)
            except Exception as e:
                logger.warning(f"[STORAGE:ERROR] Unlink failed for {file_path}: {e}")
                return (False, 0)

        return (False, 0)

    async def cleanup_workspace(self, workspace_path: str | Path, job_id: Optional[str] = None) -> Tuple[int, int]:
        """Idempotently and safely clean up an entire per-job workspace directory."""
        if not workspace_path:
            return (0, 0)

        path = Path(workspace_path)
        if not path.exists():
            return (0, 0)

        if not self._is_safe_path(path):
            logger.error(f"[SECURITY:BLOCKED] Refused to remove directory outside allowed workspace: {workspace_path}")
            return (0, 0)

        total_files = 0
        reclaimed_bytes = 0

        try:
            if path.is_file():
                success, size = await self.safe_unlink(path)
                if success:
                    total_files += 1
                    reclaimed_bytes += size
            elif path.is_dir():
                # Recursively delete files first
                for child in list(path.rglob("*")):
                    if child.is_file():
                        success, size = await self.safe_unlink(child)
                        if success:
                            total_files += 1
                            reclaimed_bytes += size

                # Remove empty subdirectories from deepest first
                for dirpath, dirnames, filenames in os.walk(str(path), topdown=False):
                    d = Path(dirpath)
                    if d != path and d.exists() and not list(d.iterdir()):
                        try:
                            d.rmdir()
                        except Exception:
                            pass

                # Remove top-level workspace directory if empty
                if path.exists() and not list(path.iterdir()):
                    try:
                        path.rmdir()
                    except Exception as e:
                        logger.warning(f"[STORAGE:RMDIR] Could not remove workspace dir {path}: {e}")
        except Exception as e:
            logger.error(f"[STORAGE:ERROR] Error cleaning workspace {workspace_path}: {e}")

        if total_files > 0:
            job_info = f" (job {job_id})" if job_id else ""
            logger.info(
                f"[STORAGE:CLEANUP] Reclaimed {format_size(reclaimed_bytes)} across "
                f"{total_files} file(s) from workspace {path.name}{job_info}."
            )

        return (total_files, reclaimed_bytes)

    async def cleanup_job_files(self, *paths: str, job_id: Optional[str] = None) -> Tuple[int, int]:
        """Clean up specific files from a job and remove their parent workspace if empty."""
        total_files = 0
        reclaimed_bytes = 0
        workspaces_to_check = set()

        for path_str in paths:
            if not path_str:
                continue
            p = Path(path_str)
            if p.exists() and p.is_file():
                workspaces_to_check.add(p.parent)
                success, size = await self.safe_unlink(p)
                if success:
                    total_files += 1
                    reclaimed_bytes += size

        # If the parent directory was an isolated job workspace, clean it up if empty
        for parent_dir in workspaces_to_check:
            if parent_dir.name.startswith("job_") and parent_dir.exists() and self._is_safe_path(parent_dir):
                if not list(parent_dir.iterdir()):
                    try:
                        parent_dir.rmdir()
                    except Exception:
                        pass

        return (total_files, reclaimed_bytes)

    def _get_active_job_ids(self) -> Set[str]:
        """Retrieve set of currently active job IDs to prevent deleting active files."""
        if self._queue_manager:
            try:
                return set(self._queue_manager.get_active_job_ids())
            except Exception:
                pass
        return set()

    async def cleanup_old_files(self, force_startup: bool = False) -> Tuple[int, int]:
        """Scan temp and download directories and remove orphaned or abandoned media files."""
        now = time.time()
        temp_retention_sec = (
            self.max_age_seconds
            if self.max_age_seconds is not None
            else (self.config.TEMP_FILE_RETENTION_HOURS * 3600)
        )
        active_ids = self._get_active_job_ids()

        total_files = 0
        total_reclaimed = 0

        for directory in [self.config.TEMP_DIRECTORY, self.config.DOWNLOAD_DIRECTORY]:
            dir_path = Path(directory)
            if not dir_path.exists() or not dir_path.is_dir():
                continue

            for item in list(dir_path.iterdir()):
                if item.name == ".gitkeep":
                    continue

                # Check if this item belongs to an active job
                is_active = False
                for active_id in active_ids:
                    if active_id and active_id in item.name:
                        is_active = True
                        break
                if is_active:
                    continue  # Strictly protect active jobs!

                try:
                    mtime = item.stat().st_mtime
                    age = now - mtime

                    # On startup recovery or if older than retention limit
                    if force_startup or age > temp_retention_sec:
                        if item.is_dir() and item.name.startswith("job_"):
                            f_count, b_count = await self.cleanup_workspace(item)
                            total_files += f_count
                            total_reclaimed += b_count
                        elif item.is_file():
                            success, size = await self.safe_unlink(item)
                            if success:
                                total_files += 1
                                total_reclaimed += size
                except Exception as e:
                    logger.debug(f"[STORAGE:CHECK] Could not inspect {item}: {e}")

        if total_files > 0:
            logger.info(
                f"[STORAGE:ORPHAN_CLEANUP] Reclaimed {format_size(total_reclaimed)} across "
                f"{total_files} orphaned file(s)."
            )

        return (total_files, total_reclaimed)

    async def check_disk_space_threshold(self) -> Tuple[bool, float, str]:
        """Check available disk space against MIN_FREE_DISK_GB.

        If disk space is lower than threshold, triggers an emergency cleanup of orphan files.
        """
        try:
            total, used, free = shutil.disk_usage(self.config.TEMP_DIRECTORY)
            free_gb = free / (1024 ** 3)
            min_gb = self.config.MIN_FREE_DISK_GB

            if free_gb < min_gb:
                logger.warning(
                    f"[STORAGE:LOW_DISK] Free disk space is {free_gb:.2f} GB (minimum: {min_gb} GB). "
                    f"Triggering emergency orphan cleanup..."
                )
                await self.cleanup_old_files(force_startup=True)

                # Re-check
                _, _, free_after = shutil.disk_usage(self.config.TEMP_DIRECTORY)
                free_gb_after = free_after / (1024 ** 3)

                if free_gb_after < min_gb:
                    msg = (
                        f"Low disk space: {free_gb_after:.2f} GB free. "
                        f"Minimum required is {min_gb} GB."
                    )
                    return (False, free_gb_after, msg)

            return (True, free_gb, "OK")
        except Exception as e:
            logger.error(f"[STORAGE:DISK_CHECK] Error reading disk usage: {e}")
            return (True, 0.0, "Could not check disk usage")

    async def get_temp_disk_usage(self) -> int:
        """Get total byte size of temporary and download files."""
        total = 0
        for directory in [self.config.TEMP_DIRECTORY, self.config.DOWNLOAD_DIRECTORY]:
            dir_path = Path(directory)
            if dir_path.exists():
                for file_path in dir_path.rglob('*'):
                    if file_path.is_file() and file_path.name != ".gitkeep":
                        try:
                            total += file_path.stat().st_size
                        except Exception:
                            pass
        return total
