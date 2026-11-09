"""Unit and integration tests for download and upload speed optimizations."""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.download_engine import DownloadEngine, DownloadProgress, DownloadResult
from app.services.bunny_extractor import ExtractionResult, VideoFormat
from app.services.hls_downloader import (
    ConcurrentHLSDownloader,
    HLSPlaylistInfo,
    HLSSegment,
    parse_m3u8,
)
from app.utils.crypto_patch import setup_crypto_patch, get_crypto_backend
from app.utils.formatters import format_speed, format_eta, format_size


# ==============================================================================
# 1. HLS Playlist Parsing Tests
# ==============================================================================

def test_parse_m3u8_master_playlist():
    """Verify master playlist parsing extracts bandwidths and resolves variant URLs."""
    master_content = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
        "360p/video.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\n"
        "720p/video.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n"
        "1080p/video.m3u8\n"
    )
    base_url = "https://vz-cdn.net/video_guid/playlist.m3u8?token=secure123"
    info = parse_m3u8(master_content, base_url)

    assert info.is_master is True
    assert len(info.master_variants) == 3
    # Variants must be sorted by bandwidth descending
    assert info.master_variants[0][0] == 5000000
    assert info.master_variants[0][1] == "https://vz-cdn.net/video_guid/1080p/video.m3u8?token=secure123"
    assert info.master_variants[1][0] == 2500000
    assert info.master_variants[2][0] == 800000


def test_parse_m3u8_media_playlist_ts_segments():
    """Verify media playlist parsing extracts segments and propagates authentication tokens."""
    media_content = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:4\n"
        "#EXTINF:4.000,\n"
        "seg_0.ts\n"
        "#EXTINF:4.000,\n"
        "seg_1.ts\n"
        "#EXTINF:2.500,\n"
        "seg_2.ts\n"
        "#EXT-X-ENDLIST\n"
    )
    base_url = "https://vz-cdn.net/video_guid/720p/video.m3u8?token=auth_tok_456"
    info = parse_m3u8(media_content, base_url)

    assert info.is_master is False
    assert info.target_duration == 4.0
    assert info.is_fmp4 is False
    assert len(info.segments) == 3

    assert info.segments[0].index == 0
    assert info.segments[0].duration == 4.0
    assert info.segments[0].url == "https://vz-cdn.net/video_guid/720p/seg_0.ts?token=auth_tok_456"

    assert info.segments[1].index == 1
    assert info.segments[1].url == "https://vz-cdn.net/video_guid/720p/seg_1.ts?token=auth_tok_456"

    assert info.segments[2].index == 2
    assert info.segments[2].duration == 2.5


def test_parse_m3u8_fmp4_init_segment():
    """Verify fMP4 HLS with #EXT-X-MAP is detected with init segment URL."""
    fmp4_content = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:6\n"
        "#EXT-X-MAP:URI=\"init.mp4\"\n"
        "#EXTINF:4.000,\n"
        "segment_0.m4s\n"
        "#EXTINF:4.000,\n"
        "segment_1.m4s\n"
        "#EXT-X-ENDLIST\n"
    )
    base_url = "https://vz-cdn.net/stream/video.m3u8?token=xyz"
    info = parse_m3u8(fmp4_content, base_url)

    assert info.is_fmp4 is True
    assert info.init_segment_url == "https://vz-cdn.net/stream/init.mp4?token=xyz"
    assert len(info.segments) == 2
    assert info.segments[0].url == "https://vz-cdn.net/stream/segment_0.m4s?token=xyz"


def test_parse_m3u8_byte_ranges():
    """Verify byte range parsing for single-file media playlists."""
    byterange_content = (
        "#EXTM3U\n"
        "#EXTINF:4.0,\n"
        "#EXT-X-BYTERANGE:1000@0\n"
        "media.ts\n"
        "#EXTINF:4.0,\n"
        "#EXT-X-BYTERANGE:2000@1000\n"
        "media.ts\n"
    )
    base_url = "https://vz-cdn.net/stream/video.m3u8"
    info = parse_m3u8(byterange_content, base_url)

    assert len(info.segments) == 2
    assert info.segments[0].byte_range == (1000, 0)
    assert info.segments[1].byte_range == (2000, 1000)


# ==============================================================================
# 2. Concurrent HLS Downloader Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_concurrent_hls_downloader_success(tmp_path):
    """Verify ConcurrentHLSDownloader downloads segments in parallel and executes stream-copy remux."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output_mp4 = workspace / "test_out.mp4"

    media_m3u8 = (
        "#EXTM3U\n"
        "#EXT-X-TARGETDURATION:4\n"
        "#EXTINF:4.0,\n"
        "seg_0.ts\n"
        "#EXTINF:4.0,\n"
        "seg_1.ts\n"
        "#EXT-X-ENDLIST\n"
    )

    progress_reports = []

    def mock_progress(p: DownloadProgress):
        progress_reports.append(p)

    downloader = ConcurrentHLSDownloader(
        playlist_url="https://vz-cdn.net/video.m3u8",
        output_path=output_mp4,
        workspace=workspace,
        concurrency=4,
        progress_cb=mock_progress,
    )

    # Mock aiohttp session GET responses
    class MockResponse:
        def __init__(self, status, body):
            self.status = status
            self._body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def text(self):
            return self._body.decode("utf-8") if isinstance(self._body, bytes) else self._body

        async def read(self):
            return self._body if isinstance(self._body, bytes) else self._body.encode("utf-8")

    def mock_get(url, **kwargs):
        if "video.m3u8" in url:
            return MockResponse(200, media_m3u8)
        elif "seg_0.ts" in url:
            return MockResponse(200, b"TS_PACKET_DATA_0" * 100)
        elif "seg_1.ts" in url:
            return MockResponse(200, b"TS_PACKET_DATA_1" * 100)
        return MockResponse(404, b"Not Found")

    with patch("aiohttp.ClientSession.get", side_effect=mock_get):
        # Mock FFmpeg remuxing process
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate.return_value = (b"", b"")
            mock_exec.return_value = proc

            # Simulate FFmpeg producing the output file
            async def fake_remux(*args, **kwargs):
                output_mp4.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2000)
                return (b"", b"")

            proc.communicate.side_effect = fake_remux

            success, out_path, size, err = await downloader.download()

            assert success is True
            assert out_path == str(output_mp4)
            assert size > 0
            assert err is None
            assert len(progress_reports) > 0


@pytest.mark.asyncio
async def test_concurrent_hls_downloader_403_forbidden(tmp_path):
    """Verify 403 Forbidden on playlist returns friendly owner authorization error."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output_mp4 = workspace / "test_out.mp4"

    downloader = ConcurrentHLSDownloader(
        playlist_url="https://vz-cdn.net/protected.m3u8",
        output_path=output_mp4,
        workspace=workspace,
    )

    class Mock403Response:
        def __init__(self):
            self.status = 403

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch("aiohttp.ClientSession.get", return_value=Mock403Response()):
        success, out_path, size, err = await downloader.download()
        assert success is False
        assert "403" in err
        assert "authorization" in err.lower() or "token" in err.lower()


# ==============================================================================
# 3. Crypto Acceleration Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_crypto_patch_active():
    """Verify setup_crypto_patch activates a C-accelerated engine."""
    backend, is_accelerated = setup_crypto_patch()
    assert is_accelerated is True
    assert backend in ("tgcrypto", "cryptg")
    assert get_crypto_backend() in ("tgcrypto", "cryptg")


@pytest.mark.asyncio
async def test_crypto_patch_ige_roundtrip():
    """Verify AES-IGE-256 roundtrip encryption and decryption with patched engine."""
    import pyrogram.crypto.aes as aes

    setup_crypto_patch()

    plaintext = b"Test 16-byte blk" * 16  # 256 bytes
    key = b"K" * 32
    iv = b"I" * 32

    encrypted = aes.ige256_encrypt(plaintext, key, iv)
    assert encrypted != plaintext
    assert len(encrypted) == len(plaintext)

    decrypted = aes.ige256_decrypt(encrypted, key, iv)
    assert decrypted == plaintext


@pytest.mark.asyncio
async def test_crypto_benchmark_speed():
    """Benchmark MTProto crypto speed verifying sub-second encryption of large buffers."""
    import pyrogram.crypto.aes as aes

    setup_crypto_patch()

    # Benchmark 5 MB
    test_data = b"X" * (5 * 1024 * 1024)
    key = b"K" * 32
    iv = b"I" * 32

    t0 = time.monotonic()
    encrypted = aes.ige256_encrypt(test_data, key, iv)
    elapsed = time.monotonic() - t0

    # 5 MB must encrypt in under 0.25 seconds with C-extensions (pyaes would take > 25 seconds)
    assert elapsed < 0.25
    assert len(encrypted) == len(test_data)


# ==============================================================================
# 4. DownloadEngine Integration & Fallback Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_download_engine_invokes_concurrent_hls(tmp_path):
    """Verify DownloadEngine routes .m3u8 streams to _download_hls_concurrent."""
    engine = DownloadEngine(
        download_dir=str(tmp_path / "downloads"),
        temp_dir=str(tmp_path / "temp"),
        hls_concurrency=8,
    )

    extraction = ExtractionResult(
        title="Test HLS",
        video_formats=[
            VideoFormat(
                format_id="hls-720p",
                quality="720p",
                height=720,
                url="https://vz-cdn.net/playlist.m3u8",
                has_audio=True,
            )
        ]
    )

    fake_result = DownloadResult(success=True, output_path="/fake/path.mp4", file_size=1024)

    with patch.object(engine, "_download_hls_concurrent", AsyncMock(return_value=fake_result)) as mock_hls:
        res = await engine.download(
            extraction=extraction,
            selected_format=extraction.video_formats[0],
        )

        assert res.success is True
        mock_hls.assert_called_once()


@pytest.mark.asyncio
async def test_download_engine_hls_fallback_to_standard(tmp_path):
    """Verify DownloadEngine falls back to standard FFmpeg HLS when concurrent downloader fails non-403."""
    engine = DownloadEngine(
        download_dir=str(tmp_path / "downloads"),
        temp_dir=str(tmp_path / "temp"),
    )

    extraction = ExtractionResult(
        title="Test Fallback",
        video_formats=[
            VideoFormat(
                format_id="hls-480p",
                quality="480p",
                height=480,
                url="https://vz-cdn.net/fallback.m3u8",
                has_audio=True,
            )
        ]
    )

    fallback_result = DownloadResult(success=True, output_path="/fallback/out.mp4", file_size=2048)

    # Make ConcurrentHLSDownloader fail with non-403 error
    with patch("app.services.download_engine.ConcurrentHLSDownloader") as MockDownloader:
        instance = MagicMock()
        instance.download = AsyncMock(return_value=(False, None, 0, "Network connection reset"))
        MockDownloader.return_value = instance

        with patch.object(engine, "_download_hls", AsyncMock(return_value=fallback_result)) as mock_ffmpeg:
            res = await engine.download(
                extraction=extraction,
                selected_format=extraction.video_formats[0],
            )

            assert res.success is True
            assert res.output_path == "/fallback/out.mp4"
            mock_ffmpeg.assert_called_once()


# ==============================================================================
# 5. Formatters & Speed Metrics Tests
# ==============================================================================

def test_speed_formatters():
    """Verify format_speed displays proper human-readable speeds in MB/s."""
    speed_bytes = 12.28 * 1024 * 1024
    formatted = format_speed(speed_bytes)
    assert "12.28 MB/s" in formatted

    zero_speed = format_speed(0)
    assert zero_speed == "0 B/s"


def test_eta_formatters():
    """Verify format_eta displays MM:SS or HH:MM:SS."""
    assert format_eta(30) == "00:30"
    assert format_eta(90) == "01:30"
    assert format_eta(3665) == "01:01:05"
    assert format_eta(None) == "--:--"
    assert format_eta(0) == "--:--"
