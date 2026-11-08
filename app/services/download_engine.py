import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yt_dlp

from app.services.bunny_extractor import ExtractionResult, VideoFormat, AudioFormat
from app.utils.formatters import sanitize_filename
from app.utils.security import sanitize_subprocess_arg

logger = logging.getLogger(__name__)


@dataclass
class DownloadProgress:
    status: str = "starting"
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    eta: Optional[float] = None
    percent: float = 0.0
    filename: str = ""


@dataclass
class DownloadResult:
    success: bool = False
    video_path: Optional[str] = None
    audio_path: Optional[str] = None
    merged_path: Optional[str] = None
    output_path: Optional[str] = None
    file_size: int = 0
    duration: int = 0
    title: str = ""
    error: Optional[str] = None
    needs_merge: bool = False


class DownloadEngine:
    """Handles downloading video and audio streams with timeouts and subprocess lifecycle management."""

    def __init__(self, download_dir: str, temp_dir: str, timeout: int = 3600, job_id: Optional[str] = None):
        self.download_dir = Path(download_dir)
        self.temp_dir = Path(temp_dir)
        self.timeout = timeout
        self.job_id = job_id
        self._cancelled = False
        self._current_process: Optional[asyncio.subprocess.Process] = None

        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        if self.job_id:
            self.workspace = self.temp_dir / f"job_{self.job_id}"
            self.workspace.mkdir(parents=True, exist_ok=True)
        else:
            self.workspace = self.temp_dir

    def cancel(self):
        """Cancel the current download and terminate running subprocess."""
        self._cancelled = True
        if self._current_process:
            try:
                self._current_process.kill()
                logger.info("Subprocess killed due to download cancellation.")
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.warning(f"Error terminating download subprocess: {e}")

    async def download(
        self,
        extraction: ExtractionResult,
        selected_format: VideoFormat,
        audio_format: Optional[AudioFormat] = None,
        progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    ) -> DownloadResult:
        """Download video (and audio if separate)."""
        self._cancelled = False
        result = DownloadResult(
            title=extraction.title,
            duration=extraction.duration,
        )

        job_id = self.job_id or str(uuid.uuid4())[:8]
        safe_title = sanitize_filename(extraction.title or "video")
        referer = getattr(extraction, "referer", None) or "https://iframe.mediadelivery.net/"
        if not referer or not referer.startswith("http"):
            referer = "https://iframe.mediadelivery.net/"

        try:
            if selected_format.url.endswith('.m3u8') or 'hls' in selected_format.format_id or '.m3u8' in selected_format.url:
                # HLS download via FFmpeg
                result = await self._download_hls(
                    selected_format.url, safe_title, job_id, result, progress_callback, referer=referer
                )
            elif extraction.direct_url:
                # Direct download
                result = await self._download_direct(
                    extraction.direct_url, safe_title, job_id, result, progress_callback, referer=referer
                )
            else:
                # Download via yt-dlp
                result = await self._download_ytdlp(
                    extraction, selected_format, audio_format,
                    safe_title, job_id, result, progress_callback, referer=referer
                )

            if self._cancelled:
                result.success = False
                result.error = "Download cancelled by user."
                if result.output_path and Path(result.output_path).exists():
                    try:
                        Path(result.output_path).unlink(missing_ok=True)
                    except Exception:
                        pass
        except (asyncio.TimeoutError, TimeoutError):
            result.success = False
            result.error = f"Download timed out after {self.timeout} seconds."
            if result.output_path and Path(result.output_path).exists():
                try:
                    Path(result.output_path).unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Download error: {e}", exc_info=True)
            result.success = False
            result.error = f"Download failed: {str(e)}"

        return result

    async def _download_ytdlp(
        self, extraction: ExtractionResult, video_fmt: VideoFormat,
        audio_fmt: Optional[AudioFormat], safe_title: str, job_id: str,
        result: DownloadResult, progress_cb: Optional[Callable],
        referer: str = "https://iframe.mediadelivery.net/"
    ) -> DownloadResult:
        """Download using yt-dlp."""
        output_template = str(self.workspace / f"{job_id}_{safe_title}.%(ext)s")
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        # Build format selection
        if video_fmt.has_audio:
            format_sel = video_fmt.format_id
        elif audio_fmt:
            format_sel = f"{video_fmt.format_id}+{audio_fmt.format_id}"
            result.needs_merge = True
        else:
            format_sel = video_fmt.format_id

        progress_data = DownloadProgress()

        def _progress_hook(d):
            if self._cancelled:
                raise yt_dlp.utils.DownloadCancelled()

            if d['status'] == 'downloading':
                progress_data.status = 'downloading'
                progress_data.downloaded_bytes = d.get('downloaded_bytes', 0) or 0
                progress_data.total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                progress_data.speed = d.get('speed') or 0
                progress_data.eta = d.get('eta')
                if progress_data.total_bytes > 0:
                    progress_data.percent = (
                        progress_data.downloaded_bytes / progress_data.total_bytes * 100
                    )
                progress_data.filename = d.get('filename', '')

                if progress_cb:
                    progress_cb(progress_data)

            elif d['status'] == 'finished':
                progress_data.status = 'finished'
                progress_data.percent = 100
                if progress_cb:
                    progress_cb(progress_data)

        ydl_opts = {
            'format': format_sel,
            'outtmpl': output_template,
            'merge_output_format': 'mp4',
            'progress_hooks': [_progress_hook],
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': False,
            'socket_timeout': 30,
            'retries': 3,
            'fragment_retries': 5,
            'http_headers': {
                'Referer': referer,
                'User-Agent': user_agent,
                'Origin': 'https://iframe.mediadelivery.net',
            },
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }] if result.needs_merge else [],
        }

        loop = asyncio.get_event_loop()

        def _do_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_fmt.url or extraction.direct_url or ''])

        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _do_download),
                timeout=self.timeout
            )
        except yt_dlp.utils.DownloadCancelled:
            result.error = "Download cancelled."
            return result

        # Find the output file
        output_path = await self._find_output(job_id, safe_title)
        if output_path and output_path.stat().st_size > 0:
            result.success = True
            result.output_path = str(output_path)
            result.file_size = output_path.stat().st_size
        else:
            result.error = "Download completed but output file not found or is empty."

        return result

    async def _download_hls(
        self, m3u8_url: str, safe_title: str, job_id: str,
        result: DownloadResult, progress_cb: Optional[Callable],
        referer: str = "https://iframe.mediadelivery.net/"
    ) -> DownloadResult:
        """Download HLS stream using FFmpeg with required headers and safe cancellation."""
        output_path = self.workspace / f"{job_id}_{safe_title}.mp4"
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        headers_str = f"Referer: {referer}\r\nUser-Agent: {user_agent}\r\nOrigin: https://iframe.mediadelivery.net\r\n"

        # Build base command with input HTTP headers before -i
        base_input_args = [
            '-headers', headers_str,
            '-referer', sanitize_subprocess_arg(referer),
            '-user_agent', sanitize_subprocess_arg(user_agent),
            '-i', sanitize_subprocess_arg(m3u8_url),
        ]

        cmd = [
            'ffmpeg', '-y',
            *base_input_args,
            '-c', 'copy',
            '-bsf:a', 'aac_adtstoasc',
            '-movflags', '+faststart',
            str(output_path)
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._current_process = process

            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )

            if self._cancelled:
                result.error = "Download cancelled."
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except Exception:
                        pass
                return result

            # If bsf:a copy failed, try copy without bsf:a
            if process.returncode != 0 and (not output_path.exists() or output_path.stat().st_size == 0):
                stderr_text = stderr.decode('utf-8', errors='replace')
                if "403" not in stderr_text and "forbidden" not in stderr_text.lower():
                    logger.warning("HLS download with bsf failed, retrying copy without bsf:a...")
                    cmd_no_bsf = [
                        'ffmpeg', '-y',
                        *base_input_args,
                        '-c', 'copy',
                        '-movflags', '+faststart',
                        str(output_path)
                    ]
                    process_retry = await asyncio.create_subprocess_exec(
                        *cmd_no_bsf,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    self._current_process = process_retry
                    stdout, stderr = await asyncio.wait_for(
                        process_retry.communicate(), timeout=self.timeout
                    )
                    process = process_retry

            if process.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                result.success = True
                result.output_path = str(output_path)
                result.file_size = output_path.stat().st_size
            else:
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except Exception:
                        pass
                stderr_text = stderr.decode('utf-8', errors='replace')[-500:]
                if "403" in stderr_text or "forbidden" in stderr_text.lower():
                    result.error = (
                        "Access denied (403 Forbidden). "
                        "The Bunny Stream media source requires token authorization from the content owner."
                    )
                else:
                    result.error = f"FFmpeg download failed: {stderr_text}"
        except (asyncio.TimeoutError, TimeoutError):
            if self._current_process:
                try:
                    self._current_process.kill()
                except Exception:
                    pass
            if output_path.exists():
                try:
                    output_path.unlink()
                except Exception:
                    pass
            result.error = "HLS download timed out."
        finally:
            self._current_process = None

        return result

    async def _download_direct(
        self, url: str, safe_title: str, job_id: str,
        result: DownloadResult, progress_cb: Optional[Callable],
        referer: str = "https://iframe.mediadelivery.net/"
    ) -> DownloadResult:
        """Download a direct MP4 URL."""
        import httpx

        output_path = self.workspace / f"{job_id}_{safe_title}.mp4"
        progress_data = DownloadProgress(status='downloading')
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30, read=self.timeout),
                follow_redirects=True, verify=True,
                headers={
                    "Referer": referer,
                    "User-Agent": user_agent,
                    "Origin": "https://iframe.mediadelivery.net",
                }
            ) as client:
                async with client.stream('GET', url) as resp:
                    if resp.status_code != 200:
                        result.error = f"Download failed with status {resp.status_code}"
                        return result

                    total = int(resp.headers.get('content-length', 0))
                    progress_data.total_bytes = total
                    downloaded = 0
                    start_time = time.monotonic()

                    with open(output_path, 'wb') as f:
                        async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                            if self._cancelled:
                                result.error = "Download cancelled."
                                break

                            f.write(chunk)
                            downloaded += len(chunk)

                            elapsed = time.monotonic() - start_time
                            progress_data.downloaded_bytes = downloaded
                            progress_data.speed = downloaded / elapsed if elapsed > 0 else 0
                            if total > 0:
                                progress_data.percent = downloaded / total * 100
                                remaining = total - downloaded
                                progress_data.eta = remaining / progress_data.speed if progress_data.speed > 0 else None

                            if progress_cb:
                                progress_cb(progress_data)

            if self._cancelled:
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except Exception:
                        pass
                result.error = "Download cancelled."
                return result

            if output_path.exists() and output_path.stat().st_size > 0:
                result.success = True
                result.output_path = str(output_path)
                result.file_size = output_path.stat().st_size
            else:
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except Exception:
                        pass
                result.error = "Downloaded file is empty or missing."
        except Exception as e:
            if output_path.exists():
                try:
                    output_path.unlink()
                except Exception:
                    pass
            result.error = f"Direct download failed: {e}"

        return result

    async def _find_output(self, job_id: str, safe_title: str) -> Optional[Path]:
        """Find the output file from yt-dlp download."""
        for ext in ['mp4', 'mkv', 'webm', 'm4a']:
            path = self.workspace / f"{job_id}_{safe_title}.{ext}"
            if path.exists() and path.stat().st_size > 0:
                return path

        # Search for any file with the job_id prefix
        for path in self.workspace.glob(f"{job_id}_*"):
            if path.is_file() and path.stat().st_size > 0:
                return path

        return None

    def cleanup(self, *paths: str):
        """Clean up temporary files."""
        for path in paths:
            if path:
                try:
                    p = Path(path)
                    if p.exists():
                        p.unlink()
                        logger.debug(f"Cleaned up: {path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up {path}: {e}")
