import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from app.utils.security import sanitize_subprocess_arg

logger = logging.getLogger(__name__)


@dataclass
class VideoMetadata:
    width: int = 0
    height: int = 0
    display_width: int = 0
    display_height: int = 0
    duration: int = 0
    sar: str = "1:1"
    dar: str = ""
    rotation: int = 0
    codec_name: str = ""
    pix_fmt: str = ""
    bit_rate: Optional[int] = None
    container_format: str = ""
    is_landscape: bool = True
    is_portrait: bool = False
    is_square: bool = False
    is_widescreen_16_9: bool = False


def _is_valid_output_file(output_path: str) -> bool:
    try:
        p = Path(output_path)
        if not p.exists():
            return False
        try:
            return p.stat().st_size > 0
        except (OSError, FileNotFoundError):
            return True
    except Exception:
        return False


def _safe_unlink(output_path: str) -> None:
    try:
        p = Path(output_path)
        if p.exists():
            p.unlink(missing_ok=True)
    except Exception:
        pass


class FFmpegService:
    """Handles FFmpeg operations for video processing."""

    @staticmethod
    async def check_ffmpeg() -> bool:
        """Check if FFmpeg is available."""
        try:
            process = await asyncio.create_subprocess_exec(
                'ffmpeg', '-version',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            return process.returncode == 0
        except FileNotFoundError:
            return False

    @staticmethod
    async def merge_video_audio(
        video_path: str,
        audio_path: str,
        output_path: str,
        timeout: int = 3600,
    ) -> Tuple[bool, str]:
        """Merge separate video and audio files into a single MP4.

        Uses stream copy when possible to avoid re-encoding.
        """
        v_path = sanitize_subprocess_arg(video_path)
        a_path = sanitize_subprocess_arg(audio_path)
        o_path = sanitize_subprocess_arg(output_path)

        cmd = [
            'ffmpeg', '-y',
            '-i', v_path,
            '-i', a_path,
            '-c', 'copy',
            '-movflags', '+faststart',
            o_path
        ]

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )

            stderr_text = stderr.decode('utf-8', errors='replace')

            if process.returncode == 0 and _is_valid_output_file(output_path):
                return True, "Merge successful"

            # If copy failed, try with re-encoding
            logger.warning("Stream copy merge failed, trying with re-encoding...")
            return await FFmpegService._merge_with_reencode(
                video_path, audio_path, output_path, timeout
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if process:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass
            _safe_unlink(output_path)
            return False, "Merge operation timed out or was cancelled."
        except Exception as e:
            _safe_unlink(output_path)
            return False, f"Merge failed: {e}"

    @staticmethod
    async def _merge_with_reencode(
        video_path: str,
        audio_path: str,
        output_path: str,
        timeout: int = 3600,
    ) -> Tuple[bool, str]:
        """Merge with re-encoding as fallback."""
        cmd = [
            'ffmpeg', '-y',
            '-i', sanitize_subprocess_arg(video_path),
            '-i', sanitize_subprocess_arg(audio_path),
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-c:a', 'aac', '-b:a', '192k',
            '-movflags', '+faststart',
            sanitize_subprocess_arg(output_path)
        ]

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )

            if process.returncode == 0 and _is_valid_output_file(output_path):
                return True, "Merge successful (re-encoded)"

            stderr_text = stderr.decode('utf-8', errors='replace')[-500:]
            _safe_unlink(output_path)
            return False, f"Re-encode merge failed: {stderr_text}"
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if process:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass
            _safe_unlink(output_path)
            return False, "Re-encode merge timed out or was cancelled."
        except Exception as e:
            _safe_unlink(output_path)
            return False, f"Re-encode merge failed: {e}"

    @staticmethod
    async def get_video_info(file_path: str) -> Optional[dict]:
        """Get video information using ffprobe."""
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_format', '-show_streams',
            sanitize_subprocess_arg(file_path)
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=30
            )

            if process.returncode == 0:
                import json
                return json.loads(stdout.decode('utf-8'))
        except Exception as e:
            logger.error(f"ffprobe error: {e}")

        return None

    @staticmethod
    async def get_video_metadata(file_path: str) -> VideoMetadata:
        """Inspect video streams, dimensions, SAR, DAR, rotation, and display aspect ratio."""
        info = await FFmpegService.get_video_info(file_path)
        if not info:
            return VideoMetadata()

        # Find primary video stream (ignore attached pic / covers)
        v_stream = None
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "video":
                disposition = stream.get("disposition", {})
                if disposition.get("attached_pic") == 1:
                    continue
                v_stream = stream
                break

        # If no stream found without attached_pic, fallback to first video stream
        if not v_stream:
            for stream in info.get("streams", []):
                if stream.get("codec_type") == "video":
                    v_stream = stream
                    break

        if not v_stream:
            return VideoMetadata()

        raw_width = int(v_stream.get("width") or 0)
        raw_height = int(v_stream.get("height") or 0)
        codec_name = v_stream.get("codec_name", "")
        pix_fmt = v_stream.get("pix_fmt", "")
        sar = v_stream.get("sample_aspect_ratio") or "1:1"
        dar = v_stream.get("display_aspect_ratio") or ""

        # Duration
        duration = 0
        format_info = info.get("format", {})
        if "duration" in format_info:
            try:
                duration = int(round(float(format_info["duration"])))
            except (ValueError, TypeError):
                pass
        if duration <= 0 and "duration" in v_stream:
            try:
                duration = int(round(float(v_stream["duration"])))
            except (ValueError, TypeError):
                pass

        # Bitrate
        bit_rate = None
        if "bit_rate" in v_stream:
            try:
                bit_rate = int(v_stream["bit_rate"])
            except (ValueError, TypeError):
                pass
        elif "bit_rate" in format_info:
            try:
                bit_rate = int(format_info["bit_rate"])
            except (ValueError, TypeError):
                pass

        container_format = format_info.get("format_name", "")

        # Rotation
        rotation = 0
        tags = v_stream.get("tags", {})
        if "rotate" in tags:
            try:
                rotation = int(float(tags["rotate"])) % 360
            except (ValueError, TypeError):
                pass
        if rotation == 0:
            for side in v_stream.get("side_data_list", []):
                if "rotation" in side:
                    try:
                        rotation = int(float(side["rotation"])) % 360
                    except (ValueError, TypeError):
                        pass
        if rotation < 0:
            rotation += 360

        # Calculate display dimensions factoring in SAR / DAR
        calc_w, calc_h = raw_width, raw_height
        if raw_width > 0 and raw_height > 0:
            if sar and sar not in ("1:1", "0:1", "N/A"):
                try:
                    num_str, den_str = sar.split(":")
                    sar_num, sar_den = int(num_str), int(den_str)
                    if sar_num > 0 and sar_den > 0:
                        calc_w = int(round(raw_width * (sar_num / sar_den)))
                except Exception:
                    pass
            elif dar and dar not in ("0:1", "N/A"):
                try:
                    num_str, den_str = dar.split(":")
                    dar_num, dar_den = int(num_str), int(den_str)
                    if dar_num > 0 and dar_den > 0:
                        calc_w = int(round(raw_height * (dar_num / dar_den)))
                except Exception:
                    pass

        # Apply rotation to display dimensions
        if rotation in (90, 270):
            display_w = calc_h
            display_h = calc_w
        else:
            display_w = calc_w
            display_h = calc_h

        # Normalize to even dimensions for video encoding / telegram preview
        if display_w > 0 and display_w % 2 != 0:
            display_w += 1
        if display_h > 0 and display_h % 2 != 0:
            display_h += 1

        is_landscape = display_w > display_h if display_w > 0 and display_h > 0 else True
        is_portrait = display_h > display_w if display_w > 0 and display_h > 0 else False
        is_square = display_w == display_h if display_w > 0 and display_h > 0 else False

        is_widescreen_16_9 = False
        if display_w > 0 and display_h > 0:
            aspect = display_w / display_h
            is_widescreen_16_9 = (1.70 <= aspect <= 1.85)

        return VideoMetadata(
            width=raw_width,
            height=raw_height,
            display_width=display_w,
            display_height=display_h,
            duration=duration,
            sar=sar,
            dar=dar,
            rotation=rotation,
            codec_name=codec_name,
            pix_fmt=pix_fmt,
            bit_rate=bit_rate,
            container_format=container_format,
            is_landscape=is_landscape,
            is_portrait=is_portrait,
            is_square=is_square,
            is_widescreen_16_9=is_widescreen_16_9,
        )

    @staticmethod
    async def get_duration(file_path: str) -> int:
        """Get video duration in seconds."""
        meta = await FFmpegService.get_video_metadata(file_path)
        if meta and meta.duration > 0:
            return meta.duration
        info = await FFmpegService.get_video_info(file_path)
        if info and 'format' in info:
            try:
                return int(float(info['format'].get('duration', 0)))
            except (ValueError, TypeError):
                pass
        return 0

    @staticmethod
    async def generate_thumbnail(
        video_path: str,
        output_path: str,
        timestamp: Optional[str] = None,
        duration: int = 0,
        metadata: Optional[VideoMetadata] = None,
    ) -> bool:
        """Generate a proper aspect-ratio preserved JPEG thumbnail from video avoiding black frames."""
        v_path = sanitize_subprocess_arg(video_path)
        o_path = sanitize_subprocess_arg(output_path)

        # Calculate smart seek timestamp if not provided
        if not timestamp:
            vid_dur = duration or (metadata.duration if metadata else 0)
            if vid_dur <= 0:
                vid_dur = await FFmpegService.get_duration(video_path)

            if vid_dur >= 30:
                seek_sec = min(15, max(5, vid_dur // 10))
            elif vid_dur >= 10:
                seek_sec = 3
            elif vid_dur >= 3:
                seek_sec = 1
            else:
                seek_sec = 0

            hours = seek_sec // 3600
            mins = (seek_sec % 3600) // 60
            secs = seek_sec % 60
            timestamp = f"{hours:02d}:{mins:02d}:{secs:02d}"

        # Aspect-ratio preserving scale filter fitting within 320x320 box (Telegram max thumbnail size)
        # dar > 1 (landscape) -> 320x(320/dar) (e.g. 320x180 for 16:9)
        # dar <= 1 (portrait/square) -> (320*dar)x320 (e.g. 180x320 for 9:16, 320x320 for 1:1)
        scale_filter = "scale=w='if(gt(dar,1),320,trunc(320*dar/2)*2)':h='if(gt(dar,1),trunc(320/dar/2)*2,320)',setsar=1"

        # Pass 1: Try with thumbnail filter to pick the most representative, non-black frame
        cmd = [
            'ffmpeg', '-y',
            '-ss', timestamp,
            '-i', v_path,
            '-vf', f"thumbnail=24,{scale_filter}",
            '-vframes', '1',
            '-q:v', '2',
            '-update', '1',
            o_path
        ]

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode == 0 and _is_valid_output_file(output_path):
                # Ensure thumbnail is under 200 KB
                if Path(output_path).stat().st_size <= 200 * 1024:
                    return True
        except Exception:
            if process:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass

        # Pass 2: Fallback to direct frame capture at 1 second
        fallback_cmd = [
            'ffmpeg', '-y',
            '-ss', '00:00:01',
            '-i', v_path,
            '-vf', scale_filter,
            '-vframes', '1',
            '-q:v', '2',
            '-update', '1',
            o_path
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *fallback_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode == 0 and _is_valid_output_file(output_path):
                return True
        except Exception:
            if process:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass

        _safe_unlink(output_path)
        return False

    @staticmethod
    async def letterbox_to_16_9(
        input_path: str,
        output_path: str,
        target_width: int = 1280,
        target_height: int = 720,
        timeout: int = 3600,
    ) -> Tuple[bool, str]:
        """Optionally letterbox/pillarbox a video into a 16:9 canvas without cropping or stretching."""
        i_path = sanitize_subprocess_arg(input_path)
        o_path = sanitize_subprocess_arg(output_path)
        pad_filter = (
            f"scale=w='min({target_width},trunc({target_height}*dar/2)*2)':"
            f"h='min({target_height},trunc({target_width}/dar/2)*2)':force_original_aspect_ratio=decrease,"
            f"pad={target_width}:{target_height}:({target_width}-iw)/2:({target_height}-ih)/2:black,setsar=1"
        )
        cmd = [
            'ffmpeg', '-y',
            '-i', i_path,
            '-vf', pad_filter,
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
            '-c:a', 'copy',
            '-movflags', '+faststart',
            o_path
        ]
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=timeout)
            if process.returncode == 0 and _is_valid_output_file(output_path):
                return True, "Letterbox conversion successful"
            return False, "Letterbox conversion failed"
        except Exception as e:
            if process:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass
            _safe_unlink(output_path)
            return False, f"Letterbox error: {e}"
