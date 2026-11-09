import asyncio
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import Message

try:
    from pyrogram import StopTransmission
except ImportError:
    class StopTransmission(Exception):
        pass

from app.services.ffmpeg_service import FFmpegService
from app.utils.formatters import sanitize_filename
from app.utils.validators import check_file_size

logger = logging.getLogger(__name__)


class UploadService:
    """Handles uploading files to Telegram with timeouts, validation, aspect ratio, and metadata."""

    def __init__(self, max_file_size_bytes: int, timeout: int = 1800):
        self.max_file_size_bytes = max_file_size_bytes
        self.timeout = timeout

    async def upload_video(
        self,
        client: Client,
        chat_id: int,
        file_path: str,
        title: str = "Video",
        duration: int = 0,
        width: int = 0,
        height: int = 0,
        progress_callback: Optional[Callable] = None,
        reply_to_message_id: int = None,
        caption: Optional[str] = None,
        caption_entities: Optional[list] = None,
    ) -> Optional[Message]:
        """Upload video to Telegram with accurate aspect ratio, dimensions, thumbnail, and timeout."""
        path = Path(file_path)

        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = path.stat().st_size
        if file_size == 0:
            raise ValueError("Cannot upload an empty video file (0 bytes).")

        # Check file size
        is_valid, msg = check_file_size(file_path, self.max_file_size_bytes)
        if not is_valid:
            raise ValueError(msg)

        # Probe video metadata for exact dimensions and aspect ratio if not passed
        metadata = None
        if width <= 0 or height <= 0 or duration <= 0:
            metadata = await FFmpegService.get_video_metadata(file_path)
            if duration <= 0:
                duration = metadata.duration
            if width <= 0:
                width = metadata.display_width
            if height <= 0:
                height = metadata.display_height

        # Fallback duration if metadata had 0
        if duration <= 0:
            duration = await FFmpegService.get_duration(file_path)

        # Generate aspect-ratio preserving thumbnail
        thumb_path = str(path.parent / f"{path.stem}_thumb.jpg")
        has_thumb = await FFmpegService.generate_thumbnail(
            file_path, thumb_path, duration=duration, metadata=metadata
        )
        if has_thumb and (not Path(thumb_path).exists() or Path(thumb_path).stat().st_size == 0):
            has_thumb = False

        safe_name = sanitize_filename(title) + ".mp4"

        final_caption = caption if caption is not None else f"🎬 <b>{title}</b>"
        caption_kwargs = {"caption": final_caption}
        if caption_entities:
            caption_kwargs["caption_entities"] = caption_entities
        else:
            caption_kwargs["parse_mode"] = ParseMode.HTML

        send_video_kwargs = {
            "chat_id": chat_id,
            "video": file_path,
            "duration": duration,
            "thumb": thumb_path if has_thumb else None,
            "file_name": safe_name,
            "supports_streaming": True,
            "progress": progress_callback,
            "reply_to_message_id": reply_to_message_id,
            **caption_kwargs,
        }
        if width > 0:
            send_video_kwargs["width"] = width
        if height > 0:
            send_video_kwargs["height"] = height

        try:
            logger.info(
                f"Starting Telegram video upload for {safe_name} ({file_size} bytes, "
                f"dimensions={width}x{height}, duration={duration}s, has_thumb={has_thumb})"
            )
            message = await asyncio.wait_for(
                client.send_video(**send_video_kwargs),
                timeout=self.timeout
            )
            if message is None:
                raise RuntimeError("Telegram video upload failed: send_video returned None")
            return message
        except (StopTransmission, asyncio.CancelledError):
            logger.info(f"Telegram video upload cancelled for {safe_name}.")
            raise
        except asyncio.TimeoutError:
            logger.error(f"Telegram video upload timed out after {self.timeout}s.")
            raise TimeoutError(f"Telegram upload timed out after {self.timeout}s. The network connection may be slow.")
        except Exception as e:
            logger.warning(f"Video upload failed, trying as document fallback: {e}")
            # Fallback to document upload
            try:
                message = await asyncio.wait_for(
                    client.send_document(
                        chat_id=chat_id,
                        document=file_path,
                        file_name=safe_name,
                        thumb=thumb_path if has_thumb else None,
                        progress=progress_callback,
                        reply_to_message_id=reply_to_message_id,
                        **caption_kwargs,
                    ),
                    timeout=self.timeout
                )
                if message is None:
                    raise RuntimeError("Telegram document upload failed: send_document returned None")
                return message
            except (StopTransmission, asyncio.CancelledError):
                logger.info(f"Telegram document fallback upload cancelled for {safe_name}.")
                raise
            except asyncio.TimeoutError:
                logger.error(f"Telegram document fallback upload timed out after {self.timeout}s.")
                raise TimeoutError(f"Telegram upload timed out after {self.timeout}s.")
            except Exception as e2:
                logger.error(f"Document upload also failed: {e2}")
                raise
        finally:
            # Clean up thumbnail
            if has_thumb:
                try:
                    Path(thumb_path).unlink(missing_ok=True)
                except Exception:
                    pass

    async def upload_as_document(
        self,
        client: Client,
        chat_id: int,
        file_path: str,
        title: str = "Video",
        progress_callback: Optional[Callable] = None,
        caption: Optional[str] = None,
        caption_entities: Optional[list] = None,
    ) -> Optional[Message]:
        """Upload file as a document."""
        safe_name = sanitize_filename(title) + ".mp4"
        final_caption = caption if caption is not None else f"📁 <b>{title}</b>"
        caption_kwargs = {"caption": final_caption}
        if caption_entities:
            caption_kwargs["caption_entities"] = caption_entities
        else:
            caption_kwargs["parse_mode"] = ParseMode.HTML

        try:
            message = await asyncio.wait_for(
                client.send_document(
                    chat_id=chat_id,
                    document=file_path,
                    file_name=safe_name,
                    progress=progress_callback,
                    **caption_kwargs,
                ),
                timeout=self.timeout
            )
            if message is None:
                raise RuntimeError("Telegram document upload failed: send_document returned None")
            return message
        except (StopTransmission, asyncio.CancelledError):
            logger.info(f"Telegram document upload cancelled for {safe_name}.")
            raise
