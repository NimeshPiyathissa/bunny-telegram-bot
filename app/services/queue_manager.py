import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Callable, Any

from app.database.repository import JobRepository

logger = logging.getLogger(__name__)


@dataclass
class QueuedJob:
    job_id: str
    telegram_id: int
    url: str
    quality: str
    video_title: str = "Video"
    status: str = "QUEUED"
    callback: Optional[Callable] = None
    engine: Optional[Any] = None
    error: Optional[str] = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    seq: int = 0


@dataclass
class ReconciliationResult:
    reconciled_db_jobs: int = 0
    orphaned_memory_jobs: int = 0
    legitimate_active_jobs: int = 0
    active_count: int = 0
    queued_count: int = 0
    queue_size: int = 0
    alive_workers: int = 0

    def __int__(self) -> int:
        return self.reconciled_db_jobs + self.orphaned_memory_jobs

    def __eq__(self, other) -> bool:
        if isinstance(other, int):
            return int(self) == other
        return super().__eq__(other)


class QueueManager:
    """Manages download queue with concurrency control, persistent DB sync, and worker lifecycle."""

    ACTIVE_STATUSES = (
        "QUEUED", "VALIDATING", "EXTRACTING", "DOWNLOADING", "MERGING", "UPLOADING"
    )
    FINAL_STATUSES = ("COMPLETED", "FAILED", "CANCELLED")

    def __init__(self, config):
        self.config = config
        self.max_concurrent = config.MAX_CONCURRENT_DOWNLOADS
        self.max_per_user = config.MAX_DOWNLOADS_PER_USER
        self._queue: asyncio.Queue = asyncio.Queue()
        self._active_jobs: Dict[str, QueuedJob] = {}
        self._finished_jobs: Dict[str, QueuedJob] = {}
        self._running = False
        self._workers: list[asyncio.Task] = []
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._seq = 0

    async def start(self):
        """Start the queue workers and supervise health."""
        self._running = True
        self._workers.clear()
        for i in range(self.max_concurrent):
            worker = asyncio.create_task(self._worker(f"worker-{i}"))
            self._workers.append(worker)
        logger.info(f"[WORKER_POOL:INIT] Queue manager started with {self.max_concurrent} workers.")

    async def stop(self):
        """Stop the queue manager and cancel worker tasks."""
        self._running = False
        for worker in self._workers:
            worker.cancel()
        self._workers.clear()
        logger.info("[WORKER_POOL:STOP] Queue manager stopped.")

    def ensure_workers_alive(self) -> int:
        """Check worker tasks and restart any that died unexpectedly."""
        if not self._running:
            return 0
        restarted = 0
        for i, worker in enumerate(self._workers):
            if worker.done():
                exc = None
                try:
                    exc = worker.exception()
                except (asyncio.CancelledError, asyncio.InvalidStateError):
                    pass
                logger.warning(f"[WORKER:RESTART] Worker worker-{i} was done (exception={exc}). Respawning worker task.")
                self._workers[i] = asyncio.create_task(self._worker(f"worker-{i}"))
                restarted += 1
        return restarted

    @property
    def alive_workers_count(self) -> int:
        return sum(1 for w in self._workers if not w.done())

    def get_active_job_ids(self) -> list[str]:
        """Return list of all currently active or queued job IDs."""
        return list(self._active_jobs.keys())

    def is_user_busy(self, telegram_id: int) -> bool:
        """Check if a user currently has reached their maximum allowed active or queued downloads."""
        active_count = sum(
            1 for j in self._active_jobs.values()
            if j.telegram_id == telegram_id and j.status in self.ACTIVE_STATUSES
        )
        return active_count >= self.max_per_user

    async def add_job(
        self,
        telegram_id: int,
        url: str,
        quality: str,
        video_title: str = "Video",
        callback: Optional[Callable] = None,
    ) -> Optional[str]:
        """Add a download job to the queue with guaranteed database persistence and duplicate check."""
        # 1. Enforce per-user limit
        if self.is_user_busy(telegram_id):
            logger.warning(f"[QUEUE:LIMIT_REJECT] User {telegram_id} reached concurrent limit ({self.max_per_user}).")
            return None

        # 2. Prevent duplicate submissions for identical URL by the same user while active
        for j in self._active_jobs.values():
            if j.telegram_id == telegram_id and j.url == url and j.status in self.ACTIVE_STATUSES:
                logger.warning(f"[QUEUE:DUPLICATE_REJECT] Duplicate active submission rejected for user {telegram_id}, URL: {url}")
                return None

        self._seq += 1
        job_id = str(uuid.uuid4())

        # 3. Persistent Database Record MUST be created FIRST before memory queueing
        try:
            await JobRepository.create(
                job_id=job_id,
                telegram_id=telegram_id,
                url=url,
                video_title=video_title,
                quality=quality,
            )
        except Exception as e:
            logger.error(f"[QUEUE:DB_ERROR] Failed to persist job {job_id} into database: {e}", exc_info=True)
            return None

        # 4. Create in-memory QueuedJob record
        job = QueuedJob(
            job_id=job_id,
            telegram_id=telegram_id,
            url=url,
            quality=quality,
            video_title=video_title,
            callback=callback,
            seq=self._seq,
        )
        self._active_jobs[job_id] = job

        # 5. Enqueue into asyncio queue with rollback if enqueue fails
        try:
            await self._queue.put(job)
        except Exception as e:
            logger.error(f"[QUEUE:PUT_ERROR] Failed to put job {job_id} into asyncio.Queue: {e}", exc_info=True)
            self._active_jobs.pop(job_id, None)
            try:
                await JobRepository.update_status(job_id, "FAILED", f"Queue submission failed: {e}")
            except Exception:
                pass
            return None

        logger.info(
            f"[QUEUE:SUBMIT] Job {job_id} queued for user {telegram_id} "
            f"(active_jobs={len(self._active_jobs)}, qsize={self._queue.qsize()})"
        )
        return job_id

    async def update_job_status(
        self, job_id: str, status: str, error: Optional[str] = None
    ) -> None:
        """Update job status in memory and database, releasing slot if status is final."""
        job = self.get_job(job_id)
        old_status = job.status if job else "UNKNOWN"
        if job:
            job.status = status
            if error:
                job.error = error

        logger.info(f"[QUEUE:STATUS_TRANSITION] Job {job_id}: {old_status} -> {status}")

        try:
            await JobRepository.update_status(job_id, status, error)
        except Exception as e:
            logger.error(f"[QUEUE:DB_STATUS_ERROR] Error updating DB status for {job_id}: {e}")

        if status in self.FINAL_STATUSES:
            await self.release_job(job_id, status=status, error=error)

    async def release_job(
        self, job_id: str, status: Optional[str] = None, error: Optional[str] = None
    ) -> None:
        """Guaranteed release of job concurrency slot."""
        job = self._active_jobs.pop(job_id, None)
        if not job:
            job = self._finished_jobs.get(job_id)

        if job:
            if status:
                job.status = status
            if error:
                job.error = error
            self._finished_jobs[job_id] = job
            # Keep finished jobs bounded
            if len(self._finished_jobs) > 100:
                oldest_key = next(iter(self._finished_jobs))
                self._finished_jobs.pop(oldest_key, None)

        final_status = status or (job.status if job else None)
        if final_status and final_status in self.FINAL_STATUSES:
            try:
                await JobRepository.update_status(job_id, final_status, error)
            except Exception as e:
                logger.error(f"[QUEUE:RELEASE_DB_ERROR] Failed to update final status for {job_id}: {e}")

        logger.info(f"[QUEUE:RELEASE] Job {job_id} slot released (final_status={final_status or 'RELEASED'}).")

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a queued or active job and release its concurrency slot."""
        job = self.get_job(job_id)
        if job:
            job.cancel_event.set()
            job.status = "CANCELLED"
            if job.engine:
                try:
                    job.engine.cancel()
                    logger.info(f"[QUEUE:CANCEL_ENGINE] DownloadEngine cancelled for job {job_id}")
                except Exception as e:
                    logger.warning(f"Error cancelling engine for job {job_id}: {e}")

            await self.release_job(job_id, status="CANCELLED")
            logger.info(f"[QUEUE:CANCEL] Job {job_id} cancelled.")
            return True
        return False

    async def reconcile_stale_jobs(self) -> ReconciliationResult:
        """Safely detect and reconcile orphaned queue entries and stale database jobs without interrupting active downloads."""
        self.ensure_workers_alive()

        # 1. Reconcile database jobs from prior terminated sessions
        reconciled_db_count = await JobRepository.reconcile_stale_jobs()

        # 2. Fetch active records from database
        active_db_jobs = await JobRepository.get_all_active_jobs(limit=100)
        db_job_map = {j.job_id: j for j in active_db_jobs}

        orphaned_memory_count = 0
        legitimate_active_count = 0

        # Check in-memory active jobs
        for j_id, j in list(self._active_jobs.items()):
            # If job is marked as finished, release it
            if j.status in self.FINAL_STATUSES:
                logger.info(f"[RECONCILE] Releasing finished memory job {j_id} (status={j.status})")
                await self.release_job(j_id, status=j.status, error=j.error)
                orphaned_memory_count += 1
                continue

            # If job does not have an active matching record in DB
            if j_id not in db_job_map:
                db_record = await JobRepository.get_by_id(j_id)
                if db_record is None or db_record.status in self.FINAL_STATUSES:
                    logger.warning(
                        f"[RECONCILE] Orphaned in-memory job detected: {j_id} "
                        f"(memory status='{j.status}', DB status='{getattr(db_record, 'status', 'MISSING')}'). Releasing."
                    )
                    await self.release_job(j_id, status=getattr(db_record, "status", "CANCELLED"))
                    orphaned_memory_count += 1
                else:
                    legitimate_active_count += 1
            else:
                legitimate_active_count += 1

        logger.info(
            f"[RECONCILE] Reconciliation complete: {orphaned_memory_count} orphaned memory jobs cleared, "
            f"{reconciled_db_count} stale DB records reconciled, "
            f"{legitimate_active_count} active jobs preserved."
        )

        return ReconciliationResult(
            reconciled_db_jobs=reconciled_db_count,
            orphaned_memory_jobs=orphaned_memory_count,
            legitimate_active_jobs=legitimate_active_count,
            active_count=self.active_count,
            queued_count=self.queued_count,
            queue_size=self.queue_size,
            alive_workers=self.alive_workers_count,
        )

    def get_job(self, job_id: str) -> Optional[QueuedJob]:
        return self._active_jobs.get(job_id) or self._finished_jobs.get(job_id)

    def get_queue_position(self, job_id: str) -> int:
        """Get position in queue (0 = processing, -1 = not found)."""
        job = self.get_job(job_id)
        if not job:
            return -1
        if job.status != "QUEUED":
            return 0
        position = 1
        for j in self._active_jobs.values():
            if j.status == "QUEUED" and (j.seq < job.seq if j.seq and job.seq else j.created_at < job.created_at):
                position += 1
        return position

    @property
    def active_count(self) -> int:
        return sum(
            1 for j in self._active_jobs.values()
            if j.status in ("DOWNLOADING", "MERGING", "UPLOADING")
        )

    @property
    def queued_count(self) -> int:
        return sum(1 for j in self._active_jobs.values() if j.status == "QUEUED")

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    async def _worker(self, name: str):
        """Resilient queue worker that processes jobs and guarantees task_done() and slot release."""
        logger.info(f"[WORKER:START] Worker {name} started.")
        while self._running:
            job = None
            try:
                try:
                    job = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

                logger.info(f"[QUEUE:DEQUEUE] Worker {name} dequeued job {job.job_id} for user {job.telegram_id}")

                try:
                    if job.cancel_event.is_set():
                        logger.info(f"[QUEUE:CANCELLED_DEQUEUE] Job {job.job_id} was cancelled before worker pickup.")
                        await self.release_job(job.job_id, status="CANCELLED")
                        continue

                    try:
                        async with self._semaphore:
                            if job.callback:
                                logger.info(f"[QUEUE:PROCESS] Worker {name} executing pipeline callback for job {job.job_id}")
                                await job.callback(job)
                            else:
                                logger.warning(f"[QUEUE:NO_CALLBACK] Worker {name} received job {job.job_id} with no callback. Marking FAILED.")
                                job.status = "FAILED"
                                job.error = "No callback attached to job."
                    except asyncio.CancelledError:
                        logger.info(f"[QUEUE:WORKER_CANCELLED] Job {job.job_id} cancelled during processing.")
                        job.status = "CANCELLED"
                    except Exception as e:
                        logger.error(f"[QUEUE:WORKER_ERROR] Worker {name} error processing job {job.job_id}: {e}", exc_info=True)
                        job.status = "FAILED"
                        job.error = str(e)
                    finally:
                        if job.status in self.FINAL_STATUSES:
                            await self.release_job(job.job_id, status=job.status, error=job.error)
                finally:
                    self._queue.task_done()
                    logger.debug(f"[QUEUE:TASK_DONE] Worker {name} called task_done() for job {job.job_id}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[WORKER:FATAL_LOOP_ERROR] Worker {name} caught unexpected error in outer loop: {e}", exc_info=True)
                await asyncio.sleep(0.5)

        logger.info(f"[WORKER:STOP] Worker {name} stopped.")
