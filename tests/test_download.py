import asyncio
import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

from app.services.download_engine import DownloadEngine, DownloadResult
from app.services.bunny_extractor import ExtractionResult, VideoFormat
from app.services.ffmpeg_service import FFmpegService
from app.services.upload_service import UploadService
from app.services.cleanup_service import CleanupService
from app.utils.validators import check_file_size, validate_output_file


@pytest.mark.asyncio
async def test_download_cancellation(tmp_path):
    """Test user-triggered cancellation stops download."""
    engine = DownloadEngine(
        download_dir=str(tmp_path / "downloads"),
        temp_dir=str(tmp_path / "temp"),
        timeout=10
    )

    extraction = ExtractionResult(
        success=True,
        title="Test Cancellation Video",
        video_formats=[
            VideoFormat(
                format_id="direct",
                quality="720p",
                height=720,
                url="https://example.com/test.mp4",
                has_audio=True
            )
        ]
    )

    # Cancel before download
    engine.cancel()
    assert engine._cancelled is True


def test_oversized_file_rejection(tmp_path):
    """Ensure files exceeding maximum configured size are rejected."""
    test_file = tmp_path / "large_file.mp4"
    test_file.write_bytes(b"0" * 1024)

    # 1 KB file with 500 byte limit
    ok, msg = check_file_size(str(test_file), max_size_bytes=500)
    assert ok is False
    assert "exceeds maximum" in msg.lower()

    # 1 KB file with 2000 byte limit
    ok, msg = check_file_size(str(test_file), max_size_bytes=2000)
    assert ok is True


def test_validate_output_file_mp4_magic_bytes(tmp_path):
    """Verify output file validation checks existence and ftyp MP4 header."""
    valid_mp4 = tmp_path / "valid.mp4"
    # MP4 file begins with 4 bytes size, then 'ftyp'
    valid_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00")
    ok, msg = validate_output_file(str(valid_mp4))
    assert ok is True

    corrupt_file = tmp_path / "corrupt.mp4"
    corrupt_file.write_bytes(b"INVALID DATA WITH NO HEADER")
    ok, msg = validate_output_file(str(corrupt_file))
    assert ok is False
    assert "valid mp4" in msg.lower()

    empty_file = tmp_path / "empty.mp4"
    empty_file.write_bytes(b"")
    ok, msg = validate_output_file(str(empty_file))
    assert ok is False
    assert "empty" in msg.lower()


@pytest.mark.asyncio
async def test_ffmpeg_merge_success():
    """Verify FFmpegService invokes ffmpeg with stream copy."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"", b"")
        mock_exec.return_value = proc

        with patch("pathlib.Path.exists", return_value=True):
            success, msg = await FFmpegService.merge_video_audio(
                video_path="temp/v.mp4",
                audio_path="temp/a.m4a",
                output_path="temp/out.mp4"
            )
            assert success is True
            assert "successful" in msg.lower()


@pytest.mark.asyncio
async def test_ffmpeg_merge_failure_and_reencode_fallback():
    """Verify fallback to re-encoding when stream copy fails."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        # First call (copy) fails
        proc1 = AsyncMock()
        proc1.returncode = 1
        proc1.communicate.return_value = (b"", b"Invalid codec parameters")

        # Second call (re-encode) succeeds
        proc2 = AsyncMock()
        proc2.returncode = 0
        proc2.communicate.return_value = (b"", b"")

        mock_exec.side_effect = [proc1, proc2]

        with patch("pathlib.Path.exists", return_value=True):
            success, msg = await FFmpegService.merge_video_audio(
                video_path="temp/v.mp4",
                audio_path="temp/a.m4a",
                output_path="temp/out.mp4"
            )
            assert success is True
            assert "re-encoded" in msg.lower()


@pytest.mark.asyncio
async def test_upload_service_video_with_document_fallback(tmp_path):
    """Verify UploadService falls back to document upload when send_video fails."""
    test_video = tmp_path / "sample.mp4"
    test_video.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00" + b"\x00" * 100)

    upload_service = UploadService(max_file_size_bytes=50 * 1024 * 1024)

    mock_client = AsyncMock()
    # send_video raises an exception
    mock_client.send_video.side_effect = Exception("VIDEO_CONTENT_TYPE_INVALID")
    # send_document succeeds
    mock_msg = MagicMock()
    mock_msg.id = 999
    mock_client.send_document.return_value = mock_msg

    with patch.object(FFmpegService, "get_duration", return_value=60):
        with patch.object(FFmpegService, "generate_thumbnail", return_value=False):
            res = await upload_service.upload_video(
                client=mock_client,
                chat_id=123456,
                file_path=str(test_video),
                title="Test Video"
            )
            assert res == mock_msg
            mock_client.send_video.assert_called_once()
            mock_client.send_document.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_service_removes_old_files(tmp_path):
    """Verify CleanupService removes files exceeding age threshold."""
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    old_file = temp_dir / "old_job.mp4"
    old_file.write_bytes(b"data")

    # Set file timestamp to 2 hours ago
    two_hours_ago = 7200
    os.utime(str(old_file), (old_file.stat().st_atime - two_hours_ago, old_file.stat().st_mtime - two_hours_ago))

    config = MagicMock()
    config.TEMP_DIRECTORY = str(temp_dir)
    config.DOWNLOAD_DIRECTORY = str(tmp_path / "downloads")
    config.AUTO_CLEANUP = True

    service = CleanupService(config, max_age_seconds=3600)
    await service.cleanup_old_files()

    assert not old_file.exists()


@pytest.mark.asyncio
async def test_download_hls_includes_referer_headers(tmp_path):
    """Verify _download_hls passes referer and headers arguments before -i to FFmpeg."""
    engine = DownloadEngine(
        download_dir=str(tmp_path / "downloads"),
        temp_dir=str(tmp_path / "temp"),
        timeout=10
    )
    result = DownloadResult()

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"", b"")
        mock_exec.return_value = proc

        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.stat") as mock_stat:
                mock_stat.return_value.st_size = 1024
                res = await engine._download_hls(
                    m3u8_url="https://vz-cdn.net/stream/480p/video.m3u8",
                    safe_title="test_video",
                    job_id="job123",
                    result=result,
                    progress_cb=None,
                    referer="https://iframe.mediadelivery.net/embed/123/guid"
                )

                assert res.success is True
                # Inspect arguments passed to ffmpeg
                cmd_args = mock_exec.call_args[0]
                assert "-referer" in cmd_args
                ref_idx = cmd_args.index("-referer")
                assert cmd_args[ref_idx + 1] == "https://iframe.mediadelivery.net/embed/123/guid"
                assert "-headers" in cmd_args
                assert "-i" in cmd_args
                # Ensure -referer and -headers precede -i
                assert cmd_args.index("-referer") < cmd_args.index("-i")
                assert cmd_args.index("-headers") < cmd_args.index("-i")


@pytest.mark.asyncio
async def test_download_hls_403_forbidden_handling(tmp_path):
    """Verify friendly error message when CDN returns 403 Forbidden."""
    engine = DownloadEngine(
        download_dir=str(tmp_path / "downloads"),
        temp_dir=str(tmp_path / "temp"),
        timeout=10
    )
    result = DownloadResult()

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate.return_value = (b"", b"HTTP error 403 Forbidden\nServer returned 403 Forbidden (access denied)")
        mock_exec.return_value = proc

        res = await engine._download_hls(
            m3u8_url="https://vz-cdn.net/protected/video.m3u8",
            safe_title="protected_video",
            job_id="job456",
            result=result,
            progress_cb=None
        )

        assert res.success is False
        assert "403" in res.error
        assert "authorization" in res.error.lower() or "token" in res.error.lower()


@pytest.mark.asyncio
async def test_upload_service_timeout_handling(tmp_path):
    """Verify UploadService times out gracefully when upload hangs."""
    test_video = tmp_path / "timeout_video.mp4"
    test_video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 50)

    upload_service = UploadService(max_file_size_bytes=50 * 1024 * 1024, timeout=1)

    mock_client = AsyncMock()
    async def slow_send(*args, **kwargs):
        await asyncio.sleep(2)
    mock_client.send_video.side_effect = slow_send
    mock_client.send_document.side_effect = slow_send

    with patch.object(FFmpegService, "get_duration", return_value=10):
        with patch.object(FFmpegService, "generate_thumbnail", return_value=False):
            with pytest.raises(TimeoutError) as exc_info:
                await upload_service.upload_video(
                    client=mock_client,
                    chat_id=123456,
                    file_path=str(test_video),
                    title="Timeout Video"
                )
            assert "timed out" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_upload_service_empty_file_rejected(tmp_path):
    """Verify UploadService rejects 0-byte empty files."""
    empty_video = tmp_path / "empty.mp4"
    empty_video.write_bytes(b"")

    upload_service = UploadService(max_file_size_bytes=50 * 1024 * 1024)
    mock_client = AsyncMock()

    with pytest.raises(ValueError) as exc_info:
        await upload_service.upload_video(
            client=mock_client,
            chat_id=123456,
            file_path=str(empty_video),
            title="Empty Video"
        )
    assert "empty" in str(exc_info.value).lower()


def test_validate_output_file_empty_and_corrupt(tmp_path):
    """Verify validate_output_file rejects missing, empty, and non-MP4 files."""
    # Non-existent
    ok, msg = validate_output_file(str(tmp_path / "nonexistent.mp4"))
    assert ok is False
    assert "not exist" in msg.lower()

    # Empty
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    ok, msg = validate_output_file(str(empty))
    assert ok is False
    assert "empty" in msg.lower()

    # Corrupt / not MP4
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"random text without ftyp header")
    ok, msg = validate_output_file(str(corrupt))
    assert ok is False
    assert "valid mp4" in msg.lower()
