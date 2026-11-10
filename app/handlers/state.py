from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from app.services.bunny_extractor import ExtractionResult, VideoFormat


class FlowState:
    WAITING_FOR_URL = "WAITING_FOR_URL"
    SELECTING_QUALITY = "SELECTING_QUALITY"
    WAITING_FOR_CAPTION = "WAITING_FOR_CAPTION"
    CONFIRMING_DOWNLOAD = "CONFIRMING_DOWNLOAD"
    PROCESSING = "PROCESSING"


@dataclass
class PendingJob:
    user_id: int
    state: str = FlowState.SELECTING_QUALITY
    extraction: Optional[ExtractionResult] = None
    selected_format: Optional[VideoFormat] = None
    quality_label: str = ""
    caption: Optional[str] = None
    caption_entities: Optional[List[Any]] = None
    prompt_message_id: Optional[int] = None
    chat_id: Optional[int] = None
    candidate_url: Optional[str] = None


# Thread/task-safe in-memory storage of pending flows per user
_pending_jobs: Dict[int, PendingJob] = {}


def get_pending_job(user_id: int) -> Optional[PendingJob]:
    """Retrieve pending job state for a user."""
    return _pending_jobs.get(user_id)


def set_pending_job(user_id: int, job: PendingJob) -> None:
    """Set pending job state for a user."""
    _pending_jobs[user_id] = job


def clear_pending_job(user_id: int) -> Optional[PendingJob]:
    """Clear pending job state for a user."""
    return _pending_jobs.pop(user_id, None)


def get_user_state(user_id: int) -> Optional[str]:
    """Get current flow state for a user."""
    job = _pending_jobs.get(user_id)
    return job.state if job else None


def set_user_state(user_id: int, state: str) -> None:
    """Set flow state for an existing pending job."""
    job = _pending_jobs.get(user_id)
    if job:
        job.state = state
