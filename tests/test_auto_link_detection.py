"""Automated regression test suite for v1.0.2 Smart Auto-Link Detection."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyrogram import Client
from pyrogram.enums import MessageEntityType, ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    User as TgUser,
    Chat,
)

from app.database.models import init_db
from app.database.repository import JobRepository, UserRepository
from app.handlers.download import (
    _pending_extractions,
    _waiting_for_url,
    register as register_download,
)
from app.handlers.quality import register as register_quality
from app.handlers.start import register as register_start
from app.handlers.state import (
    FlowState,
    PendingJob,
    _pending_jobs,
    clear_pending_job,
    get_pending_job,
    set_pending_job,
)
from app.services.bunny_extractor import ExtractionResult, VideoFormat
from app.services.queue_manager import QueueManager
from config import Config

VALID_BUNNY_URL = (
    "https://iframe.mediadelivery.net/embed/346396/af5aa834-acd6-47bc-a800-32ad1a8f7d92"
    "?autoplay=false&token=secure123"
)
SECOND_BUNNY_URL = (
    "https://iframe.mediadelivery.net/embed/346396/bbbbbbbb-1111-2222-3333-444444444444"
    "?token=token_video_2"
)


def create_test_app(tmp_path: Path) -> Client:
    """Create a configured test Client instance."""
    app = Client("test_auto_link", api_id=123, api_hash="hash", bot_token="token")
    cfg = Config(
        BOT_TOKEN="token",
        API_ID=123,
        API_HASH="hash",
        OWNER_ID=1,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        MAX_CONCURRENT_DOWNLOADS=2,
        MAX_DOWNLOADS_PER_USER=1,
        TEMP_DIRECTORY=str(tmp_path / "temp"),
        DOWNLOAD_DIRECTORY=str(tmp_path / "downloads"),
    )
    app.config = cfg
    app.queue_manager = QueueManager(cfg)
    return app


async def setup_app_and_db(tmp_path: Path) -> Client:
    """Create test client and initialize its isolated database."""
    app = create_test_app(tmp_path)
    await init_db(app.config.DATABASE_URL)
    return app


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure in-memory states are fresh before and after each test."""
    _pending_jobs.clear()
    _pending_extractions.clear()
    _waiting_for_url.clear()
    yield
    _pending_jobs.clear()
    _pending_extractions.clear()
    _waiting_for_url.clear()


def _get_handler(app: Client, handler_name: str):
    """Retrieve registered handler callback by function name."""
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback") and handler.callback.__name__ == handler_name:
            return handler.callback
    return None


# ==============================================================================
# 1. Mode B: Direct Link Without /start
# ==============================================================================

@pytest.mark.asyncio
async def test_direct_link_without_start_command(tmp_path):
    """Verify Mode B: pasting a valid direct link without /start automatically registers user,
    displays the required premium detection message, extracts formats, and displays qualities.
    """
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    await asyncio.sleep(0.01)

    url_handler = _get_handler(app, "handle_url_message")
    assert url_handler is not None

    user_id = 998811
    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(spec=TgUser)
    mock_msg.from_user.id = user_id
    mock_msg.from_user.username = "directuser"
    mock_msg.from_user.first_name = "Direct"
    mock_msg.from_user.last_name = "User"
    mock_msg.text = VALID_BUNNY_URL
    mock_msg.entities = None
    mock_msg.chat = MagicMock(spec=Chat)
    mock_msg.chat.id = user_id

    # The status message that gets sent and then edited
    status_msg = AsyncMock(spec=Message)
    status_msg.edit_text = AsyncMock()
    mock_msg.reply = AsyncMock(return_value=status_msg)

    fake_result = ExtractionResult(
        success=True,
        title="Direct Video Lesson",
        duration=360,
        video_formats=[
            VideoFormat(format_id="720p", quality="720p", height=720, url="https://cdn.net/720.mp4"),
            VideoFormat(format_id="1080p", quality="1080p", height=1080, url="https://cdn.net/1080.mp4"),
        ]
    )

    with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = fake_result
        await url_handler(app, mock_msg)

    # 1. User was registered without /start
    all_user_ids = await UserRepository.get_all_ids()
    assert user_id in all_user_ids

    # 2. Immediate response contains exact required text
    mock_msg.reply.assert_called_once()
    initial_text = mock_msg.reply.call_args.args[0]
    assert "Video Link Detected!" in initial_text
    assert "Analyzing video..." in initial_text
    assert "Fetching available qualities..." in initial_text

    # 3. Message was edited to show quality keyboard
    status_msg.edit_text.assert_called_once()
    quality_text = status_msg.edit_text.call_args.args[0]
    assert "VIDEO DETECTED" in quality_text
    assert "Direct Video Lesson" in quality_text
    assert "720p" in str(status_msg.edit_text.call_args.kwargs.get("reply_markup"))

    # 4. Pending job state is SELECTING_QUALITY
    pending = get_pending_job(user_id)
    assert pending is not None
    assert pending.state == FlowState.SELECTING_QUALITY
    assert pending.extraction == fake_result


# ==============================================================================
# 2. Mode A: Classic Download Workflow Compatibility
# ==============================================================================

@pytest.mark.asyncio
async def test_classic_download_workflow_mode_a(tmp_path):
    """Verify Mode A (/start -> Download Video -> send URL) remains fully functional."""
    app = await setup_app_and_db(tmp_path)
    register_start(app)
    register_download(app)
    await asyncio.sleep(0.01)

    dl_callback_handler = _get_handler(app, "download_video_callback")
    url_handler = _get_handler(app, "handle_url_message")
    assert dl_callback_handler is not None
    assert url_handler is not None

    user_id = 776655
    mock_callback = AsyncMock(spec=CallbackQuery)
    mock_callback.from_user = MagicMock(id=user_id, username="classicuser", first_name="Classic", last_name=None)
    mock_callback.from_user.id = user_id
    mock_callback.message = AsyncMock(spec=Message)
    mock_callback.message.chat = MagicMock(spec=Chat)
    mock_callback.message.chat.id = user_id
    mock_callback.message.edit_text = AsyncMock()
    mock_callback.answer = AsyncMock()

    # User clicks 'Download Video'
    await dl_callback_handler(app, mock_callback)
    mock_callback.message.edit_text.assert_called_once()
    assert "SEND VIDEO URL" in mock_callback.message.edit_text.call_args.args[0]
    assert _waiting_for_url.get(user_id) is True

    # User sends URL in response
    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = mock_callback.from_user
    mock_msg.chat = MagicMock(spec=Chat)
    mock_msg.chat.id = user_id
    mock_msg.text = VALID_BUNNY_URL
    mock_msg.entities = None

    status_msg = AsyncMock(spec=Message)
    status_msg.edit_text = AsyncMock()
    mock_msg.reply = AsyncMock(return_value=status_msg)

    fake_result = ExtractionResult(
        success=True,
        title="Classic Mode Video",
        duration=180,
        video_formats=[
            VideoFormat(format_id="720p", quality="720p", height=720, url="https://cdn.net/720.mp4")
        ]
    )

    with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = fake_result
        await url_handler(app, mock_msg)

    # Classic prompt used
    mock_msg.reply.assert_called_once()
    assert "ANALYZING VIDEO" in mock_msg.reply.call_args.args[0]
    assert status_msg.edit_text.called
    assert _waiting_for_url.get(user_id, False) is False


# ==============================================================================
# 3. URL Entities and Surrounding Text
# ==============================================================================

@pytest.mark.asyncio
async def test_url_with_surrounding_text_and_entities(tmp_path):
    """Verify URLs embedded within surrounding text or provided via Telegram URL entities are parsed."""
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    await asyncio.sleep(0.01)

    url_handler = _get_handler(app, "handle_url_message")

    # Surrounding conversational text
    user_id = 445566
    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(spec=TgUser)
    mock_msg.from_user.id = user_id
    mock_msg.from_user.username = "textuser"
    mock_msg.from_user.first_name = "Text"
    mock_msg.from_user.last_name = None
    mock_msg.chat = MagicMock(spec=Chat)
    mock_msg.chat.id = user_id
    mock_msg.text = (
        f"Hi, please download this video for my homework: {VALID_BUNNY_URL}. Thanks!"
    )
    mock_msg.entities = None

    status_msg = AsyncMock(spec=Message)
    status_msg.edit_text = AsyncMock()
    mock_msg.reply = AsyncMock(return_value=status_msg)

    fake_result = ExtractionResult(
        success=True,
        title="Surrounding Text Video",
        duration=120,
        video_formats=[VideoFormat(format_id="480p", quality="480p", height=480, url="https://cdn.net/480.mp4")]
    )

    with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = fake_result
        await url_handler(app, mock_msg)

    # Extractor received the normalized clean URL without surrounding text or trailing period
    called_url = mock_extract.call_args.args[0]
    assert called_url == VALID_BUNNY_URL


@pytest.mark.asyncio
async def test_url_via_telegram_text_link_entity(tmp_path):
    """Verify hyperlinked text (TEXT_LINK entity) triggers auto-link detection."""
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    await asyncio.sleep(0.01)

    url_handler = _get_handler(app, "handle_url_message")

    user_id = 223344
    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(spec=TgUser)
    mock_msg.from_user.id = user_id
    mock_msg.from_user.username = "linkuser"
    mock_msg.from_user.first_name = "Link"
    mock_msg.from_user.last_name = None
    mock_msg.chat = MagicMock(spec=Chat)
    mock_msg.chat.id = user_id
    mock_msg.text = "Click here to watch lecture"
    mock_msg.entities = [
        MessageEntity(
            type=MessageEntityType.TEXT_LINK,
            offset=0,
            length=27,
            url=VALID_BUNNY_URL,
        )
    ]

    status_msg = AsyncMock(spec=Message)
    status_msg.edit_text = AsyncMock()
    mock_msg.reply = AsyncMock(return_value=status_msg)

    fake_result = ExtractionResult(
        success=True,
        title="Hyperlink Video",
        video_formats=[VideoFormat(format_id="720p", quality="720p", height=720, url="https://cdn.net/720.mp4")]
    )

    with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = fake_result
        await url_handler(app, mock_msg)

    assert mock_extract.call_args.args[0] == VALID_BUNNY_URL
    assert "Video Link Detected!" in mock_msg.reply.call_args.args[0]


# ==============================================================================
# 4. Invalid or Unsupported URLs (Never Fetch Untrusted Domains)
# ==============================================================================

@pytest.mark.asyncio
async def test_unsupported_domain_rejected_without_external_fetch(tmp_path):
    """Verify unsupported domains (YouTube, external sites) are rejected gracefully without fetching."""
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    await asyncio.sleep(0.01)

    url_handler = _get_handler(app, "handle_url_message")

    user_id = 991122
    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(spec=TgUser)
    mock_msg.from_user.id = user_id
    mock_msg.from_user.username = "ytuser"
    mock_msg.from_user.first_name = "YT"
    mock_msg.from_user.last_name = None
    mock_msg.text = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    mock_msg.entities = None
    mock_msg.reply = AsyncMock()

    with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract:
        await url_handler(app, mock_msg)
        # CRUCIAL: External fetch MUST NEVER be invoked on arbitrary untrusted domains
        mock_extract.assert_not_called()

    mock_msg.reply.assert_called_once()
    reply_text = mock_msg.reply.call_args.args[0]
    assert "Unsupported URL" in reply_text
    assert "iframe.mediadelivery.net" in reply_text


@pytest.mark.asyncio
async def test_malformed_url_rejected_gracefully(tmp_path):
    """Verify malformed or invalid URLs produce friendly error messages."""
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    await asyncio.sleep(0.01)

    url_handler = _get_handler(app, "handle_url_message")

    user_id = 991133
    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(spec=TgUser)
    mock_msg.from_user.id = user_id
    mock_msg.from_user.username = "badurl"
    mock_msg.from_user.first_name = "Bad"
    mock_msg.from_user.last_name = None
    mock_msg.text = "http://"
    mock_msg.entities = None
    mock_msg.reply = AsyncMock()

    with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract:
        await url_handler(app, mock_msg)
        mock_extract.assert_not_called()

    # When idle and text has no valid Bunny URL, it returns cleanly without crashing
    assert mock_extract.called is False


# ==============================================================================
# 5. Active Jobs and State Conflicts (Duplicate Job Prevention & Replace/Continue)
# ==============================================================================

@pytest.mark.asyncio
async def test_active_job_conflict_prevents_duplicate_download(tmp_path):
    """Verify a user with an active job in progress cannot start a duplicate download."""
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    await asyncio.sleep(0.01)

    url_handler = _get_handler(app, "handle_url_message")

    user_id = 887766
    # Mark user as busy in queue manager
    app.queue_manager.is_user_busy = MagicMock(return_value=True)

    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(spec=TgUser)
    mock_msg.from_user.id = user_id
    mock_msg.from_user.username = "busyuser"
    mock_msg.from_user.first_name = "Busy"
    mock_msg.from_user.last_name = None
    mock_msg.text = VALID_BUNNY_URL
    mock_msg.entities = None
    mock_msg.reply = AsyncMock()

    with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract:
        await url_handler(app, mock_msg)
        mock_extract.assert_not_called()

    mock_msg.reply.assert_called_once()
    reply_text = mock_msg.reply.call_args.args[0]
    assert "Active Download In Progress" in reply_text
    assert "/cancel" in reply_text


@pytest.mark.asyncio
async def test_unfinished_workflow_conflict_offers_continue_or_replace(tmp_path):
    """Verify sending a new link during an unfinished workflow prompts to continue or replace."""
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    await asyncio.sleep(0.01)

    url_handler = _get_handler(app, "handle_url_message")
    continue_handler = _get_handler(app, "workflow_continue")
    replace_handler = _get_handler(app, "workflow_replace")

    user_id = 554433
    extraction1 = ExtractionResult(
        success=True,
        title="First Video In Setup",
        duration=100,
        video_formats=[VideoFormat(format_id="720p", quality="720p", height=720, url="https://cdn.net/v1.mp4")],
    )
    set_pending_job(user_id, PendingJob(
        user_id=user_id,
        state=FlowState.SELECTING_QUALITY,
        extraction=extraction1,
    ))

    # User sends a second video URL while first is still waiting for quality selection
    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(spec=TgUser)
    mock_msg.from_user.id = user_id
    mock_msg.from_user.username = "twolinks"
    mock_msg.from_user.first_name = "Two"
    mock_msg.from_user.last_name = None
    mock_msg.text = SECOND_BUNNY_URL
    mock_msg.entities = None
    mock_msg.reply = AsyncMock()

    await url_handler(app, mock_msg)

    # 1. Prompted with conflict keyboard
    mock_msg.reply.assert_called_once()
    conflict_msg = mock_msg.reply.call_args.args[0]
    assert "Unfinished Download in Progress" in conflict_msg
    assert "First Video In Setup" in conflict_msg
    assert "bbbbbbbb-11" in conflict_msg

    pending = get_pending_job(user_id)
    assert pending.candidate_url == SECOND_BUNNY_URL

    # 2. Test Continue Current
    mock_cb_continue = AsyncMock(spec=CallbackQuery)
    mock_cb_continue.from_user = mock_msg.from_user
    mock_cb_continue.message = AsyncMock(spec=Message)
    mock_cb_continue.message.chat = MagicMock(spec=Chat)
    mock_cb_continue.message.chat.id = user_id
    mock_cb_continue.message.edit_text = AsyncMock()
    mock_cb_continue.answer = AsyncMock()

    await continue_handler(app, mock_cb_continue)
    assert pending.candidate_url is None
    assert "First Video In Setup" in mock_cb_continue.message.edit_text.call_args.args[0]

    # 3. Test Replace With New Link
    pending.candidate_url = SECOND_BUNNY_URL
    mock_cb_replace = AsyncMock(spec=CallbackQuery)
    mock_cb_replace.from_user = mock_msg.from_user
    mock_cb_replace.message = AsyncMock(spec=Message)
    mock_cb_replace.message.chat = MagicMock(spec=Chat)
    mock_cb_replace.message.chat.id = user_id
    mock_cb_replace.message.edit_text = AsyncMock()
    mock_cb_replace.answer = AsyncMock()

    extraction2 = ExtractionResult(
        success=True,
        title="Replaced Second Video",
        duration=250,
        video_formats=[VideoFormat(format_id="1080p", quality="1080p", height=1080, url="https://cdn.net/v2.mp4")],
    )

    with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract2:
        mock_extract2.return_value = extraction2
        await replace_handler(app, mock_cb_replace)

    # State updated to new video
    new_pending = get_pending_job(user_id)
    assert new_pending.extraction.title == "Replaced Second Video"
    assert "Replaced Second Video" in mock_cb_replace.message.edit_text.call_args.args[0]


# ==============================================================================
# 6. Multiple Users Concurrency and Isolation
# ==============================================================================

@pytest.mark.asyncio
async def test_multiple_users_isolation(tmp_path):
    """Verify workflows for multiple independent users do not interfere with each other."""
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    await asyncio.sleep(0.01)

    url_handler = _get_handler(app, "handle_url_message")

    user_a = 10001
    user_b = 10002

    # User A sends link A
    msg_a = AsyncMock(spec=Message)
    msg_a.from_user = MagicMock(id=user_a, username="usera", first_name="A", last_name=None)
    msg_a.chat = MagicMock(spec=Chat)
    msg_a.chat.id = user_a
    msg_a.text = VALID_BUNNY_URL
    msg_a.entities = None
    msg_a.reply = AsyncMock(return_value=AsyncMock())

    # User B sends link B
    msg_b = AsyncMock(spec=Message)
    msg_b.from_user = MagicMock(id=user_b, username="userb", first_name="B", last_name=None)
    msg_b.chat = MagicMock(spec=Chat)
    msg_b.chat.id = user_b
    msg_b.text = SECOND_BUNNY_URL
    msg_b.entities = None
    msg_b.reply = AsyncMock(return_value=AsyncMock())

    res_a = ExtractionResult(success=True, title="Video A", video_formats=[VideoFormat("720p", "720p", 720)])
    res_b = ExtractionResult(success=True, title="Video B", video_formats=[VideoFormat("1080p", "1080p", 1080)])

    with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.side_effect = [res_a, res_b]
        await url_handler(app, msg_a)
        await url_handler(app, msg_b)

    job_a = get_pending_job(user_a)
    job_b = get_pending_job(user_b)

    assert job_a is not None and job_b is not None
    assert job_a.extraction.title == "Video A"
    assert job_b.extraction.title == "Video B"


# ==============================================================================
# 7. Quality Selection, Caption Input, Skip Caption & Cancellation in Mode B
# ==============================================================================

@pytest.mark.asyncio
async def test_direct_link_quality_selection_and_caption_skip(tmp_path):
    """Verify Mode B workflow seamlessly proceeds to quality selection, caption prompt, and Skip Caption."""
    app = await setup_app_and_db(tmp_path)
    register_quality(app)
    await asyncio.sleep(0.01)

    quality_handler = _get_handler(app, "quality_selected")
    skip_handler = _get_handler(app, "caption_skip")

    user_id = 771122
    extraction = ExtractionResult(
        success=True,
        title="Direct Video For Quality Test",
        duration=200,
        video_formats=[
            VideoFormat(format_id="720p_fmt", quality="720p", height=720, url="https://cdn.net/720.mp4")
        ]
    )
    set_pending_job(user_id, PendingJob(
        user_id=user_id,
        state=FlowState.SELECTING_QUALITY,
        extraction=extraction,
    ))

    # User selects quality
    cb_qual = AsyncMock(spec=CallbackQuery)
    cb_qual.from_user = MagicMock(id=user_id)
    cb_qual.data = "quality:720p_fmt:720"
    cb_qual.message = AsyncMock()
    cb_qual.message.edit_text = AsyncMock()
    cb_qual.answer = AsyncMock()

    with patch("app.handlers.quality.check_disk_space", return_value=(True, "OK")):
        await quality_handler(app, cb_qual)

    # Prompted for caption, NOT immediately downloading
    pending = get_pending_job(user_id)
    assert pending.state == FlowState.WAITING_FOR_CAPTION
    assert pending.quality_label == "720p"
    assert "CUSTOM VIDEO CAPTION" in cb_qual.message.edit_text.call_args.args[0]

    # User clicks 'Skip Caption'
    cb_skip = AsyncMock(spec=CallbackQuery)
    cb_skip.from_user = MagicMock(id=user_id)
    cb_skip.data = "caption_skip"
    cb_skip.message = AsyncMock()
    cb_skip.message.edit_text = AsyncMock()
    cb_skip.answer = AsyncMock()

    await skip_handler(app, cb_skip)
    assert pending.state == FlowState.CONFIRMING_DOWNLOAD
    assert "VIDEO READY" in cb_skip.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_direct_link_cancellation_clears_state(tmp_path):
    """Verify user can cancel direct link workflow at any step."""
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    await asyncio.sleep(0.01)

    cancel_cb_handler = _get_handler(app, "cancel_download")
    cancel_cmd_handler = _get_handler(app, "cancel_command")

    user_id = 993311
    set_pending_job(user_id, PendingJob(user_id=user_id, state=FlowState.SELECTING_QUALITY))

    # Test callback cancel
    cb_cancel = AsyncMock(spec=CallbackQuery)
    cb_cancel.from_user = MagicMock(id=user_id)
    cb_cancel.message = AsyncMock()
    cb_cancel.message.edit_text = AsyncMock()
    cb_cancel.answer = AsyncMock()

    await cancel_cb_handler(app, cb_cancel)
    assert get_pending_job(user_id) is None
    assert "Download cancelled" in cb_cancel.message.edit_text.call_args.args[0]

    # Test /cancel command
    set_pending_job(user_id, PendingJob(user_id=user_id, state=FlowState.WAITING_FOR_CAPTION))
    cmd_msg = AsyncMock(spec=Message)
    cmd_msg.from_user = MagicMock(id=user_id)
    cmd_msg.reply = AsyncMock()

    await cancel_cmd_handler(app, cmd_msg)
    assert get_pending_job(user_id) is None


# ==============================================================================
# 8. Security and Authorization Checks
# ==============================================================================

@pytest.mark.asyncio
async def test_banned_user_sending_direct_link_is_blocked(tmp_path):
    """Verify banned users cannot trigger extraction via direct links."""
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    await asyncio.sleep(0.01)

    url_handler = _get_handler(app, "handle_url_message")

    user_id = 666777
    # Pre-register and ban user
    await UserRepository.get_or_create(user_id, "banneduser", "Banned", None)
    await UserRepository.set_banned(user_id, True)

    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(id=user_id, username="banneduser", first_name="Banned", last_name=None)
    mock_msg.text = VALID_BUNNY_URL
    mock_msg.entities = None
    mock_msg.reply = AsyncMock()

    with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract:
        await url_handler(app, mock_msg)
        mock_extract.assert_not_called()

    mock_msg.reply.assert_called_once()
    assert "banned" in mock_msg.reply.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_rate_limited_user_sending_direct_link_is_throttled(tmp_path):
    """Verify rate-limiting prevents spamming direct links."""
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    await asyncio.sleep(0.01)

    url_handler = _get_handler(app, "handle_url_message")

    user_id = 555111
    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(id=user_id, username="spammer", first_name="Spam", last_name=None)
    mock_msg.text = VALID_BUNNY_URL
    mock_msg.entities = None
    mock_msg.reply = AsyncMock()

    with patch("app.handlers.download.rate_limiter.is_allowed", return_value=False):
        with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract:
            await url_handler(app, mock_msg)
            mock_extract.assert_not_called()

    mock_msg.reply.assert_called_once()
    assert "Too Many Requests" in mock_msg.reply.call_args.args[0]


# ==============================================================================
# 9. End-to-End Pipeline Integration from Direct Link
# ==============================================================================

@pytest.mark.asyncio
async def test_direct_link_end_to_end_pipeline_integration(tmp_path):
    """Verify full pipeline: Direct Link -> Quality Selection -> Caption -> Confirm -> Download/Upload."""
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    register_quality(app)
    await asyncio.sleep(0.01)

    url_handler = _get_handler(app, "handle_url_message")
    qual_handler = _get_handler(app, "quality_selected")
    skip_handler = _get_handler(app, "caption_skip")
    confirm_handler = _get_handler(app, "start_download_confirmed")

    user_id = 999888
    # Step 1: User sends direct link
    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(id=user_id, username="e2euser", first_name="E2E", last_name=None)
    mock_msg.chat = MagicMock(spec=Chat)
    mock_msg.chat.id = user_id
    mock_msg.text = VALID_BUNNY_URL
    mock_msg.entities = None

    status_msg = AsyncMock(spec=Message)
    status_msg.edit_text = AsyncMock()
    mock_msg.reply = AsyncMock(return_value=status_msg)

    fake_result = ExtractionResult(
        success=True,
        title="End To End Pipeline Video",
        duration=150,
        video_formats=[
            VideoFormat(format_id="1080p", quality="1080p", height=1080, url="https://cdn.net/1080.mp4")
        ]
    )

    with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = fake_result
        await url_handler(app, mock_msg)

    pending = get_pending_job(user_id)
    assert pending.state == FlowState.SELECTING_QUALITY

    # Step 2: Quality Selected
    cb_qual = AsyncMock(spec=CallbackQuery)
    cb_qual.from_user = mock_msg.from_user
    cb_qual.data = "quality:1080p:1080"
    cb_qual.message = AsyncMock(spec=Message)
    cb_qual.message.chat = MagicMock(spec=Chat)
    cb_qual.message.chat.id = user_id
    cb_qual.message.edit_text = AsyncMock()
    cb_qual.answer = AsyncMock()

    with patch("app.handlers.quality.check_disk_space", return_value=(True, "OK")):
        await qual_handler(app, cb_qual)

    assert pending.state == FlowState.WAITING_FOR_CAPTION

    # Step 3: Skip Caption
    cb_skip = AsyncMock(spec=CallbackQuery)
    cb_skip.from_user = mock_msg.from_user
    cb_skip.data = "caption_skip"
    cb_skip.message = AsyncMock(spec=Message)
    cb_skip.message.chat = MagicMock(spec=Chat)
    cb_skip.message.chat.id = user_id
    cb_skip.message.edit_text = AsyncMock()
    cb_skip.answer = AsyncMock()

    await skip_handler(app, cb_skip)
    assert pending.state == FlowState.CONFIRMING_DOWNLOAD

    # Step 4: Confirm Download -> Starts processing
    cb_confirm = AsyncMock(spec=CallbackQuery)
    cb_confirm.from_user = mock_msg.from_user
    cb_confirm.data = "start_download_confirmed"
    cb_confirm.message = AsyncMock(spec=Message)
    cb_confirm.message.chat = MagicMock(spec=Chat)
    cb_confirm.message.chat.id = user_id
    cb_confirm.message.edit_text = AsyncMock()
    cb_confirm.answer = AsyncMock()

    with patch("app.handlers.quality.check_disk_space", return_value=(True, "OK")):
        await confirm_handler(app, cb_confirm)

    # Job is submitted to queue manager, pending flow cleared, and user informed
    assert get_pending_job(user_id) is None
    assert app.queue_manager.queued_count == 1
    cb_confirm.message.edit_text.assert_called_once()


@pytest.mark.asyncio
async def test_direct_link_custom_caption_workflow(tmp_path):
    """Verify custom caption input with direct link flow attaches caption and formatting."""
    app = await setup_app_and_db(tmp_path)
    register_download(app)
    register_quality(app)
    await asyncio.sleep(0.01)

    url_handler = _get_handler(app, "handle_url_message")
    qual_handler = _get_handler(app, "quality_selected")

    user_id = 444333
    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(id=user_id, username="capuser", first_name="Cap", last_name=None)
    mock_msg.chat = MagicMock(spec=Chat)
    mock_msg.chat.id = user_id
    mock_msg.text = VALID_BUNNY_URL
    mock_msg.entities = None

    status_msg = AsyncMock(spec=Message)
    status_msg.edit_text = AsyncMock()
    mock_msg.reply = AsyncMock(return_value=status_msg)

    fake_result = ExtractionResult(
        success=True,
        title="Custom Caption Direct Video",
        duration=60,
        video_formats=[VideoFormat(format_id="720p", quality="720p", height=720, url="https://cdn.net/720.mp4")]
    )

    with patch("app.handlers.download.BunnyExtractor.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = fake_result
        await url_handler(app, mock_msg)

    cb_qual = AsyncMock(spec=CallbackQuery)
    cb_qual.from_user = mock_msg.from_user
    cb_qual.data = "quality:720p:720"
    cb_qual.message = AsyncMock(spec=Message)
    cb_qual.message.chat = MagicMock(spec=Chat)
    cb_qual.message.chat.id = user_id
    cb_qual.message.edit_text = AsyncMock()
    cb_qual.answer = AsyncMock()

    with patch("app.handlers.quality.check_disk_space", return_value=(True, "OK")):
        await qual_handler(app, cb_qual)

    # User sends custom caption
    cap_msg = AsyncMock(spec=Message)
    cap_msg.from_user = mock_msg.from_user
    cap_msg.chat = mock_msg.chat
    cap_msg.text = "🔥 Exclusive Masterclass Part 1 🚀\nDetailed explanation inside."
    cap_msg.entities = None
    cap_msg.reply = AsyncMock()

    await url_handler(app, cap_msg)

    pending = get_pending_job(user_id)
    assert pending.state == FlowState.CONFIRMING_DOWNLOAD
    assert "Exclusive Masterclass Part 1" in pending.caption
    assert "VIDEO READY" in cap_msg.reply.call_args.args[0]

