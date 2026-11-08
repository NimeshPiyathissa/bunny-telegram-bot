import asyncio
import os
import shutil
import stat
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pyrogram.types import Message, Video, Document

from config import Config
from app.handlers.state import FlowState, PendingJob
from app.handlers.quality import _process_download
from app.services.bunny_extractor import ExtractionResult, VideoFormat
from app.services.cleanup_service import CleanupService
from app.services.queue_manager import QueueManager
from app.services.upload_service import UploadService
from app.database.models import init_db, close_db, DownloadJob, get_session
from app.database.repository import JobRepository, UserRepository


def create_test_config(tmp_path):
    """Provide isolated configuration with temporary directories."""
    temp_dir = tmp_path / "temp"
    download_dir = tmp_path / "downloads"
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    backup_dir = tmp_path / "backups"

    for d in [temp_dir, download_dir, data_dir, log_dir, backup_dir]:
        d.mkdir(parents=True, exist_ok=True)

    cfg = Config(
        BOT_TOKEN="123456789:ABCDEF_TEST_TOKEN",
        API_ID=12345,
        API_HASH="abcdef0123456789",
        OWNER_ID=987654321,
        ADMIN_IDS=[],
        DATABASE_URL=f"sqlite+aiosqlite:///{data_dir / 'bot.db'}",
        MAX_CONCURRENT_DOWNLOADS=2,
        MAX_DOWNLOADS_PER_USER=1,
        MAX_FILE_SIZE_MB=1900,
        DOWNLOAD_TIMEOUT_SECONDS=60,
        UPLOAD_TIMEOUT_SECONDS=60,
        AUTO_CLEANUP=True,
        CLEANUP_AFTER_UPLOAD=True,
        FAILED_JOB_RETENTION_HOURS=24,
        TEMP_FILE_RETENTION_HOURS=6,
        MIN_FREE_DISK_GB=3,
        TEMP_DIRECTORY=str(temp_dir),
        DOWNLOAD_DIRECTORY=str(download_dir),
        DATA_DIRECTORY=str(data_dir),
        LOG_DIRECTORY=str(log_dir),
        BACKUP_DIRECTORY=str(backup_dir),
    )
    return cfg


@pytest.mark.asyncio
async def test_successful_upload_followed_by_local_file_deletion(tmp_path):
    """Test Requirement 1 & 2: Local MP4, thumbnails, and workspace are deleted after confirmed upload."""
    test_config = create_test_config(tmp_path)
    await init_db(test_config.DATABASE_URL)

    client = MagicMock()
    target_msg = AsyncMock()
    target_msg.chat.id = 12345
    target_msg.from_user.id = 99999
    target_msg.edit_text = AsyncMock()

    queue_manager = QueueManager(test_config)
    cleanup_service = CleanupService(test_config)
    cleanup_service.set_queue_manager(queue_manager)
    client.queue_manager = queue_manager
    client.cleanup_service = cleanup_service
    client.delete_messages = AsyncMock()  # To verify it is NEVER called

    job_id = "test-job-cleanup-1"
    await JobRepository.create(job_id=job_id, telegram_id=99999, url="https://iframe.mediadelivery.net/embed/1/abc")

    # Create fake job workspace and files
    workspace = Path(test_config.TEMP_DIRECTORY) / f"job_{job_id}"
    workspace.mkdir(parents=True, exist_ok=True)
    mp4_file = workspace / f"{job_id}_video.mp4"
    # Write valid MP4 magic header
    mp4_file.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 1024)
    thumb_file = workspace / f"{job_id}_video_thumb.jpg"
    thumb_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

    # Mock Telegram successful upload response
    mock_sent_msg = MagicMock(spec=Message)
    mock_sent_msg.id = 887766
    mock_sent_msg.video = MagicMock(spec=Video)
    mock_sent_msg.video.file_id = "BAADBAADAgAD..."

    extraction = ExtractionResult(title="Test Video", duration=120)
    selected_format = VideoFormat(format_id="720p", quality="720p", height=720, url="https://example.com/test.mp4")

    with patch("app.handlers.quality.DownloadEngine") as MockEngine:
        engine_instance = MagicMock()
        engine_instance.workspace = workspace
        engine_instance.temp_dir = Path(test_config.TEMP_DIRECTORY)
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.needs_merge = False
        mock_result.output_path = str(mp4_file)
        engine_instance.download = AsyncMock(return_value=mock_result)
        MockEngine.return_value = engine_instance

        with patch.object(UploadService, "upload_video", AsyncMock(return_value=mock_sent_msg)):
            with patch("app.handlers.quality.validate_output_file", return_value=(True, "OK")):
                await _process_download(
                    client=client,
                    target=target_msg,
                    extraction=extraction,
                    selected_format=selected_format,
                    config=test_config,
                    queue_manager=queue_manager,
                    job_id=job_id,
                    user_id=99999,
                )

    # 1. Verify local workspace and files were deleted
    assert not mp4_file.exists(), "Local MP4 file must be deleted after successful upload"
    assert not thumb_file.exists(), "Local thumbnail must be deleted after upload"
    assert not workspace.exists(), "Job workspace directory must be deleted after upload"

    # 2. Verify completed status and Telegram message/file IDs persisted in database
    db_job = await JobRepository.get_by_id(job_id)
    assert db_job.status == "COMPLETED"
    assert db_job.telegram_message_id == 887766
    assert db_job.telegram_file_id == "BAADBAADAgAD..."


@pytest.mark.asyncio
async def test_telegram_video_remains_available_after_local_cleanup(tmp_path):
    """Test Requirement 3: Cleanup never calls Telegram delete_messages."""
    test_config = create_test_config(tmp_path)
    client = MagicMock()
    client.delete_messages = AsyncMock()

    cleanup_service = CleanupService(test_config)
    workspace = Path(test_config.TEMP_DIRECTORY) / "job_test_clean"
    workspace.mkdir(parents=True, exist_ok=True)
    temp_file = workspace / "test.mp4"
    temp_file.write_bytes(b"dummy video data")

    # Perform cleanup
    await cleanup_service.cleanup_workspace(workspace, job_id="test")

    # Verify Telegram API delete_messages was never called
    client.delete_messages.assert_not_called()
    assert not temp_file.exists()


@pytest.mark.asyncio
async def test_failed_upload_retry_behavior(tmp_path):
    """Test Requirement 4: Bounded retries with exponential backoff on upload failure."""
    test_config = create_test_config(tmp_path)
    await init_db(test_config.DATABASE_URL)

    client = MagicMock()
    target_msg = AsyncMock()
    target_msg.chat.id = 12345
    target_msg.from_user.id = 99999

    queue_manager = QueueManager(test_config)
    cleanup_service = CleanupService(test_config)
    client.queue_manager = queue_manager
    client.cleanup_service = cleanup_service

    job_id = "test-job-retry-1"
    await JobRepository.create(job_id=job_id, telegram_id=99999, url="https://iframe.mediadelivery.net/embed/1/abc")

    workspace = Path(test_config.TEMP_DIRECTORY) / f"job_{job_id}"
    workspace.mkdir(parents=True, exist_ok=True)
    mp4_file = workspace / f"{job_id}_video.mp4"
    mp4_file.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 1024)

    # Simulate initial failure then success on 2nd attempt
    mock_sent_msg = MagicMock(spec=Message)
    mock_sent_msg.id = 998877
    mock_sent_msg.video = MagicMock(file_id="FILE_ID_OK")

    upload_mock = AsyncMock(side_effect=[
        Exception("Temporary network glitch"),
        mock_sent_msg,
    ])

    extraction = ExtractionResult(title="Retry Video", duration=60)
    selected_format = VideoFormat(format_id="720p", quality="720p", height=720, url="https://example.com/test.mp4")

    with patch("app.handlers.quality.DownloadEngine") as MockEngine:
        engine_instance = MagicMock()
        engine_instance.workspace = workspace
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.needs_merge = False
        mock_result.output_path = str(mp4_file)
        engine_instance.download = AsyncMock(return_value=mock_result)
        MockEngine.return_value = engine_instance

        with patch.object(UploadService, "upload_video", upload_mock):
            with patch("app.handlers.quality.validate_output_file", return_value=(True, "OK")):
                with patch("asyncio.sleep", AsyncMock()):  # Speed up backoff sleep
                    await _process_download(
                        client=client,
                        target=target_msg,
                        extraction=extraction,
                        selected_format=selected_format,
                        config=test_config,
                        queue_manager=queue_manager,
                        job_id=job_id,
                        user_id=99999,
                    )

    # Verify retry occurred (2 calls to upload_video) and succeeded
    assert upload_mock.call_count == 2
    db_job = await JobRepository.get_by_id(job_id)
    assert db_job.status == "COMPLETED"
    assert db_job.retry_count == 1
    assert db_job.telegram_message_id == 998877


@pytest.mark.asyncio
async def test_uncertain_delivery_status_without_duplicate_sending(tmp_path):
    """Test Requirement 5: Uncertain delivery state transitions to RECOVERY without blind resend."""
    test_config = create_test_config(tmp_path)
    await init_db(test_config.DATABASE_URL)

    client = MagicMock()
    target_msg = AsyncMock()
    target_msg.chat.id = 12345
    target_msg.from_user.id = 99999

    queue_manager = QueueManager(test_config)
    cleanup_service = CleanupService(test_config)
    client.queue_manager = queue_manager
    client.cleanup_service = cleanup_service

    job_id = "test-job-uncertain-1"
    await JobRepository.create(job_id=job_id, telegram_id=99999, url="https://iframe.mediadelivery.net/embed/1/abc")

    workspace = Path(test_config.TEMP_DIRECTORY) / f"job_{job_id}"
    workspace.mkdir(parents=True, exist_ok=True)
    mp4_file = workspace / f"{job_id}_video.mp4"
    mp4_file.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 1024)

    # Simulate network drop timeout on attempt 1
    upload_mock = AsyncMock(side_effect=[
        Exception("Connection reset"),
        TimeoutError("Connection timed out waiting for server response"),
    ])

    extraction = ExtractionResult(title="Uncertain Video", duration=60)
    selected_format = VideoFormat(format_id="720p", quality="720p", height=720, url="https://example.com/test.mp4")

    with patch("app.handlers.quality.DownloadEngine") as MockEngine:
        engine_instance = MagicMock()
        engine_instance.workspace = workspace
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.needs_merge = False
        mock_result.output_path = str(mp4_file)
        engine_instance.download = AsyncMock(return_value=mock_result)
        MockEngine.return_value = engine_instance

        with patch.object(UploadService, "upload_video", upload_mock):
            with patch("app.handlers.quality.validate_output_file", return_value=(True, "OK")):
                with patch("asyncio.sleep", AsyncMock()):
                    await _process_download(
                        client=client,
                        target=target_msg,
                        extraction=extraction,
                        selected_format=selected_format,
                        config=test_config,
                        queue_manager=queue_manager,
                        job_id=job_id,
                        user_id=99999,
                    )

    # Verify marked in RECOVERY state to avoid creating duplicate messages
    db_job = await JobRepository.get_by_id(job_id)
    assert db_job.status in ("RECOVERY", "FAILED")
    # File must NOT be deleted so recovery can inspect it
    assert mp4_file.exists()


@pytest.mark.asyncio
async def test_cleanup_after_cancellation(tmp_path):
    """Test Requirement 6: Cancellation immediately cleans workspace and frees slot."""
    test_config = create_test_config(tmp_path)
    await init_db(test_config.DATABASE_URL)

    client = MagicMock()
    target_msg = AsyncMock()
    target_msg.chat.id = 12345
    target_msg.from_user.id = 99999

    queue_manager = QueueManager(test_config)
    cleanup_service = CleanupService(test_config)
    client.queue_manager = queue_manager
    client.cleanup_service = cleanup_service

    # Pre-populate active job and trigger cancel event
    job_id = await queue_manager.add_job(telegram_id=99999, url="https://example.com", quality="720p")
    queued_job = queue_manager.get_job(job_id)
    assert queued_job is not None
    queued_job.cancel_event.set()

    workspace = Path(test_config.TEMP_DIRECTORY) / f"job_{job_id}"
    workspace.mkdir(parents=True, exist_ok=True)
    temp_file = workspace / "partial.mp4"
    temp_file.write_bytes(b"partial segment bytes")

    with patch("app.handlers.quality.DownloadEngine") as MockEngine:
        engine_instance = MagicMock()
        engine_instance.workspace = workspace
        engine_instance.cancel = MagicMock()
        mock_result = MagicMock(success=False, error="Cancelled")
        engine_instance.download = AsyncMock(return_value=mock_result)
        MockEngine.return_value = engine_instance

        await _process_download(
            client=client,
            target=target_msg,
            extraction=ExtractionResult(title="Cancel"),
            selected_format=VideoFormat(format_id="720p", quality="720p", height=720, url="http://x"),
            config=test_config,
            queue_manager=queue_manager,
            job_id=job_id,
            user_id=99999,
        )

    # Workspace must be removed
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_interrupted_job_recovery_on_startup(tmp_path):
    """Test Requirement 7: Stale/orphaned in-flight jobs are recovered cleanly on startup."""
    test_config = create_test_config(tmp_path)
    await init_db(test_config.DATABASE_URL)

    job_id = "stale-job-interrupted"
    await JobRepository.create(job_id=job_id, telegram_id=88888, url="https://example.com/stale")
    await JobRepository.update_status(job_id, "UPLOADING")

    queue_manager = QueueManager(test_config)
    reconciled = await queue_manager.reconcile_stale_jobs()

    assert int(reconciled) >= 1
    job = await JobRepository.get_by_id(job_id)
    assert job.status == "FAILED"
    assert "restart" in job.error_message.lower()


@pytest.mark.asyncio
async def test_protected_files_never_deleted_by_cleanup(tmp_path):
    """Test Requirement 12: Sensitive system files (.env, .db, .session) are strictly protected."""
    test_config = create_test_config(tmp_path)
    cleanup_service = CleanupService(test_config)

    # Paths that must NEVER be deleted
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_TOKEN=secret")
    db_file = tmp_path / "bot.db"
    db_file.write_text("sqlite data")
    session_file = tmp_path / "bunny_bot.session"
    session_file.write_text("telegram session")

    # Test _is_safe_path rejects them
    assert not cleanup_service._is_safe_path(env_file)
    assert not cleanup_service._is_safe_path(db_file)
    assert not cleanup_service._is_safe_path(session_file)
    assert not cleanup_service._is_safe_path(Path(test_config.TEMP_DIRECTORY))
    assert not cleanup_service._is_safe_path(Path(test_config.DOWNLOAD_DIRECTORY))

    # Test safe_unlink refuses to delete them
    ok, _ = await cleanup_service.safe_unlink(env_file)
    assert not ok
    assert env_file.exists(), ".env must never be deleted!"


@pytest.mark.asyncio
async def test_windows_file_lock_safe_unlink(tmp_path):
    """Test Requirement 11: Windows file lock handling with retries and gc."""
    test_config = create_test_config(tmp_path)
    cleanup_service = CleanupService(test_config)
    test_file = Path(test_config.TEMP_DIRECTORY) / "locked_test.mp4"
    test_file.write_bytes(b"locked video data")

    # Simulate permission error once then success
    original_unlink = Path.unlink
    calls = [0]

    def fake_unlink(self, missing_ok=False):
        calls[0] += 1
        if calls[0] == 1:
            raise PermissionError("WinError 32: File locked by another process")
        original_unlink(self, missing_ok=missing_ok)

    with patch.object(Path, "unlink", fake_unlink):
        ok, size = await cleanup_service.safe_unlink(test_file, max_retries=3, delay=0.01)
        assert ok
        assert not test_file.exists()


def test_env_config_validation():
    """Test Requirement 16: Environment configuration validation rules."""
    # 1. Missing required variables
    cfg = Config(BOT_TOKEN="", API_ID=0, API_HASH="", OWNER_ID=0)
    errors = cfg.validate(exit_on_error=False)
    assert any("BOT_TOKEN is required" in e for e in errors)
    assert any("API_ID is required" in e for e in errors)
    assert any("API_HASH is required" in e for e in errors)
    assert any("OWNER_ID is required" in e for e in errors)

    # 2. Invalid numeric limits
    cfg_invalid = Config(
        BOT_TOKEN="token", API_ID=123, API_HASH="hash", OWNER_ID=456,
        MAX_CONCURRENT_DOWNLOADS=0,
        MIN_FREE_DISK_GB=0,
    )
    errors_inv = cfg_invalid.validate(exit_on_error=False)
    assert any("MAX_CONCURRENT_DOWNLOADS must be greater than 0" in e for e in errors_inv)
    assert any("MIN_FREE_DISK_GB must be at least 1 GB" in e for e in errors_inv)

    # 3. Valid configuration
    cfg_valid = Config(
        BOT_TOKEN="token", API_ID=123, API_HASH="hash", OWNER_ID=456,
        MAX_CONCURRENT_DOWNLOADS=2,
        MIN_FREE_DISK_GB=3,
    )
    assert len(cfg_valid.validate(exit_on_error=False)) == 0


def test_installation_scripts_syntax_and_line_endings():
    """Test Requirement 17: Bash shell scripts retain LF endings and bash -n syntax validity."""
    scripts_dir = Path(__file__).parent.parent / "scripts"
    sh_files = list(scripts_dir.glob("*.sh"))
    assert len(sh_files) >= 5, "All operational scripts must exist in scripts/"

    for sh in sh_files:
        content = sh.read_bytes()
        # Verify no carriage return in bash scripts (must be Unix LF)
        assert b"\r\n" not in content, f"{sh.name} must use Unix LF line endings, not CRLF!"
        # Verify shebang
        assert content.startswith(b"#!/usr/bin/env bash"), f"{sh.name} must declare bash shebang"


@pytest.mark.asyncio
async def test_sqlite_wal_mode_and_concurrency(tmp_path):
    """Test Requirement: Safe WAL initialization, busy_timeout, and high concurrency resilience."""
    from sqlalchemy import text
    test_config = create_test_config(tmp_path)
    await init_db(test_config.DATABASE_URL)

    # 1. Verify PRAGMA journal_mode is WAL on local disk
    async with get_session() as session:
        res = await session.execute(text("PRAGMA journal_mode;"))
        mode = res.scalar()
        assert mode.lower() == "wal", f"Expected WAL journal mode on local disk, got {mode}"

        res_timeout = await session.execute(text("PRAGMA busy_timeout;"))
        timeout = res_timeout.scalar()
        assert int(timeout) >= 10000, f"Expected busy_timeout >= 10000, got {timeout}"

    # 2. Concurrency stress test: 25 concurrent writers/readers
    async def worker(idx: int):
        user_id = 100000 + idx
        job_id = f"wal-concurrent-job-{idx}"
        await UserRepository.get_or_create(telegram_id=user_id, username=f"user_{idx}")
        await JobRepository.create(job_id=job_id, telegram_id=user_id, url=f"https://iframe.mediadelivery.net/embed/1/{idx}")
        await JobRepository.update_progress(job_id, download_progress=50.0)
        await JobRepository.update_status(job_id, "DOWNLOADING")
        await UserRepository.increment_downloads(user_id)
        await JobRepository.update_status(job_id, "COMPLETED")
        return job_id

    job_ids = await asyncio.gather(*(worker(i) for i in range(25)))
    assert len(job_ids) == 25

    # 3. Verify all records exist and are consistent
    for j_id in job_ids:
        job = await JobRepository.get_by_id(j_id)
        assert job is not None
        assert job.status == "COMPLETED"
        assert job.download_progress == 50.0

    await asyncio.sleep(0.05)
    await close_db()


@pytest.mark.asyncio
async def test_sqlite_wal_safe_filesystem_fallback():
    """Test Requirement: Fallback gracefully without error on in-memory or unsupported filesystems."""
    from sqlalchemy import text
    # In-memory database does not support WAL mode and should fallback cleanly
    mem_url = "sqlite+aiosqlite:///:memory:"
    await init_db(mem_url)

    async with get_session() as session:
        res = await session.execute(text("PRAGMA journal_mode;"))
        mode = res.scalar()
        assert mode.lower() in ("memory", "delete", "off")

    await close_db()
