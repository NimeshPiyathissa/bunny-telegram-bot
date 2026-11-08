import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from app.services.bunny_extractor import (
    BunnyExtractor,
    ExtractionResult,
    VideoFormat,
    AudioFormat,
)


@pytest.mark.asyncio
async def test_extractor_ytdlp_success():
    """Test successful yt-dlp extraction with separate video and audio formats."""
    extractor = BunnyExtractor()
    mock_info = {
        "title": "Bunny Stream Demo Video",
        "duration": 3600,
        "thumbnail": "https://vz-cdn.net/thumb.jpg",
        "formats": [
            {
                "format_id": "v1080",
                "height": 1080,
                "width": 1920,
                "fps": 30,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "filesize": 500000000,
                "url": "https://vz-cdn.net/1080p.mp4",
                "ext": "mp4",
            },
            {
                "format_id": "v720",
                "height": 720,
                "width": 1280,
                "fps": 30,
                "vcodec": "avc1.4d401f",
                "acodec": "none",
                "filesize": 250000000,
                "url": "https://vz-cdn.net/720p.mp4",
                "ext": "mp4",
            },
            {
                "format_id": "a128",
                "height": None,
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128,
                "filesize": 50000000,
                "url": "https://vz-cdn.net/audio.m4a",
                "ext": "m4a",
            },
        ],
    }

    with patch.object(extractor, "_extract_with_ytdlp") as mock_extract:
        expected = ExtractionResult(
            success=True,
            title=mock_info["title"],
            duration=mock_info["duration"],
            thumbnail=mock_info["thumbnail"],
            video_formats=[
                VideoFormat(
                    format_id="v1080",
                    quality="1080p",
                    height=1080,
                    width=1920,
                    fps=30,
                    filesize=500000000,
                    vcodec="avc1.640028",
                    has_audio=False,
                    url="https://vz-cdn.net/1080p.mp4",
                ),
                VideoFormat(
                    format_id="v720",
                    quality="720p",
                    height=720,
                    width=1280,
                    fps=30,
                    filesize=250000000,
                    vcodec="avc1.4d401f",
                    has_audio=False,
                    url="https://vz-cdn.net/720p.mp4",
                ),
            ],
            audio_formats=[
                AudioFormat(
                    format_id="a128",
                    acodec="mp4a.40.2",
                    abr=128,
                    filesize=50000000,
                    url="https://vz-cdn.net/audio.m4a",
                )
            ],
            needs_merge=True,
        )
        mock_extract.return_value = expected

        with patch("app.services.bunny_extractor.validate_url", return_value=(True, "")):
            result = await extractor.extract(
                "https://iframe.mediadelivery.net/embed/346396/af5aa834-acd6-47bc-a800-32ad1a8f7d92"
            )
            assert result.success is True
            assert result.title == "Bunny Stream Demo Video"
            assert result.duration == 3600
            assert len(result.video_formats) == 2
            assert result.video_formats[0].height == 1080
            assert result.needs_merge is True


@pytest.mark.asyncio
async def test_extractor_inaccessible_video_auth_denied():
    """Verify handling when video access is 403 Forbidden / requires auth."""
    extractor = BunnyExtractor()

    with patch.object(extractor, "_extract_with_ytdlp", side_effect=Exception("HTTP 403 Forbidden")):
        with patch.object(extractor, "_probe_direct") as mock_probe:
            mock_probe.return_value = ExtractionResult(
                success=False,
                error="Access denied (403 Forbidden). The video requires authorization."
            )
            with patch("app.services.bunny_extractor.validate_url", return_value=(True, "")):
                result = await extractor.extract(
                    "https://iframe.mediadelivery.net/embed/346396/af5aa834-acd6-47bc-a800-32ad1a8f7d92"
                )
                assert result.success is False
                assert "403" in result.error or "authorization" in result.error.lower()


@pytest.mark.asyncio
async def test_extractor_missing_formats():
    """Verify handling when media has no downloadable video formats."""
    extractor = BunnyExtractor()
    initial_res = ExtractionResult()

    with patch("yt_dlp.YoutubeDL") as MockYDL:
        instance = MockYDL.return_value.__enter__.return_value
        instance.extract_info.return_value = {
            "title": "No video streams",
            "duration": 10,
            "formats": []
        }
        res = await extractor._extract_with_ytdlp("https://iframe.mediadelivery.net/embed/1/2", initial_res)
        assert res.success is False
        assert "no downloadable video formats" in res.error.lower()


@pytest.mark.asyncio
async def test_hls_master_playlist_parsing():
    """Verify HLS master playlist parsing for multi-bitrate streams."""
    extractor = BunnyExtractor()
    result = ExtractionResult()

    hls_content = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360p/video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=842x480
480p/video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720
720p/video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
1080p/video.m3u8
"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = hls_content

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    parsed = await extractor._parse_hls(
        "https://vz-cdn.net/playlist.m3u8", result, mock_client
    )

    assert parsed.success is True
    assert len(parsed.video_formats) == 4
    # Ensure sorted by height descending
    assert parsed.video_formats[0].height == 1080
    assert parsed.video_formats[1].height == 720
    assert parsed.video_formats[2].height == 480
    assert parsed.video_formats[3].height == 360
    assert parsed.video_formats[0].url == "https://vz-cdn.net/1080p/video.m3u8"
