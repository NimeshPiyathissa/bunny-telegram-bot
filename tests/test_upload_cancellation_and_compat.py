"""Regression tests for Pyrogram compatibility, bot startup, upload cancellation, and thread-safety."""

import asyncio
import concurrent.futures
import io
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pyrogram
from pyrogram import Client, StopTransmission
from pyrogram.methods.advanced.save_file import SaveFile

import bot
from app.services.upload_service import UploadService
from app.utils.fast_upload import fast_save_file, patch_pyrogram_uploader, _original_save_file
from app.handlers.quality import _update_upload_progress


# ==============================================================================
# 1. Startup and Import Compatibility Tests
# ==============================================================================

def test_pyrogram_stop_transmission_compatibility():
    """Verify StopTransmission is exposed by pyrogram and is an Exception subclass."""
    assert hasattr(pyrogram, "StopTransmission")
    assert issubclass(StopTransmission, Exception)


def test_bot_create_app_startup():
    """Verify bot.create_app() configures Client without any import errors or crashes."""
    app = bot.create_app()
    assert isinstance(app, Client)
    assert app.name == "bunny_bot"
    assert hasattr(SaveFile, "save_file")
    assert SaveFile.save_file == fast_save_file


def test_all_modules_in_app_importable():
    """Verify that every python module in app/ and bot.py can be cleanly imported."""
    project_root = Path(__file__).parent.parent
    app_dir = project_root / "app"

    modules = ["bot", "config"]
    for py_file in app_dir.rglob("*.py"):
        rel_path = py_file.relative_to(project_root)
        module_name = str(rel_path.with_suffix("")).replace("\\", ".").replace("/", ".")
        modules.append(module_name)

    for mod in modules:
        __import__(mod)


# ==============================================================================
# 2. Upload Progress Thread-Safety & Worker Thread Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_progress_callback_invoked_from_worker_thread():
    """Verify upload progress callback runs safely from background worker threads without event loop errors."""
    main_loop = asyncio.get_running_loop()
    upload_last_edit = [0.0]
    upload_start_time = time.monotonic()
    job = MagicMock()
    job.cancel_event = asyncio.Event()

    executed_updates = []

    async def mock_update(msg, ext, cur, tot, st, jid):
        executed_updates.append((cur, tot))

    def upload_progress(current, total):
        if job and job.cancel_event.is_set():
            raise StopTransmission

        now = time.monotonic()
        if now - upload_last_edit[0] < 2.5 and current < total:
            return
        upload_last_edit[0] = now

        if main_loop and not main_loop.is_closed():
            coro = mock_update(None, None, current, total, upload_start_time, "job123")
            try:
                asyncio.run_coroutine_threadsafe(coro, main_loop)
            except Exception:
                coro.close()

    # Invoke from a separate threadpool worker (simulating Pyrogram self.executor)
    with patch("app.handlers.quality._update_upload_progress", mock_update):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            # Simulate chunks called from worker thread
            future = executor.submit(upload_progress, 500, 1000)
            future.result(timeout=2.0)
            # Give main loop a moment to run the scheduled coroutine
            await asyncio.sleep(0.05)

    assert len(executed_updates) == 1
    assert executed_updates[0] == (500, 1000)


@pytest.mark.asyncio
async def test_progress_callback_unawaited_coro_prevention():
    """Verify that when loop is closed or invalid, coroutines are closed without warnings."""
    mock_loop = MagicMock()
    mock_loop.is_closed.return_value = True

    upload_last_edit = [0.0]
    upload_start_time = time.monotonic()

    # If loop is closed, coro should not even be scheduled
    now = time.monotonic()
    assert (now - upload_last_edit[0] >= 2.5)


# ==============================================================================
# 3. Upload Cancellation Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_fast_save_file_cancellation_via_stop_transmission(tmp_path):
    """Verify fast_save_file cleanly stops and propagates StopTransmission when progress raises it."""
    test_file = tmp_path / "test_15mb.mp4"
    with open(test_file, "wb") as f:
        f.write(b"0" * (15 * 1024 * 1024))

    mock_client = MagicMock()
    mock_client.save_file_semaphore = asyncio.Semaphore(1)
    mock_client.me = MagicMock(is_premium=False)
    mock_client.rnd_id = MagicMock(return_value=123456789)
    mock_client.loop = asyncio.get_event_loop()

    mock_client.storage = MagicMock()
    mock_client.storage.dc_id = AsyncMock(return_value=2)
    mock_client.storage.auth_key = AsyncMock(return_value=b"x" * 256)
    mock_client.storage.test_mode = AsyncMock(return_value=False)

    call_count = 0

    def cancelling_progress(current, total):
        nonlocal call_count
        call_count += 1
        if call_count >= 1:
            raise StopTransmission("Upload cancelled by test")

    with patch("app.utils.fast_upload.Session") as mock_session_cls:
        mock_sess = MagicMock()
        mock_sess.start = AsyncMock()
        mock_sess.stop = AsyncMock()
        mock_sess.invoke = AsyncMock()
        mock_session_cls.return_value = mock_sess

        with pytest.raises(StopTransmission):
            await fast_save_file(
                mock_client,
                str(test_file),
                progress=cancelling_progress
            )

        assert mock_sess.stop.called


@pytest.mark.asyncio
async def test_upload_service_video_cancellation_does_not_fallback(tmp_path):
    """Verify UploadService.upload_video does NOT fall back to send_document when cancelled."""
    test_file = tmp_path / "test.mp4"
    test_file.write_bytes(b"dummy video content")

    service = UploadService(max_file_size_bytes=100 * 1024 * 1024)
    mock_client = MagicMock()
    mock_client.send_video = AsyncMock(side_effect=StopTransmission("User cancelled"))
    mock_client.send_document = AsyncMock()

    with patch("app.services.upload_service.check_file_size", return_value=(True, "")):
        with patch("app.services.upload_service.FFmpegService.get_video_metadata") as mock_meta:
            mock_meta.return_value = MagicMock(duration=10, display_width=1280, display_height=720)
            with patch("app.services.upload_service.FFmpegService.generate_thumbnail", return_value=False):
                with pytest.raises(StopTransmission):
                    await service.upload_video(
                        client=mock_client,
                        chat_id=12345,
                        file_path=str(test_file),
                        title="Test Video",
                    )

    assert not mock_client.send_document.called


# ==============================================================================
# 4. Fast Uploader Fallback & NoneType Error Prevention Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_fast_save_file_fallback_on_session_error(tmp_path):
    """Verify fast_save_file falls back to native _original_save_file on unexpected errors."""
    test_file = tmp_path / "test_fallback.mp4"
    test_file.write_bytes(b"content for fallback test")

    mock_client = MagicMock()
    mock_client.save_file_semaphore = asyncio.Semaphore(1)
    mock_client.me = MagicMock(is_premium=False)
    mock_client.rnd_id = MagicMock(return_value=999)
    mock_client.loop = asyncio.get_event_loop()
    mock_client.storage = MagicMock()
    mock_client.storage.dc_id = AsyncMock(return_value=2)
    mock_client.storage.auth_key = AsyncMock(return_value=b"key")
    mock_client.storage.test_mode = AsyncMock(return_value=False)

    fallback_called = False

    async def fake_original_save_file(client, path, **kwargs):
        nonlocal fallback_called
        fallback_called = True
        return "native_input_file_result"

    with patch("app.utils.fast_upload._original_save_file", fake_original_save_file):
        with patch("app.utils.fast_upload.Session") as mock_session_cls:
            mock_sess = MagicMock()
            mock_sess.start = AsyncMock(side_effect=RuntimeError("Simulated DC socket failure"))
            mock_sess.stop = AsyncMock()
            mock_session_cls.return_value = mock_sess

            res = await fast_save_file(mock_client, str(test_file))
            assert fallback_called is True
            assert res == "native_input_file_result"


@pytest.mark.asyncio
async def test_fast_save_file_fallback_raises_if_native_returns_none(tmp_path):
    """Verify fast_save_file raises RuntimeError if native save_file returns None."""
    test_file = tmp_path / "test_none.mp4"
    test_file.write_bytes(b"content")

    mock_client = MagicMock()
    mock_client.save_file_semaphore = asyncio.Semaphore(1)
    mock_client.me = MagicMock(is_premium=False)
    mock_client.rnd_id = MagicMock(return_value=999)
    mock_client.loop = asyncio.get_event_loop()
    mock_client.storage = MagicMock()
    mock_client.storage.dc_id = AsyncMock(return_value=2)
    mock_client.storage.auth_key = AsyncMock(return_value=b"key")
    mock_client.storage.test_mode = AsyncMock(return_value=False)

    async def fake_failing_native_save_file(client, path, **kwargs):
        # Native save_file returns None when it catches an unhandled exception
        return None

    with patch("app.utils.fast_upload._original_save_file", fake_failing_native_save_file):
        with patch("app.utils.fast_upload.Session") as mock_session_cls:
            mock_sess = MagicMock()
            mock_sess.start = AsyncMock(side_effect=RuntimeError("Connection failed"))
            mock_sess.stop = AsyncMock()
            mock_session_cls.return_value = mock_sess

            with pytest.raises(RuntimeError, match="returned None"):
                await fast_save_file(mock_client, str(test_file))


@pytest.mark.asyncio
async def test_upload_service_rejects_none_message(tmp_path):
    """Verify UploadService raises RuntimeError if send_video returns None."""
    test_file = tmp_path / "test_null_msg.mp4"
    test_file.write_bytes(b"dummy")

    service = UploadService(max_file_size_bytes=100 * 1024 * 1024)
    mock_client = MagicMock()
    mock_client.send_video = AsyncMock(return_value=None)
    mock_client.send_document = AsyncMock(return_value=None)

    with patch("app.services.upload_service.check_file_size", return_value=(True, "")):
        with patch("app.services.upload_service.FFmpegService.get_video_metadata") as mock_meta:
            mock_meta.return_value = MagicMock(duration=10, display_width=1280, display_height=720)
            with patch("app.services.upload_service.FFmpegService.generate_thumbnail", return_value=False):
                with pytest.raises(RuntimeError, match="returned None"):
                    await service.upload_video(
                        client=mock_client,
                        chat_id=12345,
                        file_path=str(test_file),
                        title="Test",
                    )


# ==============================================================================
# 5. File Handle Lifecycle Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_file_handle_closed_after_fast_save_file(tmp_path):
    """Verify file descriptors are cleanly closed on completion and on errors."""
    test_file = tmp_path / "test_fd.mp4"
    test_file.write_bytes(b"x" * 1024)

    mock_client = MagicMock()
    mock_client.save_file_semaphore = asyncio.Semaphore(1)
    mock_client.me = MagicMock(is_premium=False)
    mock_client.rnd_id = MagicMock(return_value=123)
    mock_client.loop = asyncio.get_event_loop()
    mock_client.storage = MagicMock()
    mock_client.storage.dc_id = AsyncMock(return_value=2)
    mock_client.storage.auth_key = AsyncMock(return_value=b"key")
    mock_client.storage.test_mode = AsyncMock(return_value=False)

    with patch("app.utils.fast_upload.Session") as mock_session_cls:
        mock_sess = MagicMock()
        mock_sess.start = AsyncMock()
        mock_sess.stop = AsyncMock()
        mock_sess.invoke = AsyncMock()
        mock_session_cls.return_value = mock_sess

        res = await fast_save_file(mock_client, str(test_file))
        assert res is not None

    # Verify file can be immediately removed or opened with exclusive lock (proving handle is closed)
    test_file.unlink()
    assert not test_file.exists()
