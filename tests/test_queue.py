import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from app.services.queue_manager import QueueManager, QueuedJob
from app.database.models import init_db, DownloadJob, get_session
from app.database.repository import JobRepository
from app.utils.validators import check_disk_space


@pytest.mark.asyncio
async def test_queue_per_user_limit():
    """Verify per-user concurrent job limits are strictly enforced."""
    config = MagicMock()
    config.MAX_CONCURRENT_DOWNLOADS = 5
    config.MAX_DOWNLOADS_PER_USER = 1

    qm = QueueManager(config)

    with patch("app.database.repository.JobRepository.create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = MagicMock()

        # First job for user 100 succeeds
        job1 = await qm.add_job(
            telegram_id=100,
            url="https://iframe.mediadelivery.net/embed/1/2",
            quality="1080p",
            video_title="Video 1"
        )
        assert job1 is not None

        # Second job for same user should be rejected (exceeds per-user limit of 1)
        job2 = await qm.add_job(
            telegram_id=100,
            url="https://iframe.mediadelivery.net/embed/1/3",
            quality="720p",
            video_title="Video 2"
        )
        assert job2 is None

        # Different user (200) can queue a job
        job3 = await qm.add_job(
            telegram_id=200,
            url="https://iframe.mediadelivery.net/embed/1/4",
            quality="480p",
            video_title="Video 3"
        )
        assert job3 is not None


@pytest.mark.asyncio
async def test_queue_position_and_cancellation():
    """Verify queue position reporting and cancellation propagation."""
    config = MagicMock()
    config.MAX_CONCURRENT_DOWNLOADS = 1
    config.MAX_DOWNLOADS_PER_USER = 5

    qm = QueueManager(config)

    with patch("app.database.repository.JobRepository.create", new_callable=AsyncMock):
        with patch("app.database.repository.JobRepository.cancel_job", new_callable=AsyncMock):
            job1_id = await qm.add_job(
                telegram_id=10, url="http://example.com/1", quality="1080p"
            )
            job2_id = await qm.add_job(
                telegram_id=11, url="http://example.com/2", quality="720p"
            )

            # Positions
            pos1 = qm.get_queue_position(job1_id)
            pos2 = qm.get_queue_position(job2_id)
            assert pos1 == 1
            assert pos2 == 2

            # Cancel job 1
            cancelled = await qm.cancel_job(job1_id)
            assert cancelled is True
            job1 = qm.get_job(job1_id)
            assert job1.status == "CANCELLED"
            assert job1.cancel_event.is_set()


@pytest.mark.asyncio
async def test_queue_slot_released_on_completion_and_failure():
    """Verify concurrency slots are released immediately when a job completes or fails."""
    config = MagicMock()
    config.MAX_CONCURRENT_DOWNLOADS = 2
    config.MAX_DOWNLOADS_PER_USER = 1

    qm = QueueManager(config)

    with patch("app.database.repository.JobRepository.create", new_callable=AsyncMock):
        with patch("app.database.repository.JobRepository.update_status", new_callable=AsyncMock):
            # User 50 submits job 1
            job1_id = await qm.add_job(telegram_id=50, url="http://example.com/1", quality="720p")
            assert job1_id is not None

            # User 50 tries to submit job 2 -> rejected because job 1 is active
            job2_rejected = await qm.add_job(telegram_id=50, url="http://example.com/2", quality="720p")
            assert job2_rejected is None

            # Job 1 completes -> releases slot
            await qm.release_job(job1_id, status="COMPLETED")

            # Now User 50 can submit job 3 -> succeeds!
            job3_id = await qm.add_job(telegram_id=50, url="http://example.com/3", quality="1080p")
            assert job3_id is not None

            # Job 3 fails -> releases slot
            await qm.update_job_status(job3_id, "FAILED", error="Network error")

            # Now User 50 can submit job 4 -> succeeds without false 'Queue Full'
            job4_id = await qm.add_job(telegram_id=50, url="http://example.com/4", quality="480p")
            assert job4_id is not None


@pytest.mark.asyncio
async def test_cancellation_releases_slot_and_cancels_engine():
    """Verify cancelling an active job aborts the underlying engine and immediately frees the slot."""
    config = MagicMock()
    config.MAX_CONCURRENT_DOWNLOADS = 2
    config.MAX_DOWNLOADS_PER_USER = 1

    qm = QueueManager(config)

    with patch("app.database.repository.JobRepository.create", new_callable=AsyncMock):
        with patch("app.database.repository.JobRepository.update_status", new_callable=AsyncMock):
            with patch("app.database.repository.JobRepository.cancel_job", new_callable=AsyncMock):
                job_id = await qm.add_job(telegram_id=70, url="http://example.com/1", quality="720p")
                assert job_id is not None

                job = qm.get_job(job_id)
                mock_engine = MagicMock()
                job.engine = mock_engine

                # Cancel the job
                cancelled = await qm.cancel_job(job_id)
                assert cancelled is True
                mock_engine.cancel.assert_called_once()
                assert job.status == "CANCELLED"

                # Concurrency slot is immediately freed
                new_job_id = await qm.add_job(telegram_id=70, url="http://example.com/2", quality="1080p")
                assert new_job_id is not None


@pytest.mark.asyncio
async def test_orphaned_queue_entry_detection_and_reconciliation():
    """Verify that an in-memory job missing or marked FAILED in the DB is detected as an orphan and released."""
    config = MagicMock()
    config.MAX_CONCURRENT_DOWNLOADS = 2
    config.MAX_DOWNLOADS_PER_USER = 1

    qm = QueueManager(config)

    # Manually insert an orphaned job into memory with status QUEUED
    orphan_job = QueuedJob(
        job_id="orphan-404",
        telegram_id=555,
        url="http://example.com/orphan",
        quality="720p",
        status="QUEUED"
    )
    qm._active_jobs["orphan-404"] = orphan_job

    assert qm.queued_count == 1

    # In database, this job does not exist or has status FAILED
    with patch("app.database.repository.JobRepository.reconcile_stale_jobs", new_callable=AsyncMock) as mock_rec_db:
        mock_rec_db.return_value = 0
        with patch("app.database.repository.JobRepository.get_all_active_jobs", new_callable=AsyncMock) as mock_active_db:
            mock_active_db.return_value = []
            with patch("app.database.repository.JobRepository.get_by_id", new_callable=AsyncMock) as mock_get_by_id:
                mock_get_by_id.return_value = None  # Missing from DB

                res = await qm.reconcile_stale_jobs()
                assert res.orphaned_memory_jobs == 1
                assert qm.queued_count == 0
                assert "orphan-404" not in qm._active_jobs


@pytest.mark.asyncio
async def test_database_failure_reconciled_safely_on_add_job():
    """Verify database write failure during add_job leaves no phantom in-memory job."""
    config = MagicMock()
    config.MAX_CONCURRENT_DOWNLOADS = 2
    config.MAX_DOWNLOADS_PER_USER = 1

    qm = QueueManager(config)

    with patch("app.database.repository.JobRepository.create", side_effect=Exception("Database write error")):
        job_id = await qm.add_job(telegram_id=888, url="http://example.com/dbfail", quality="720p")
        assert job_id is None
        assert qm.queued_count == 0
        assert qm.queue_size == 0
        assert len(qm._active_jobs) == 0


@pytest.mark.asyncio
async def test_duplicate_queue_submission_rejected():
    """Verify submitting the same URL for the same user while active is rejected."""
    config = MagicMock()
    config.MAX_CONCURRENT_DOWNLOADS = 5
    config.MAX_DOWNLOADS_PER_USER = 5

    qm = QueueManager(config)

    with patch("app.database.repository.JobRepository.create", new_callable=AsyncMock):
        job1 = await qm.add_job(telegram_id=300, url="http://example.com/same", quality="720p")
        assert job1 is not None

        # Duplicate submission with identical URL
        job2 = await qm.add_job(telegram_id=300, url="http://example.com/same", quality="1080p")
        assert job2 is None


@pytest.mark.asyncio
async def test_worker_task_done_and_crash_supervision():
    """Verify task_done() is called and worker supervision restarts dead worker tasks."""
    config = MagicMock()
    config.MAX_CONCURRENT_DOWNLOADS = 2
    config.MAX_DOWNLOADS_PER_USER = 2

    qm = QueueManager(config)
    await qm.start()

    executed = asyncio.Event()

    async def sample_callback(job):
        job.status = "COMPLETED"
        executed.set()

    with patch("app.database.repository.JobRepository.create", new_callable=AsyncMock):
        with patch("app.database.repository.JobRepository.update_status", new_callable=AsyncMock):
            job_id = await qm.add_job(telegram_id=400, url="http://example.com/work", quality="720p", callback=sample_callback)
            assert job_id is not None

            # Wait for execution to finish
            await asyncio.wait_for(executed.wait(), timeout=5.0)
            await asyncio.sleep(0.1)

            assert qm.queue_size == 0
            assert qm._queue._unfinished_tasks == 0

            # Simulate worker death by cancelling worker-0
            qm._workers[0].cancel()
            await asyncio.sleep(0.05)
            assert qm.alive_workers_count == 1

            # Watchdog restarts dead worker
            restarted = qm.ensure_workers_alive()
            assert restarted == 1
            assert qm.alive_workers_count == 2

    await qm.stop()


@pytest.mark.asyncio
async def test_worker_marks_job_failed_if_no_callback():
    """Verify worker does not leave a phantom queued job if dequeued with no callback."""
    config = MagicMock()
    config.MAX_CONCURRENT_DOWNLOADS = 1
    config.MAX_DOWNLOADS_PER_USER = 1

    qm = QueueManager(config)
    await qm.start()

    with patch("app.database.repository.JobRepository.create", new_callable=AsyncMock):
        with patch("app.database.repository.JobRepository.update_status", new_callable=AsyncMock):
            # Enqueue job without callback
            job_id = await qm.add_job(telegram_id=450, url="http://example.com/nocallback", quality="720p", callback=None)
            assert job_id is not None

            # Allow worker loop to process item
            await asyncio.sleep(0.2)

            # Job must be marked FAILED and released from active queue
            assert qm.queued_count == 0
            job = qm.get_job(job_id)
            assert job is not None
            assert job.status == "FAILED"
            assert "no callback" in job.error.lower()

    await qm.stop()


@pytest.mark.asyncio
async def test_restart_stale_job_recovery_unit():
    """Verify reconcile_stale_jobs reconciles uncompleted jobs from database."""
    config = MagicMock()
    config.MAX_CONCURRENT_DOWNLOADS = 2
    config.MAX_DOWNLOADS_PER_USER = 1

    qm = QueueManager(config)

    with patch("app.database.repository.JobRepository.reconcile_stale_jobs", new_callable=AsyncMock) as mock_reconcile:
        mock_reconcile.return_value = 3
        with patch("app.database.repository.JobRepository.get_all_active_jobs", new_callable=AsyncMock) as mock_get_active:
            mock_get_active.return_value = []
            count = await qm.reconcile_stale_jobs()
            assert count == 3
            mock_reconcile.assert_called_once()


@pytest.mark.asyncio
async def test_database_restart_reconciliation_integration(tmp_path):
    """Verify database reconciliation marks interrupted jobs as FAILED and frees get_active_jobs."""
    db_file = tmp_path / "test_restart.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    await init_db(db_url)

    # Insert an interrupted job (e.g. was DOWNLOADING when process died)
    async with get_session() as session:
        job = DownloadJob(
            job_id="stale-job-123",
            telegram_id=999,
            url="http://example.com/test",
            status="DOWNLOADING",
        )
        session.add(job)
        await session.commit()

    # Verify get_active_jobs finds the job
    active = await JobRepository.get_active_jobs(999)
    assert len(active) == 1

    # Simulate bot startup recovery
    reconciled_count = await JobRepository.reconcile_stale_jobs()
    assert reconciled_count == 1

    # Active jobs count must now be 0 for this user
    active_after = await JobRepository.get_active_jobs(999)
    assert len(active_after) == 0

    # Job status must be FAILED with restart message
    stale_record = await JobRepository.get_by_id("stale-job-123")
    assert stale_record.status == "FAILED"
    assert "bot restart" in stale_record.error_message.lower()


def test_disk_space_exhaustion(tmp_path):
    """Verify check_disk_space correctly identifies low storage space."""
    # Real disk check on temp path with reasonable threshold
    ok, msg = check_disk_space(str(tmp_path), required_mb=1)
    assert ok is True

    # Demanding unrealistic disk space (e.g., 500,000,000 MB = 500 TB) should fail
    ok, msg = check_disk_space(str(tmp_path), required_mb=500_000_000)
    assert ok is False
    assert "insufficient disk space" in msg.lower()
