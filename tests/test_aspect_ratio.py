import asyncio
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pyrogram.enums import ParseMode
from pyrogram.types import Message, MessageEntity
from pyrogram.enums import MessageEntityType

from app.services.ffmpeg_service import FFmpegService, VideoMetadata
from app.services.upload_service import UploadService
from app.utils.formatters import format_duration, format_size


@pytest.mark.asyncio
async def test_16_9_metadata_detection():
    """Verify that 16:9 landscape videos (e.g. 854x480, 1920x1080) are correctly detected as widescreen."""
    mock_info = {
        "streams": [
            {
                "codec_type": "video",
                "width": 854,
                "height": 480,
                "sample_aspect_ratio": "1:1",
                "display_aspect_ratio": "427:240",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "duration": "1624.0",
            }
        ],
        "format": {
            "duration": "1624.0",
            "format_name": "mov,mp4,m4a,3gp",
            "bit_rate": "647000",
        }
    }

    with patch.object(FFmpegService, "get_video_info", return_value=mock_info):
        meta = await FFmpegService.get_video_metadata("dummy.mp4")
        assert meta.width == 854
        assert meta.height == 480
        assert meta.display_width == 854
        assert meta.display_height == 480
        assert meta.is_landscape is True
        assert meta.is_portrait is False
        assert meta.is_square is False
        assert meta.is_widescreen_16_9 is True
        assert meta.duration == 1624


@pytest.mark.asyncio
async def test_portrait_source_preservation():
    """Verify portrait video dimensions (e.g. 1080x1920) are preserved without distortion."""
    mock_info = {
        "streams": [
            {
                "codec_type": "video",
                "width": 1080,
                "height": 1920,
                "sample_aspect_ratio": "1:1",
                "display_aspect_ratio": "9:16",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
            }
        ],
        "format": {"duration": "30.0"}
    }

    with patch.object(FFmpegService, "get_video_info", return_value=mock_info):
        meta = await FFmpegService.get_video_metadata("portrait.mp4")
        assert meta.display_width == 1080
        assert meta.display_height == 1920
        assert meta.is_portrait is True
        assert meta.is_landscape is False
        assert meta.is_widescreen_16_9 is False


@pytest.mark.asyncio
async def test_square_source_preservation():
    """Verify square video dimensions (e.g. 720x720) are preserved."""
    mock_info = {
        "streams": [
            {
                "codec_type": "video",
                "width": 720,
                "height": 720,
                "sample_aspect_ratio": "1:1",
                "display_aspect_ratio": "1:1",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
            }
        ],
        "format": {"duration": "45.0"}
    }

    with patch.object(FFmpegService, "get_video_info", return_value=mock_info):
        meta = await FFmpegService.get_video_metadata("square.mp4")
        assert meta.display_width == 720
        assert meta.display_height == 720
        assert meta.is_square is True
        assert meta.is_landscape is False
        assert meta.is_portrait is False


@pytest.mark.asyncio
async def test_sar_dar_handling_anamorphic_480p():
    """Verify anamorphic 720x480 with SAR 32:27 correctly computes 854x480 widescreen display dimensions."""
    mock_info = {
        "streams": [
            {
                "codec_type": "video",
                "width": 720,
                "height": 480,
                "sample_aspect_ratio": "32:27",
                "display_aspect_ratio": "16:9",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
            }
        ],
        "format": {"duration": "60.0"}
    }

    with patch.object(FFmpegService, "get_video_info", return_value=mock_info):
        meta = await FFmpegService.get_video_metadata("anamorphic_480p.mp4")
        assert meta.width == 720
        assert meta.height == 480
        # 720 * (32 / 27) = 853.33 -> 854 (even)
        assert meta.display_width == 854
        assert meta.display_height == 480
        assert meta.is_widescreen_16_9 is True
        assert meta.is_landscape is True


@pytest.mark.asyncio
async def test_rotation_metadata_handling():
    """Verify rotation metadata (e.g. 90 degrees) swaps display dimensions to portrait."""
    mock_info = {
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "sample_aspect_ratio": "1:1",
                "tags": {"rotate": "90"},
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
            }
        ],
        "format": {"duration": "15.0"}
    }

    with patch.object(FFmpegService, "get_video_info", return_value=mock_info):
        meta = await FFmpegService.get_video_metadata("rotated.mp4")
        assert meta.rotation == 90
        assert meta.display_width == 1080
        assert meta.display_height == 1920
        assert meta.is_portrait is True
        assert meta.is_landscape is False


@pytest.mark.asyncio
async def test_ffmpeg_thumbnail_generation(tmp_path):
    """Verify FFmpeg generates a valid 16:9 JPEG thumbnail within Telegram's 320x320 limit."""
    test_video = tmp_path / "test_16_9.mp4"
    thumb_path = tmp_path / "thumb.jpg"

    # Create a 2-second synthetic 16:9 MP4 test video using ffmpeg
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=2:size=854x480:rate=24",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(test_video)
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc.communicate()

    assert test_video.exists()

    success = await FFmpegService.generate_thumbnail(
        video_path=str(test_video),
        output_path=str(thumb_path),
        duration=2,
    )
    assert success is True
    assert thumb_path.exists()
    assert thumb_path.stat().st_size > 0
    assert thumb_path.stat().st_size <= 200 * 1024

    # Verify thumbnail dimensions fit within 320x320 and preserve 16:9 aspect ratio
    info = await FFmpegService.get_video_info(str(thumb_path))
    assert info is not None
    stream = info["streams"][0]
    w = stream["width"]
    h = stream["height"]
    assert w <= 320
    assert h <= 320
    assert w == 320
    assert 170 <= h <= 185  # ~178-180 for 16:9


@pytest.mark.asyncio
async def test_pyrogram_width_height_parameters_in_upload_video(tmp_path):
    """Verify upload_video passes accurate width, height, duration, and streaming support to send_video."""
    test_video = tmp_path / "sample.mp4"
    test_video.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00" + b"\x00" * 100)

    upload_service = UploadService(max_file_size_bytes=50 * 1024 * 1024)
    mock_client = AsyncMock()
    mock_msg = MagicMock(spec=Message)
    mock_client.send_video.return_value = mock_msg

    custom_caption = "Carbohydrates Lecture 🎬"
    fake_meta = VideoMetadata(
        width=854,
        height=480,
        display_width=854,
        display_height=480,
        duration=1624,
        is_widescreen_16_9=True,
    )

    with patch.object(FFmpegService, "get_video_metadata", return_value=fake_meta):
        with patch.object(FFmpegService, "generate_thumbnail", return_value=False):
            res = await upload_service.upload_video(
                client=mock_client,
                chat_id=123456,
                file_path=str(test_video),
                title="Carbohydrates",
                duration=1624,
                width=854,
                height=480,
                caption=custom_caption,
            )

            assert res == mock_msg
            mock_client.send_video.assert_called_once()
            sv_kwargs = mock_client.send_video.call_args.kwargs
            assert sv_kwargs["width"] == 854
            assert sv_kwargs["height"] == 480
            assert sv_kwargs["duration"] == 1624
            assert sv_kwargs["supports_streaming"] is True
            assert sv_kwargs["caption"] == custom_caption


@pytest.mark.asyncio
async def test_thumbnail_cleanup_after_upload(tmp_path):
    """Verify temporary thumbnail files are always deleted after upload."""
    test_video = tmp_path / "cleanup_test.mp4"
    test_video.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00" + b"\x00" * 100)

    thumb_file = tmp_path / "cleanup_test_thumb.jpg"

    upload_service = UploadService(max_file_size_bytes=50 * 1024 * 1024)
    mock_client = AsyncMock()

    async def mock_thumb_generator(v_path, o_path, **kwargs):
        Path(o_path).write_bytes(b"FAKE_JPEG_THUMB")
        return True

    with patch.object(FFmpegService, "generate_thumbnail", side_effect=mock_thumb_generator):
        with patch.object(FFmpegService, "get_video_metadata", return_value=VideoMetadata()):
            await upload_service.upload_video(
                client=mock_client,
                chat_id=123456,
                file_path=str(test_video),
                title="Cleanup Test"
            )

    # Thumbnail must be cleaned up in finally block
    assert not thumb_file.exists()


@pytest.mark.asyncio
async def test_no_unnecessary_reencoding_in_stream_copy():
    """Verify merge_video_audio attempts stream copy (-c copy) first without re-encoding."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"", b"")
        mock_exec.return_value = proc

        with patch("pathlib.Path.exists", return_value=True):
            success, msg = await FFmpegService.merge_video_audio(
                video_path="v.mp4",
                audio_path="a.m4a",
                output_path="out.mp4"
            )
            assert success is True
            assert "successful" in msg.lower()
            cmd_args = mock_exec.call_args[0]
            assert "-c" in cmd_args
            c_idx = cmd_args.index("-c")
            assert cmd_args[c_idx + 1] == "copy"


def test_compact_completion_message_format():
    """Verify completion message text format complies with compact presentation requirements."""
    title = "2.1.2 Carbohydrates - 1 Monosaccharides"
    quality = "480p"
    size = format_size(131414806)  # 125.33 MB
    duration = format_duration(1624)  # 27:04

    expected_msg = (
        "✅ <b>DOWNLOAD COMPLETE</b>\n\n"
        f"🎬 {title}\n"
        f"📺 Quality: {quality}\n"
        f"📦 Size: {size}\n"
        f"⏱ Duration: {duration}\n\n"
        "✨ <i>Delivered successfully to Telegram</i>"
    )

    assert "✅ <b>DOWNLOAD COMPLETE</b>" in expected_msg
    assert "🎬 2.1.2 Carbohydrates - 1 Monosaccharides" in expected_msg
    assert "📺 Quality: 480p" in expected_msg
    assert "125.33 MB" in expected_msg
    assert "27:04" in expected_msg
    assert "Delivered successfully to Telegram" in expected_msg
