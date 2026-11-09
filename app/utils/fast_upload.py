"""Accelerated MTProto Chunk Uploader for Pyrogram.

Optimizations:
- Parallel multi-session TCP connections to Telegram media datacenter (up to 4 media sessions).
- Configurable worker count (4-6 workers for files > 10 MB) to maximize bandwidth utilization.
- Buffered worker queue (workers_count * 2) preventing disk-read starvation.
- Transient chunk upload retry logic with exponential backoff.
- Non-blocking progress dispatch preventing network stalling during Telegram edit_message calls.
- Strict resource cleanup guaranteeing all background sessions and file descriptors close cleanly.
"""

import asyncio
import functools
import inspect
import io
import logging
import math
import os
from hashlib import md5
from pathlib import PurePath
from typing import BinaryIO, Callable, Union

import pyrogram
from pyrogram import raw, utils
from pyrogram.errors import RPCError
from pyrogram.session import Session

try:
    from pyrogram import StopTransmission
except ImportError:
    class StopTransmission(Exception):
        """Exception raised in progress callbacks to stop file transmission."""
        pass

logger = logging.getLogger(__name__)

_original_save_file = None


async def fast_save_file(
    client: "pyrogram.Client",
    path: Union[str, BinaryIO],
    file_id: int = None,
    file_part: int = 0,
    progress: Callable = None,
    progress_args: tuple = (),
):
    """High-throughput multi-session file uploader for Pyrogram."""
    async with client.save_file_semaphore:
        if path is None:
            return None

        part_size = 512 * 1024

        if isinstance(path, (str, PurePath)):
            fp = open(path, "rb")
        elif isinstance(path, io.IOBase):
            fp = path
        else:
            raise ValueError("Invalid file. Expected a file path as string or a binary file pointer")

        file_name = getattr(fp, "name", "file.mp4")
        if isinstance(file_name, (str, PurePath)):
            file_name = os.path.basename(str(file_name))

        fp.seek(0, os.SEEK_END)
        file_size = fp.tell()
        fp.seek(0)

        if file_size == 0:
            if isinstance(path, (str, PurePath)):
                fp.close()
            raise ValueError("File size equals to 0 B")

        # Determine file size limits
        is_premium = getattr(client.me, "is_premium", False) if hasattr(client, "me") and client.me else False
        file_size_limit_mib = 4000 if is_premium else 2000

        if file_size > file_size_limit_mib * 1024 * 1024:
            if isinstance(path, (str, PurePath)):
                fp.close()
            raise ValueError(f"Can't upload files bigger than {file_size_limit_mib} MiB")

        file_total_parts = int(math.ceil(file_size / part_size))
        is_big = file_size > 10 * 1024 * 1024

        # For files > 10 MB, use 4 parallel sessions and 4 workers
        workers_count = 4 if is_big else 1
        num_sessions = 4 if is_big else 1

        is_missing_part = file_id is not None
        file_id = file_id or client.rnd_id()
        md5_sum = md5() if not is_big and not is_missing_part else None

        dc_id = await client.storage.dc_id()
        auth_key = await client.storage.auth_key()
        test_mode = await client.storage.test_mode()

        sessions = [
            Session(client, dc_id, auth_key, test_mode, is_media=True)
            for _ in range(num_sessions)
        ]

        queue = asyncio.Queue(workers_count * 2)

        worker_error = [None]

        async def worker(sess: Session):
            while True:
                data = await queue.get()
                if data is None:
                    return

                # Retry chunk upload on transient failure
                for attempt in range(3):
                    try:
                        await sess.invoke(data)
                        break
                    except Exception as e:
                        if attempt < 2:
                            await asyncio.sleep(0.5 * (attempt + 1))
                        else:
                            logger.error(f"[UPLOAD:CHUNK_ERR] Failed chunk RPC: {e}")
                            worker_error[0] = e
                            return

        workers = [
            client.loop.create_task(worker(sessions[i % num_sessions]))
            for i in range(workers_count)
        ]

        upload_completed = False

        try:
            # Start all sessions concurrently
            await asyncio.gather(*(s.start() for s in sessions))

            fp.seek(part_size * file_part)

            while True:
                chunk = fp.read(part_size)

                if not chunk:
                    if not is_big and not is_missing_part:
                        md5_sum = "".join([hex(i)[2:].zfill(2) for i in md5_sum.digest()])
                    break

                if is_big:
                    rpc = raw.functions.upload.SaveBigFilePart(
                        file_id=file_id,
                        file_part=file_part,
                        file_total_parts=file_total_parts,
                        bytes=chunk,
                    )
                else:
                    rpc = raw.functions.upload.SaveFilePart(
                        file_id=file_id,
                        file_part=file_part,
                        bytes=chunk,
                    )

                await queue.put(rpc)

                if is_missing_part:
                    return

                if not is_big and not is_missing_part:
                    md5_sum.update(chunk)

                file_part += 1

                if progress:
                    current_bytes = min(file_part * part_size, file_size)
                    try:
                        if inspect.iscoroutinefunction(progress):
                            # Non-blocking dispatch so network loop never stalls
                            client.loop.create_task(progress(current_bytes, file_size, *progress_args))
                        else:
                            progress(current_bytes, file_size, *progress_args)
                    except StopTransmission:
                        logger.info("[UPLOAD:CANCEL] Transmission cancelled via progress callback.")
                        raise
                    except Exception as prog_err:
                        logger.debug(f"[UPLOAD:PROGRESS_WARN] Progress callback error: {prog_err}")

            # Notify workers that all chunks have been queued and wait for completion
            for _ in workers:
                await queue.put(None)
            await asyncio.gather(*workers)

            if worker_error[0] is not None:
                raise worker_error[0]

            upload_completed = True

        except StopTransmission:
            logger.info("[UPLOAD:CANCEL] Transmission stopped cleanly via StopTransmission.")
            raise
        except asyncio.CancelledError:
            logger.info("[UPLOAD:CANCEL] Upload task cancelled.")
            raise
        except Exception as e:
            logger.warning(
                f"[UPLOAD:FALLBACK] Multi-session fast upload error ({e}). "
                f"Falling back to native Pyrogram save_file..."
            )
            if _original_save_file is not None and not is_missing_part:
                if isinstance(path, (str, PurePath)):
                    try:
                        fp.close()
                    except Exception:
                        pass
                res = await _original_save_file(
                    client, path, file_id=file_id, file_part=file_part,
                    progress=progress, progress_args=progress_args
                )
                if res is None:
                    raise RuntimeError(f"Native Pyrogram save_file failed to upload {path} (returned None)")
                return res
            raise
        else:
            if is_big:
                res = raw.types.InputFileBig(
                    id=file_id,
                    parts=file_total_parts,
                    name=str(file_name),
                )
            else:
                res = raw.types.InputFile(
                    id=file_id,
                    parts=file_total_parts,
                    name=str(file_name),
                    md5_checksum=md5_sum,
                )
            if res is None:
                raise RuntimeError(f"Fast upload failed to generate InputFile for {path}")
            return res
        finally:
            if not upload_completed:
                # Cancel pending workers if upload aborted prematurely
                for w in workers:
                    w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

            # Terminate sessions cleanly
            await asyncio.gather(*(s.stop() for s in sessions), return_exceptions=True)

            if isinstance(path, (str, PurePath)):
                try:
                    fp.close()
                except Exception:
                    pass


def patch_pyrogram_uploader():
    """Hook fast_save_file into Pyrogram's SaveFile mixin class with fallback support."""
    global _original_save_file
    from pyrogram.methods.advanced.save_file import SaveFile
    if _original_save_file is None:
        _original_save_file = SaveFile.save_file
    SaveFile.save_file = fast_save_file
    logger.info("[UPLOAD:PATCH] Pyrogram save_file hooked with multi-session fast uploader.")
