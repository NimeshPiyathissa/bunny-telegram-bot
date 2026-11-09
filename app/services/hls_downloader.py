"""High-Performance Concurrent HLS Segment Downloader.

Features:
- Multi-connection segment downloading with persistent aiohttp connection pooling.
- Configurable concurrency (default: 10).
- Automatic keep-alive, DNS caching, and TCP reuse across segments.
- Master playlist resolution and Bunny Stream token query parameter preservation.
- Exponential backoff retry handling on transient segment download failures.
- Real-time download progress tracking (MB/s speed, completed percent, dynamic ETA).
- Stream-copy remuxing via FFmpeg (-c copy) for zero quality loss and no re-encoding.
- Safe cancellation, isolated per-job segment workspaces, and post-assembly cleanup.
"""

import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import aiohttp

from app.utils.security import sanitize_subprocess_arg

logger = logging.getLogger(__name__)


@dataclass
class HLSKeyInfo:
    method: str
    uri: Optional[str] = None
    iv: Optional[bytes] = None
    key: Optional[bytes] = None


@dataclass
class HLSSegment:
    index: int
    url: str
    duration: float = 0.0
    byte_range: Optional[Tuple[int, int]] = None  # (length, offset)
    key_info: Optional[HLSKeyInfo] = None


@dataclass
class HLSPlaylistInfo:
    is_master: bool = False
    master_variants: List[Tuple[int, str]] = field(default_factory=list)  # [(bandwidth, url)]
    segments: List[HLSSegment] = field(default_factory=list)
    init_segment_url: Optional[str] = None
    target_duration: float = 4.0
    is_fmp4: bool = False


def _attach_tokens(base_url: str, target_url: str) -> str:
    """Ensure authentication query parameters from base_url are propagated to target_url."""
    base_parsed = urlparse(base_url)
    target_parsed = urlparse(target_url)

    if base_parsed.query and not target_parsed.query:
        separator = "&" if "?" in target_url else "?"
        return f"{target_url}{separator}{base_parsed.query}"
    return target_url


def parse_m3u8(content: str, playlist_url: str) -> HLSPlaylistInfo:
    """Parse M3U8 content for master variants, media segments, and encryption keys."""
    info = HLSPlaylistInfo()
    lines = [line.strip() for line in content.splitlines() if line.strip()]

    # 1. Master Playlist Check
    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        info.is_master = True
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
                bw = 0
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                if bw_match:
                    bw = int(bw_match.group(1))

                stream_url = lines[i + 1]
                if not stream_url.startswith("#"):
                    resolved = urljoin(playlist_url, stream_url)
                    resolved = _attach_tokens(playlist_url, resolved)
                    info.master_variants.append((bw, resolved))

        # Sort variants by bandwidth descending
        info.master_variants.sort(key=lambda v: v[0], reverse=True)
        return info

    # 2. Media Playlist Parsing
    current_key_info: Optional[HLSKeyInfo] = None
    next_duration = 0.0
    next_byte_range: Optional[Tuple[int, int]] = None
    current_offset = 0
    seg_index = 0

    for line in lines:
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                info.target_duration = float(line.split(":", 1)[1])
            except (ValueError, IndexError):
                pass

        elif line.startswith("#EXT-X-MAP:"):
            # fMP4 initialization segment: #EXT-X-MAP:URI="init.mp4"
            info.is_fmp4 = True
            uri_match = re.search(r'URI="([^"]+)"', line)
            if uri_match:
                init_url = urljoin(playlist_url, uri_match.group(1))
                info.init_segment_url = _attach_tokens(playlist_url, init_url)

        elif line.startswith("#EXT-X-KEY:"):
            method_match = re.search(r'METHOD=([^,\s]+)', line)
            uri_match = re.search(r'URI="([^"]+)"', line)
            iv_match = re.search(r'IV=0x([0-9a-fA-F]+)', line)

            method = method_match.group(1) if method_match else "NONE"
            if method == "NONE":
                current_key_info = None
            else:
                key_uri = urljoin(playlist_url, uri_match.group(1)) if uri_match else None
                if key_uri:
                    key_uri = _attach_tokens(playlist_url, key_uri)
                iv_bytes = bytes.fromhex(iv_match.group(1)) if iv_match else None
                current_key_info = HLSKeyInfo(method=method, uri=key_uri, iv=iv_bytes)

        elif line.startswith("#EXT-X-BYTERANGE:"):
            # Format: #EXT-X-BYTERANGE:length[@offset]
            val = line.split(":", 1)[1]
            if "@" in val:
                length_str, offset_str = val.split("@", 1)
                offset = int(offset_str)
                length = int(length_str)
                current_offset = offset + length
                next_byte_range = (length, offset)
            else:
                length = int(val)
                next_byte_range = (length, current_offset)
                current_offset += length

        elif line.startswith("#EXTINF:"):
            # Format: #EXTINF:4.000,
            dur_str = line.split(":", 1)[1].split(",", 1)[0]
            try:
                next_duration = float(dur_str)
            except ValueError:
                next_duration = 0.0

        elif not line.startswith("#"):
            # Segment URI line
            seg_url = urljoin(playlist_url, line)
            seg_url = _attach_tokens(playlist_url, seg_url)

            if seg_url.endswith(".m4s") or ".m4s?" in seg_url:
                info.is_fmp4 = True

            info.segments.append(
                HLSSegment(
                    index=seg_index,
                    url=seg_url,
                    duration=next_duration,
                    byte_range=next_byte_range,
                    key_info=current_key_info,
                )
            )
            seg_index += 1
            next_duration = 0.0
            next_byte_range = None

    return info


class ConcurrentHLSDownloader:
    """High-speed concurrent HLS downloader with connection pooling and stream copy remuxing."""

    def __init__(
        self,
        playlist_url: str,
        output_path: Path,
        workspace: Path,
        concurrency: int = 10,
        timeout: int = 3600,
        referer: str = "https://iframe.mediadelivery.net/",
        user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        progress_cb: Optional[Callable] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ):
        self.playlist_url = playlist_url
        self.output_path = output_path
        self.workspace = workspace
        self.concurrency = max(1, min(concurrency, 32))
        self.timeout = timeout
        self.referer = referer
        self.user_agent = user_agent
        self.progress_cb = progress_cb
        self.cancel_check = cancel_check
        self.segments_dir = self.workspace / "hls_segments"

        self._headers = {
            "Referer": self.referer,
            "User-Agent": self.user_agent,
            "Origin": "https://iframe.mediadelivery.net",
        }

    async def download(self) -> Tuple[bool, Optional[str], int, Optional[str]]:
        """Execute concurrent segment downloading and stream-copy reassembly.

        Returns:
            Tuple[bool, Optional[str], int, Optional[str]]: (success, output_path, file_size, error_message)
        """
        self.segments_dir.mkdir(parents=True, exist_ok=True)

        connector = aiohttp.TCPConnector(
            limit=max(30, self.concurrency * 2),
            limit_per_host=max(20, self.concurrency),
            ttl_dns_cache=300,
            keepalive_timeout=60,
            force_close=False,
        )

        client_timeout = aiohttp.ClientTimeout(
            total=self.timeout,
            sock_connect=15,
            sock_read=30,
        )

        async with aiohttp.ClientSession(
            connector=connector,
            timeout=client_timeout,
            headers=self._headers,
        ) as session:
            # 1. Fetch and parse playlist
            logger.info(f"[HLS:FETCH] Fetching playlist: {self.playlist_url}")
            try:
                async with session.get(self.playlist_url) as resp:
                    if resp.status == 403:
                        return (
                            False, None, 0,
                            "Access denied (403 Forbidden). "
                            "The Bunny Stream media source requires token authorization from the content owner."
                        )
                    if resp.status != 200:
                        return (False, None, 0, f"HLS playlist fetch returned status {resp.status}")
                    content = await resp.text()
            except Exception as e:
                return (False, None, 0, f"Failed to fetch HLS playlist: {e}")

            playlist_info = parse_m3u8(content, self.playlist_url)

            # If master playlist, pick the best variant and fetch media playlist
            target_media_url = self.playlist_url
            if playlist_info.is_master:
                if not playlist_info.master_variants:
                    return (False, None, 0, "No playable streams found in master HLS playlist.")
                best_variant_url = playlist_info.master_variants[0][1]
                target_media_url = best_variant_url
                logger.info(f"[HLS:MASTER] Resolved best variant: {target_media_url}")

                try:
                    async with session.get(best_variant_url) as resp:
                        if resp.status == 403:
                            return (
                                False, None, 0,
                                "Access denied (403 Forbidden). "
                                "The Bunny Stream media source requires token authorization from the content owner."
                            )
                        if resp.status != 200:
                            return (False, None, 0, f"Media playlist returned status {resp.status}")
                        media_content = await resp.text()
                        playlist_info = parse_m3u8(media_content, target_media_url)
                except Exception as e:
                    return (False, None, 0, f"Failed to fetch variant playlist: {e}")

            total_segments = len(playlist_info.segments)
            if total_segments == 0:
                return (False, None, 0, "HLS playlist contains zero media segments.")

            logger.info(
                f"[HLS:START] Downloading {total_segments} segments concurrently "
                f"(concurrency={self.concurrency}, fmp4={playlist_info.is_fmp4})"
            )

            # 2. Download init segment if fMP4
            if playlist_info.init_segment_url:
                init_path = self.segments_dir / "init.mp4"
                try:
                    async with session.get(playlist_info.init_segment_url) as resp:
                        if resp.status == 200:
                            init_path.write_bytes(await resp.read())
                        else:
                            logger.warning(f"[HLS:INIT] Init segment fetch status {resp.status}")
                except Exception as e:
                    logger.warning(f"[HLS:INIT] Could not fetch init segment: {e}")

            # 3. Concurrent segment downloading
            semaphore = asyncio.Semaphore(self.concurrency)
            lock = asyncio.Lock()
            completed_segments = 0
            downloaded_bytes = 0
            start_time = time.monotonic()
            last_progress_time = [0.0]

            def _trigger_progress():
                if not self.progress_cb:
                    return
                now = time.monotonic()
                if now - last_progress_time[0] < 1.2 and completed_segments < total_segments:
                    return
                last_progress_time[0] = now

                elapsed = max(0.001, now - start_time)
                speed = downloaded_bytes / elapsed if elapsed > 0 else 0.0

                if completed_segments > 0:
                    avg_size = downloaded_bytes / completed_segments
                    est_total = int(avg_size * total_segments)
                else:
                    est_total = 0

                pct = (completed_segments / total_segments * 100) if total_segments > 0 else 0
                remaining_bytes = max(0, est_total - downloaded_bytes)
                eta = (remaining_bytes / speed) if speed > 0 and est_total > 0 else None

                from app.services.download_engine import DownloadProgress
                p = DownloadProgress(
                    status="downloading",
                    downloaded_bytes=downloaded_bytes,
                    total_bytes=est_total,
                    speed=speed,
                    eta=eta,
                    percent=pct,
                    filename=self.output_path.name,
                )
                try:
                    self.progress_cb(p)
                except Exception:
                    pass

            async def _download_segment(seg: HLSSegment):
                nonlocal completed_segments, downloaded_bytes

                if self.cancel_check and self.cancel_check():
                    return

                seg_filename = f"seg_{seg.index:05d}.{'m4s' if playlist_info.is_fmp4 else 'ts'}"
                seg_file = self.segments_dir / seg_filename

                if seg_file.exists() and seg_file.stat().st_size > 0:
                    async with lock:
                        completed_segments += 1
                        downloaded_bytes += seg_file.stat().st_size
                    _trigger_progress()
                    return

                req_headers = dict(self._headers)
                if seg.byte_range:
                    length, offset = seg.byte_range
                    req_headers["Range"] = f"bytes={offset}-{offset + length - 1}"

                max_retries = 4
                for attempt in range(max_retries):
                    if self.cancel_check and self.cancel_check():
                        return

                    try:
                        async with semaphore:
                            async with session.get(seg.url, headers=req_headers) as resp:
                                if resp.status == 403:
                                    raise PermissionError("Access denied (403 Forbidden).")
                                if resp.status not in (200, 206):
                                    raise RuntimeError(f"HTTP status {resp.status}")

                                data = await resp.read()
                                if not data:
                                    raise RuntimeError("Zero bytes received")

                                temp_path = self.segments_dir / f"{seg_filename}.tmp"
                                temp_path.write_bytes(data)
                                temp_path.replace(seg_file)

                                seg_size = len(data)
                                async with lock:
                                    completed_segments += 1
                                    downloaded_bytes += seg_size

                                _trigger_progress()
                                return
                    except PermissionError:
                        raise
                    except Exception as err:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(0.3 * (2 ** attempt))
                        else:
                            raise RuntimeError(
                                f"Segment {seg.index} download failed after {max_retries} retries: {err}"
                            )

            # Run all segment downloads concurrently
            tasks = [_download_segment(seg) for seg in playlist_info.segments]
            try:
                await asyncio.gather(*tasks)
            except PermissionError as perm_err:
                self.cleanup()
                return (
                    False, None, 0,
                    "Access denied (403 Forbidden). "
                    "The Bunny Stream media source requires token authorization from the content owner."
                )
            except Exception as e:
                self.cleanup()
                return (False, None, 0, f"Concurrent download failed: {e}")

            if self.cancel_check and self.cancel_check():
                self.cleanup()
                return (False, None, 0, "Download cancelled by user.")

            # 4. Stream Copy Remuxing
            logger.info(
                f"[HLS:REMUX] All {completed_segments}/{total_segments} segments downloaded. "
                f"Assembling into {self.output_path.name} via FFmpeg stream copy..."
            )
            success, remux_err = await self._remux_segments(playlist_info)
            if not success:
                return (False, None, 0, f"FFmpeg reassembly failed: {remux_err}")

            if not self.output_path.exists() or self.output_path.stat().st_size == 0:
                return (False, None, 0, "Remux completed but output MP4 file is empty or missing.")

            final_size = self.output_path.stat().st_size
            self.cleanup()

            # Final progress report at 100%
            if self.progress_cb:
                from app.services.download_engine import DownloadProgress
                p = DownloadProgress(
                    status="finished",
                    downloaded_bytes=final_size,
                    total_bytes=final_size,
                    percent=100.0,
                    filename=self.output_path.name,
                )
                try:
                    self.progress_cb(p)
                except Exception:
                    pass

            logger.info(
                f"[HLS:DONE] Successfully assembled {self.output_path.name} "
                f"({final_size} bytes) in {time.monotonic() - start_time:.2f}s"
            )
            return (True, str(self.output_path), final_size, None)

    async def _remux_segments(self, playlist_info: HLSPlaylistInfo) -> Tuple[bool, Optional[str]]:
        """Assembles downloaded segments into a valid MP4 using stream copy without re-encoding."""
        concat_file = self.workspace / f"concat_{self.output_path.stem}.txt"

        try:
            if playlist_info.is_fmp4:
                # For fMP4: concatenate init.mp4 + segment .m4s files
                combined_fmp4 = self.workspace / f"combined_{self.output_path.stem}.mp4"
                with open(combined_fmp4, "wb") as outfile:
                    init_path = self.segments_dir / "init.mp4"
                    if init_path.exists():
                        outfile.write(init_path.read_bytes())

                    for seg in playlist_info.segments:
                        seg_path = self.segments_dir / f"seg_{seg.index:05d}.m4s"
                        if seg_path.exists():
                            outfile.write(seg_path.read_bytes())

                cmd = [
                    'ffmpeg', '-y',
                    '-i', sanitize_subprocess_arg(str(combined_fmp4)),
                    '-c', 'copy',
                    '-movflags', '+faststart',
                    sanitize_subprocess_arg(str(self.output_path)),
                ]
            else:
                # For MPEG-TS (.ts): write concat demuxer file
                lines = []
                for seg in playlist_info.segments:
                    seg_path = self.segments_dir / f"seg_{seg.index:05d}.ts"
                    escaped_path = str(seg_path.resolve()).replace('\\', '/').replace("'", "'\\''")
                    lines.append(f"file '{escaped_path}'")

                concat_file.write_text("\n".join(lines), encoding="utf-8")

                cmd = [
                    'ffmpeg', '-y',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', sanitize_subprocess_arg(str(concat_file)),
                    '-c', 'copy',
                    '-bsf:a', 'aac_adtstoasc',
                    '-movflags', '+faststart',
                    sanitize_subprocess_arg(str(self.output_path)),
                ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)

            if proc.returncode == 0 and self.output_path.exists() and self.output_path.stat().st_size > 0:
                return (True, None)

            # Retry without bsf:a if MPEG-TS copy failed
            if not playlist_info.is_fmp4:
                logger.warning("[HLS:REMUX] Retrying remux without bsf:a...")
                cmd_retry = [
                    'ffmpeg', '-y',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', sanitize_subprocess_arg(str(concat_file)),
                    '-c', 'copy',
                    '-movflags', '+faststart',
                    sanitize_subprocess_arg(str(self.output_path)),
                ]
                proc_retry = await asyncio.create_subprocess_exec(
                    *cmd_retry,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_r, stderr_r = await asyncio.wait_for(proc_retry.communicate(), timeout=self.timeout)
                if proc_retry.returncode == 0 and self.output_path.exists() and self.output_path.stat().st_size > 0:
                    return (True, None)
                stderr = stderr_r

            err_text = stderr.decode('utf-8', errors='replace')[-500:]
            return (False, err_text)
        except Exception as e:
            return (False, str(e))
        finally:
            if concat_file.exists():
                try:
                    concat_file.unlink(missing_ok=True)
                except Exception:
                    pass

    def cleanup(self):
        """Remove temporary segment workspace files."""
        if self.segments_dir.exists():
            try:
                shutil.rmtree(self.segments_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"[HLS:CLEANUP] Failed to remove {self.segments_dir}: {e}")
