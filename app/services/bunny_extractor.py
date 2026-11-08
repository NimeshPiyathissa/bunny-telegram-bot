import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx
import yt_dlp

from app.utils.security import validate_url, is_bunny_url, parse_bunny_embed_url

logger = logging.getLogger(__name__)


@dataclass
class VideoFormat:
    format_id: str
    quality: str  # e.g. "720p", "1080p"
    height: int
    width: int = 0
    fps: int = 0
    filesize: Optional[int] = None
    vcodec: str = ""
    acodec: str = ""
    url: str = ""
    has_audio: bool = False
    ext: str = "mp4"


@dataclass
class AudioFormat:
    format_id: str
    acodec: str
    abr: float = 0
    filesize: Optional[int] = None
    url: str = ""
    ext: str = "m4a"


@dataclass
class ExtractionResult:
    success: bool = False
    title: str = "Unknown Video"
    duration: int = 0
    thumbnail: Optional[str] = None
    video_formats: List[VideoFormat] = field(default_factory=list)
    audio_formats: List[AudioFormat] = field(default_factory=list)
    direct_url: Optional[str] = None  # If a single direct MP4 is available
    error: Optional[str] = None
    needs_merge: bool = False  # True if video and audio are separate
    library_id: Optional[str] = None
    video_guid: Optional[str] = None
    referer: Optional[str] = None


class BunnyExtractor:
    """Extracts video information from Bunny Stream URLs."""

    BUNNY_CDN_PATTERN = re.compile(
        r'https?://[\w.-]*\.?(?:b-cdn\.net|bunnycdn\.com|mediadelivery\.net)/'
    )

    def __init__(self):
        self._ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'no_color': True,
            'geo_bypass': False,
            'nocheckcertificate': False,  # Always verify SSL
            'socket_timeout': 30,
        }

    async def extract(self, url: str) -> ExtractionResult:
        """Extract video information from a URL."""
        result = ExtractionResult()
        result.referer = url

        # Validate URL
        is_valid, error_msg = validate_url(url)
        if not is_valid:
            result.error = error_msg
            return result

        # Parse Bunny embed URL if applicable
        bunny_info = parse_bunny_embed_url(url)
        if bunny_info:
            result.library_id = bunny_info["library_id"]
            result.video_guid = bunny_info["video_guid"]

        # Try yt-dlp extraction first
        try:
            result = await self._extract_with_ytdlp(url, result)
        except Exception as e:
            logger.warning(f"yt-dlp extraction failed: {e}")
            # Try direct HTTP probing as fallback
            try:
                result = await self._probe_direct(url, result)
            except Exception as e2:
                logger.error(f"Direct probing also failed: {e2}")
                result.error = (
                    "Could not extract video information. "
                    "This may be due to:\n"
                    "• Authorization/token requirements\n"
                    "• DRM protection\n"
                    "• Geo-restrictions\n"
                    "• The URL is not a downloadable video\n\n"
                    "Please ensure you have an authorized, downloadable URL "
                    "from the content owner."
                )

        return result

    async def _extract_with_ytdlp(self, url: str, result: ExtractionResult) -> ExtractionResult:
        """Extract using yt-dlp."""
        loop = asyncio.get_event_loop()

        ydl_opts = {
            **self._ydl_opts,
            'http_headers': {
                'Referer': url,
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Origin': 'https://iframe.mediadelivery.net',
            }
        }

        def _do_extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await loop.run_in_executor(None, _do_extract)

        if not info:
            result.error = "No video information found."
            return result

        result.success = True
        result.title = info.get('title', 'Unknown Video')
        result.duration = int(info.get('duration', 0) or 0)
        result.thumbnail = info.get('thumbnail')

        formats = info.get('formats', [])

        video_fmts = []
        audio_fmts = []
        seen_heights = set()

        for fmt in formats:
            vcodec = fmt.get('vcodec', 'none')
            acodec = fmt.get('acodec', 'none')
            height = fmt.get('height', 0) or 0

            has_video = vcodec and vcodec != 'none'
            has_audio = acodec and acodec != 'none'

            if has_video and height > 0:
                if height not in seen_heights:
                    seen_heights.add(height)
                    video_fmts.append(VideoFormat(
                        format_id=fmt.get('format_id', ''),
                        quality=f"{height}p",
                        height=height,
                        width=fmt.get('width', 0) or 0,
                        fps=fmt.get('fps', 0) or 0,
                        filesize=fmt.get('filesize') or fmt.get('filesize_approx'),
                        vcodec=vcodec,
                        acodec=acodec if has_audio else '',
                        url=fmt.get('url', ''),
                        has_audio=has_audio,
                        ext=fmt.get('ext', 'mp4'),
                    ))
            elif has_audio and not has_video:
                audio_fmts.append(AudioFormat(
                    format_id=fmt.get('format_id', ''),
                    acodec=acodec,
                    abr=fmt.get('abr', 0) or 0,
                    filesize=fmt.get('filesize') or fmt.get('filesize_approx'),
                    url=fmt.get('url', ''),
                    ext=fmt.get('ext', 'm4a'),
                ))

        # Sort by height descending
        video_fmts.sort(key=lambda f: f.height, reverse=True)
        audio_fmts.sort(key=lambda f: f.abr, reverse=True)

        result.video_formats = video_fmts
        result.audio_formats = audio_fmts

        # Determine if merging is needed
        if video_fmts and not any(f.has_audio for f in video_fmts):
            result.needs_merge = audio_fmts != []

        # Check for direct URL
        best_url = info.get('url')
        if best_url and not formats:
            result.direct_url = best_url

        if not video_fmts and not result.direct_url:
            result.success = False
            result.error = (
                "No downloadable video formats found. "
                "The video may require authorization or may not be available for download."
            )

        return result

    async def _probe_direct(self, url: str, result: ExtractionResult) -> ExtractionResult:
        """Try to probe URL directly via HTTP."""
        bunny_info = parse_bunny_embed_url(url)
        if not bunny_info:
            result.error = "Not a recognized Bunny Stream URL and yt-dlp extraction failed."
            return result

        library_id = bunny_info["library_id"]
        video_guid = bunny_info["video_guid"]

        # Try known Bunny CDN patterns for HLS playlist
        playlist_url = f"https://iframe.mediadelivery.net/embed/{library_id}/{video_guid}"

        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, verify=True,
            headers={"Referer": url, "User-Agent": "Mozilla/5.0"}
        ) as client:
            try:
                resp = await client.get(playlist_url)
                if resp.status_code == 200:
                    text = resp.text
                    # Look for HLS playlist URLs in the page
                    m3u8_matches = re.findall(
                        r'https?://[^"\s]+\.m3u8[^"\s]*', text
                    )
                    mp4_matches = re.findall(
                        r'https?://[^"\s]+\.mp4[^"\s]*', text
                    )

                    # Try to extract title from page
                    title_match = re.search(r'<title>([^<]+)</title>', text)
                    if title_match:
                        result.title = title_match.group(1).strip()

                    if m3u8_matches:
                        # Try to parse HLS playlist to get qualities
                        result = await self._parse_hls(m3u8_matches[0], result, client)
                    elif mp4_matches:
                        result.direct_url = mp4_matches[0]
                        result.success = True
                        result.video_formats = [
                            VideoFormat(
                                format_id="direct",
                                quality="Direct MP4",
                                height=0,
                                url=mp4_matches[0],
                                has_audio=True,
                            )
                        ]
                    else:
                        result.error = (
                            "Could not find downloadable media in the embed page. "
                            "The video may require specific authorization tokens."
                        )
                elif resp.status_code == 403:
                    result.error = (
                        "Access denied (403 Forbidden). "
                        "The video requires authorization. "
                        "Please provide an authorized URL from the content owner."
                    )
                elif resp.status_code == 404:
                    result.error = "Video not found (404). Please check the URL."
                else:
                    result.error = f"Server returned status {resp.status_code}."
            except httpx.TimeoutException:
                result.error = "Connection timed out while accessing the video."
            except httpx.ConnectError:
                result.error = "Could not connect to the server."

        return result

    async def _parse_hls(self, m3u8_url: str, result: ExtractionResult,
                         client: httpx.AsyncClient) -> ExtractionResult:
        """Parse an HLS master playlist to extract available qualities."""
        try:
            resp = await client.get(m3u8_url)
            if resp.status_code != 200:
                result.error = "Could not access HLS playlist."
                return result

            content = resp.text
            # Parse master playlist
            qualities = []
            lines = content.strip().split('\n')

            for i, line in enumerate(lines):
                if line.startswith('#EXT-X-STREAM-INF:'):
                    # Extract resolution
                    res_match = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
                    bw_match = re.search(r'BANDWIDTH=(\d+)', line)

                    if res_match and i + 1 < len(lines):
                        width = int(res_match.group(1))
                        height = int(res_match.group(2))
                        stream_url = lines[i + 1].strip()

                        # Resolve relative URLs
                        if not stream_url.startswith('http'):
                            base = m3u8_url.rsplit('/', 1)[0]
                            stream_url = f"{base}/{stream_url}"
                        if '?' in m3u8_url and '?' not in stream_url:
                            token_query = m3u8_url.split('?', 1)[1]
                            stream_url = f"{stream_url}?{token_query}"

                        qualities.append(VideoFormat(
                            format_id=f"hls-{height}p",
                            quality=f"{height}p",
                            height=height,
                            width=width,
                            url=stream_url,
                            has_audio=True,
                            ext="mp4",
                        ))

            if qualities:
                qualities.sort(key=lambda f: f.height, reverse=True)
                result.video_formats = qualities
                result.success = True
            else:
                # It's a media playlist, not a master playlist
                result.direct_url = m3u8_url
                result.success = True
                result.video_formats = [
                    VideoFormat(
                        format_id="hls-direct",
                        quality="HLS Stream",
                        height=0,
                        url=m3u8_url,
                        has_audio=True,
                    )
                ]
        except Exception as e:
            logger.error(f"HLS parsing error: {e}")
            result.error = f"Error parsing HLS playlist: {e}"

        return result
