import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pyrogram import Client
from pyrogram.enums import ParseMode, MessageEntityType
from pyrogram.types import (
    Message, CallbackQuery, User as TgUser, Chat, MessageEntity, InlineKeyboardMarkup
)

from app.keyboards.caption import caption_input_keyboard, caption_confirm_keyboard
from app.handlers.state import (
    FlowState, PendingJob, get_pending_job, set_pending_job, clear_pending_job, _pending_jobs
)
from app.handlers.download import register as register_download, _pending_extractions, _waiting_for_url
from app.handlers.quality import register as register_quality, _process_download
from app.services.bunny_extractor import ExtractionResult, VideoFormat
from app.services.upload_service import UploadService
from app.services.ffmpeg_service import FFmpegService


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure clean state before and after each test."""
    _pending_jobs.clear()
    _pending_extractions.clear()
    _waiting_for_url.clear()
    yield
    _pending_jobs.clear()
    _pending_extractions.clear()
    _waiting_for_url.clear()


def test_caption_keyboards_structure():
    """Verify caption input and confirmation keyboards contain required buttons and callbacks."""
    input_kb = caption_input_keyboard()
    assert isinstance(input_kb, InlineKeyboardMarkup)
    callbacks_input = [btn.callback_data for row in input_kb.inline_keyboard for btn in row]
    assert "caption_skip" in callbacks_input
    assert "caption_back_quality" in callbacks_input
    assert "cancel_download" in callbacks_input

    confirm_kb = caption_confirm_keyboard()
    assert isinstance(confirm_kb, InlineKeyboardMarkup)
    callbacks_confirm = [btn.callback_data for row in confirm_kb.inline_keyboard for btn in row]
    assert "start_download_confirmed" in callbacks_confirm
    assert "caption_edit" in callbacks_confirm
    assert "caption_back_quality" in callbacks_confirm
    assert "cancel_download" in callbacks_confirm


@pytest.mark.asyncio
async def test_quality_selection_prompts_caption_without_starting_download():
    """Verify selecting a quality transitions to WAITING_FOR_CAPTION and does NOT start download."""
    app = Client("test_session_quality", api_id=123, api_hash="hash", bot_token="token")
    app.config = MagicMock()
    app.config.TEMP_DIRECTORY = "temp"
    app.queue_manager = MagicMock()
    app.queue_manager.is_user_busy.return_value = False

    register_quality(app)
    await asyncio.sleep(0.01)

    quality_cb_handler = None
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback") and handler.callback.__name__ == "quality_selected":
            quality_cb_handler = handler.callback
            break
    assert quality_cb_handler is not None

    user_id = 111222
    extraction = ExtractionResult(
        success=True,
        title="Sample Bunny Stream Video",
        video_formats=[
            VideoFormat(format_id="720p_fmt", quality="720p", height=720, url="https://cdn.net/720.mp4")
        ]
    )
    _pending_extractions[user_id] = extraction
    set_pending_job(user_id, PendingJob(
        user_id=user_id,
        state=FlowState.SELECTING_QUALITY,
        extraction=extraction,
    ))

    mock_callback = AsyncMock(spec=CallbackQuery)
    mock_callback.from_user = MagicMock(spec=TgUser)
    mock_callback.from_user.id = user_id
    mock_callback.data = "quality:720p_fmt:720"
    mock_callback.message = AsyncMock(spec=Message)
    mock_callback.message.edit_text = AsyncMock()
    mock_callback.answer = AsyncMock()

    with patch("app.handlers.quality.check_disk_space", return_value=(True, "OK")):
        await quality_cb_handler(app, mock_callback)

    # 1. Download MUST NOT be added to queue manager yet
    app.queue_manager.add_job.assert_not_called()

    # 2. State must transition to WAITING_FOR_CAPTION
    pending = get_pending_job(user_id)
    assert pending is not None
    assert pending.state == FlowState.WAITING_FOR_CAPTION
    assert pending.quality_label == "720p"

    # 3. Caption prompt UI must be shown
    mock_callback.message.edit_text.assert_called_once()
    msg_text = mock_callback.message.edit_text.call_args.args[0]
    assert "CUSTOM VIDEO CAPTION" in msg_text
    assert "Sample Bunny Stream Video" in msg_text
    assert "720p" in msg_text
    assert "Type your caption below" in msg_text


@pytest.mark.asyncio
async def test_custom_caption_text_message_submission_and_unicode_sinhala():
    """Verify custom caption text message supports Unicode, Sinhala, emojis, and multiline."""
    app = Client("test_session_text", api_id=123, api_hash="hash", bot_token="token")
    register_download(app)
    await asyncio.sleep(0.01)

    text_handler = None
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback") and handler.callback.__name__ == "handle_url_message":
            text_handler = handler.callback
            break
    assert text_handler is not None

    user_id = 333444
    extraction = ExtractionResult(
        success=True,
        title="Physics Lesson 01",
        video_formats=[
            VideoFormat(format_id="1080p_fmt", quality="1080p", height=1080, url="https://cdn.net/1080.mp4")
        ]
    )
    set_pending_job(user_id, PendingJob(
        user_id=user_id,
        state=FlowState.WAITING_FOR_CAPTION,
        extraction=extraction,
        selected_format=extraction.video_formats[0],
        quality_label="1080p",
    ))

    # Caption containing Sinhala, emojis, and multiline text
    sinhala_caption = "🎬 භෞතික විද්‍යාව පාඩම 01 🔥\n✨ සියලුම විවරණ සහිතයි\n📥 බාගත කරගන්න."
    mock_entity = MessageEntity(type=MessageEntityType.BOLD, offset=0, length=2)

    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(spec=TgUser)
    mock_msg.from_user.id = user_id
    mock_msg.text = sinhala_caption
    mock_msg.entities = [mock_entity]
    mock_msg.reply = AsyncMock()

    await text_handler(app, mock_msg)

    # State must transition to CONFIRMING_DOWNLOAD
    pending = get_pending_job(user_id)
    assert pending is not None
    assert pending.state == FlowState.CONFIRMING_DOWNLOAD
    assert pending.caption == sinhala_caption
    assert pending.caption_entities == [mock_entity]

    # Confirmation screen must be displayed with confirmation keyboard
    mock_msg.reply.assert_called_once()
    confirm_text = mock_msg.reply.call_args.args[0]
    assert "VIDEO READY" in confirm_text
    assert "Physics Lesson 01" in confirm_text
    assert "1080p" in confirm_text
    assert "භෞතික විද්‍යාව" in confirm_text

    call_kwargs = mock_msg.reply.call_args.kwargs
    assert call_kwargs.get("parse_mode") == ParseMode.HTML
    assert isinstance(call_kwargs.get("reply_markup"), InlineKeyboardMarkup)


@pytest.mark.asyncio
async def test_caption_length_exceeding_1024_characters_is_rejected():
    """Verify captions exceeding Telegram's 1024-character limit show warning and remain in WAITING_FOR_CAPTION."""
    app = Client("test_session_overflow", api_id=123, api_hash="hash", bot_token="token")
    register_download(app)
    await asyncio.sleep(0.01)

    text_handler = None
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback") and handler.callback.__name__ == "handle_url_message":
            text_handler = handler.callback
            break
    assert text_handler is not None

    user_id = 555666
    extraction = ExtractionResult(success=True, title="Test Video", video_formats=[])
    set_pending_job(user_id, PendingJob(
        user_id=user_id,
        state=FlowState.WAITING_FOR_CAPTION,
        extraction=extraction,
        quality_label="720p",
    ))

    # 1050 character caption (exceeds 1024)
    long_caption = "A" * 1050
    mock_msg = AsyncMock(spec=Message)
    mock_msg.from_user = MagicMock(spec=TgUser)
    mock_msg.from_user.id = user_id
    mock_msg.text = long_caption
    mock_msg.entities = None
    mock_msg.reply = AsyncMock()

    await text_handler(app, mock_msg)

    # Verify warning sent and user stays in WAITING_FOR_CAPTION
    pending = get_pending_job(user_id)
    assert pending.state == FlowState.WAITING_FOR_CAPTION
    assert pending.caption is None

    mock_msg.reply.assert_called_once()
    reply_text = mock_msg.reply.call_args.args[0]
    assert "Caption Too Long" in reply_text
    assert "1050" in reply_text
    assert "1024" in reply_text


@pytest.mark.asyncio
async def test_caption_skip_callback():
    """Verify Skip Caption sets default title caption and transitions to confirmation."""
    app = Client("test_session_skip", api_id=123, api_hash="hash", bot_token="token")
    register_quality(app)
    await asyncio.sleep(0.01)

    skip_handler = None
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback") and handler.callback.__name__ == "caption_skip":
            skip_handler = handler.callback
            break
    assert skip_handler is not None

    user_id = 777888
    extraction = ExtractionResult(
        success=True,
        title="Intro to Antigravity",
        video_formats=[
            VideoFormat(format_id="480p", quality="480p", height=480, url="https://cdn.net/480.mp4")
        ]
    )
    set_pending_job(user_id, PendingJob(
        user_id=user_id,
        state=FlowState.WAITING_FOR_CAPTION,
        extraction=extraction,
        selected_format=extraction.video_formats[0],
        quality_label="480p",
    ))

    mock_callback = AsyncMock(spec=CallbackQuery)
    mock_callback.from_user = MagicMock(spec=TgUser)
    mock_callback.from_user.id = user_id
    mock_callback.message = AsyncMock(spec=Message)
    mock_callback.message.edit_text = AsyncMock()
    mock_callback.answer = AsyncMock()

    await skip_handler(app, mock_callback)

    pending = get_pending_job(user_id)
    assert pending.state == FlowState.CONFIRMING_DOWNLOAD
    assert "Intro to Antigravity" in pending.caption

    mock_callback.message.edit_text.assert_called_once()
    msg_text = mock_callback.message.edit_text.call_args.args[0]
    assert "VIDEO READY" in msg_text
    assert "Intro to Antigravity" in msg_text
    assert "(Default)" in msg_text


@pytest.mark.asyncio
async def test_caption_back_and_edit_navigation():
    """Verify navigating back to quality selection and editing caption preserves state."""
    app = Client("test_session_nav", api_id=123, api_hash="hash", bot_token="token")
    register_quality(app)
    await asyncio.sleep(0.01)

    back_handler = None
    edit_handler = None
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback"):
            if handler.callback.__name__ == "caption_back_quality":
                back_handler = handler.callback
            elif handler.callback.__name__ == "caption_edit":
                edit_handler = handler.callback

    assert back_handler is not None
    assert edit_handler is not None

    user_id = 999000
    extraction = ExtractionResult(
        success=True,
        title="Navigation Video",
        video_formats=[
            VideoFormat(format_id="720p", quality="720p", height=720, url="https://cdn.net/720.mp4")
        ]
    )
    job = PendingJob(
        user_id=user_id,
        state=FlowState.CONFIRMING_DOWNLOAD,
        extraction=extraction,
        selected_format=extraction.video_formats[0],
        quality_label="720p",
        caption="Draft Caption",
    )
    set_pending_job(user_id, job)

    # 1. Test caption_edit: returns to WAITING_FOR_CAPTION
    mock_callback = AsyncMock(spec=CallbackQuery)
    mock_callback.from_user = MagicMock(spec=TgUser)
    mock_callback.from_user.id = user_id
    mock_callback.message = AsyncMock(spec=Message)
    mock_callback.message.edit_text = AsyncMock()
    mock_callback.answer = AsyncMock()

    await edit_handler(app, mock_callback)
    assert get_pending_job(user_id).state == FlowState.WAITING_FOR_CAPTION
    edit_text = mock_callback.message.edit_text.call_args.args[0]
    assert "CUSTOM VIDEO CAPTION" in edit_text

    # 2. Test caption_back_quality: returns to SELECTING_QUALITY
    mock_callback.message.edit_text.reset_mock()
    await back_handler(app, mock_callback)
    assert get_pending_job(user_id).state == FlowState.SELECTING_QUALITY
    back_text = mock_callback.message.edit_text.call_args.args[0]
    assert "VIDEO DETECTED" in back_text
    assert "Available qualities" in back_text


@pytest.mark.asyncio
async def test_start_download_confirmed_enqueues_with_custom_caption():
    """Verify clicking Start Download enqueues job and passes custom caption to pipeline worker."""
    app = Client("test_session_start", api_id=123, api_hash="hash", bot_token="token")
    app.config = MagicMock()
    app.config.TEMP_DIRECTORY = "temp"
    app.queue_manager = MagicMock()
    app.queue_manager.is_user_busy.return_value = False
    app.queue_manager.add_job = AsyncMock(return_value="job-uuid-1234")
    app.queue_manager.get_queue_position.return_value = 1
    app.queue_manager.active_count = 0
    app.queue_manager.max_concurrent = 2

    register_quality(app)
    await asyncio.sleep(0.01)

    start_handler = None
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback") and handler.callback.__name__ == "start_download_confirmed":
            start_handler = handler.callback
            break
    assert start_handler is not None

    user_id = 888777
    extraction = ExtractionResult(
        success=True,
        title="Confirmed Video",
        video_formats=[
            VideoFormat(format_id="720p", quality="720p", height=720, url="https://cdn.net/720.mp4")
        ]
    )
    custom_caption = "Confirmed custom caption with emojis 🎬🚀"
    set_pending_job(user_id, PendingJob(
        user_id=user_id,
        state=FlowState.CONFIRMING_DOWNLOAD,
        extraction=extraction,
        selected_format=extraction.video_formats[0],
        quality_label="720p",
        caption=custom_caption,
        caption_entities=None,
    ))

    mock_callback = AsyncMock(spec=CallbackQuery)
    mock_callback.from_user = MagicMock(spec=TgUser)
    mock_callback.from_user.id = user_id
    mock_callback.message = AsyncMock(spec=Message)
    mock_callback.message.edit_text = AsyncMock()
    mock_callback.answer = AsyncMock()

    with patch("app.handlers.quality.check_disk_space", return_value=(True, "OK")):
        await start_handler(app, mock_callback)

    # Job is now enqueued in queue manager
    app.queue_manager.add_job.assert_called_once()
    call_kwargs = app.queue_manager.add_job.call_args.kwargs
    assert call_kwargs["telegram_id"] == user_id
    assert call_kwargs["quality"] == "720p"
    assert call_kwargs["video_title"] == "Confirmed Video"
    assert callable(call_kwargs["callback"])

    # Status message updated to STARTING DOWNLOAD
    mock_callback.message.edit_text.assert_called_once()
    assert "STARTING DOWNLOAD" in mock_callback.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_upload_service_delivers_custom_caption_and_entities(tmp_path):
    """Verify UploadService passes user's custom caption and entities to send_video and send_document."""
    test_video = tmp_path / "caption_test.mp4"
    test_video.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00" + b"\x00" * 50)

    upload_service = UploadService(max_file_size_bytes=50 * 1024 * 1024)
    mock_client = AsyncMock()
    mock_sent_msg = MagicMock(spec=Message)
    mock_client.send_video.return_value = mock_sent_msg

    custom_caption = "✨ Exclusive Video Caption 🚀"
    mock_entity = MessageEntity(type=MessageEntityType.BOLD, offset=0, length=2)

    with patch.object(FFmpegService, "get_duration", return_value=30):
        with patch.object(FFmpegService, "generate_thumbnail", return_value=False):
            # 1. Test video upload with custom caption and entities
            res = await upload_service.upload_video(
                client=mock_client,
                chat_id=123456,
                file_path=str(test_video),
                title="Default Title",
                caption=custom_caption,
                caption_entities=[mock_entity],
            )
            assert res == mock_sent_msg
            mock_client.send_video.assert_called_once()
            sv_kwargs = mock_client.send_video.call_args.kwargs
            assert sv_kwargs["caption"] == custom_caption
            assert sv_kwargs["caption_entities"] == [mock_entity]

            # 2. Test document fallback preserves custom caption and entities
            mock_client.send_video.side_effect = Exception("VIDEO_ENCODING_ERROR")
            mock_client.send_document.return_value = mock_sent_msg

            res2 = await upload_service.upload_video(
                client=mock_client,
                chat_id=123456,
                file_path=str(test_video),
                title="Default Title",
                caption=custom_caption,
                caption_entities=[mock_entity],
            )
            assert res2 == mock_sent_msg
            mock_client.send_document.assert_called_once()
            sd_kwargs = mock_client.send_document.call_args.kwargs
            assert sd_kwargs["caption"] == custom_caption
            assert sd_kwargs["caption_entities"] == [mock_entity]
