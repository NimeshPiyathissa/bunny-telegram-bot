import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.parser import Parser
from pyrogram.types import Message, CallbackQuery, User as TgUser

from app.handlers.start import WELCOME_TEXT, HELP_TEXT, register as register_start
from app.handlers.settings import register as register_settings
from app.handlers.progress import register as register_progress
from app.handlers.admin import register as register_admin
from app.database.models import User, DownloadJob


def test_welcome_text_html_validity():
    """Verify that WELCOME_TEXT can be parsed by Pyrogram's Parser with ParseMode.HTML."""
    parser = Parser(None)
    import asyncio
    res = asyncio.run(parser.parse(WELCOME_TEXT, ParseMode.HTML))
    assert res is not None
    assert "message" in res
    assert "entities" in res


def test_help_text_html_validity():
    """Verify that HELP_TEXT can be parsed by Pyrogram's Parser with ParseMode.HTML."""
    parser = Parser(None)
    import asyncio
    res = asyncio.run(parser.parse(HELP_TEXT, ParseMode.HTML))
    assert res is not None
    assert "message" in res
    assert "entities" in res


@pytest.mark.asyncio
async def test_start_command_handler_success():
    """Verify /start command replies with valid ParseMode.HTML and registers user."""
    import asyncio
    app = Client("test_session", api_id=123, api_hash="hash", bot_token="token")
    register_start(app)
    await asyncio.sleep(0.01)

    start_handler = None
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback") and handler.callback.__name__ == "start_command":
            start_handler = handler.callback
            break

    assert start_handler is not None

    with patch("app.database.repository.UserRepository.get_or_create", new_callable=AsyncMock):
        with patch("app.database.repository.UserRepository.is_banned", new_callable=AsyncMock) as mock_banned:
            mock_banned.return_value = False
            mock_message = AsyncMock(spec=Message)
            mock_tg_user = MagicMock(spec=TgUser)
            mock_tg_user.id = 12345
            mock_tg_user.username = "testuser"
            mock_tg_user.first_name = "Test"
            mock_tg_user.last_name = None
            mock_message.from_user = mock_tg_user
            mock_message.reply = AsyncMock()

            await start_handler(app, mock_message)

            mock_message.reply.assert_called_once()
            call_kwargs = mock_message.reply.call_args.kwargs
            assert call_kwargs.get("parse_mode") == ParseMode.HTML


@pytest.mark.asyncio
async def test_start_command_handler_banned_user():
    """Verify banned users receive the banned notice without crash."""
    import asyncio
    app = Client("test_session", api_id=123, api_hash="hash", bot_token="token")
    register_start(app)
    await asyncio.sleep(0.01)

    start_handler = None
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback") and handler.callback.__name__ == "start_command":
            start_handler = handler.callback
            break

    assert start_handler is not None

    with patch("app.database.repository.UserRepository.get_or_create", new_callable=AsyncMock):
        with patch("app.database.repository.UserRepository.is_banned", new_callable=AsyncMock) as mock_banned:
            mock_banned.return_value = True
            mock_message = AsyncMock(spec=Message)
            mock_tg_user = MagicMock(spec=TgUser)
            mock_tg_user.id = 666
            mock_tg_user.username = "banneduser"
            mock_tg_user.first_name = "Banned"
            mock_tg_user.last_name = None
            mock_message.from_user = mock_tg_user
            mock_message.reply = AsyncMock()

            await start_handler(app, mock_message)

            mock_message.reply.assert_called_once()
            assert "banned" in mock_message.reply.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_help_command_handler_parse_mode():
    """Verify /help command uses ParseMode.HTML."""
    import asyncio
    app = Client("test_session", api_id=123, api_hash="hash", bot_token="token")
    register_start(app)
    await asyncio.sleep(0.01)

    help_handler = None
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback") and handler.callback.__name__ == "help_command":
            help_handler = handler.callback
            break

    assert help_handler is not None
    mock_message = AsyncMock(spec=Message)
    mock_message.reply = AsyncMock()

    await help_handler(app, mock_message)
    mock_message.reply.assert_called_once()
    call_kwargs = mock_message.reply.call_args.kwargs
    assert call_kwargs.get("parse_mode") == ParseMode.HTML


@pytest.mark.asyncio
async def test_settings_callback_parse_mode():
    """Verify settings callback uses ParseMode.HTML."""
    import asyncio
    app = Client("test_session", api_id=123, api_hash="hash", bot_token="token")
    app.config = MagicMock()
    app.config.MAX_CONCURRENT_DOWNLOADS = 2
    app.config.MAX_DOWNLOADS_PER_USER = 1
    app.config.MAX_FILE_SIZE_MB = 1900
    app.config.DOWNLOAD_TIMEOUT_SECONDS = 3600
    app.config.AUTO_CLEANUP = True

    register_settings(app)
    await asyncio.sleep(0.01)

    settings_handler = None
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback") and handler.callback.__name__ == "settings_callback":
            settings_handler = handler.callback
            break

    assert settings_handler is not None
    mock_callback = AsyncMock(spec=CallbackQuery)
    mock_callback.message = AsyncMock(spec=Message)
    mock_callback.message.edit_text = AsyncMock()
    mock_callback.answer = AsyncMock()

    await settings_handler(app, mock_callback)
    mock_callback.message.edit_text.assert_called_once()
    call_kwargs = mock_callback.message.edit_text.call_args.kwargs
    assert call_kwargs.get("parse_mode") == ParseMode.HTML


@pytest.mark.asyncio
async def test_admin_jobs_command_displays_active_and_failed():
    """Verify /jobs admin command lists active, queued, and failed jobs."""
    import asyncio
    app = Client("test_session_admin", api_id=123, api_hash="hash", bot_token="token")
    app.config = MagicMock()
    app.config.is_admin.return_value = True

    mock_qm = MagicMock()
    mock_qm.active_count = 1
    mock_qm.queued_count = 0
    app.queue_manager = mock_qm

    register_admin(app)
    await asyncio.sleep(0.01)

    jobs_handler = None
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback") and handler.callback.__name__ == "jobs_command":
            jobs_handler = handler.callback
            break

    assert jobs_handler is not None

    mock_active = DownloadJob(
        job_id="active12345", telegram_id=111, status="DOWNLOADING", selected_quality="720p"
    )
    mock_failed = DownloadJob(
        job_id="failed12345", telegram_id=222, status="FAILED",
        error_message="Access denied (403 Forbidden)?token=secret123"
    )

    with patch("app.database.repository.JobRepository.get_all_active_jobs", new_callable=AsyncMock) as m_act:
        m_act.return_value = [mock_active]
        with patch("app.database.repository.JobRepository.get_failed_jobs", new_callable=AsyncMock) as m_fail:
            m_fail.return_value = [mock_failed]
            mock_message = AsyncMock(spec=Message)
            mock_message.from_user = MagicMock()
            mock_message.from_user.id = 999
            mock_message.reply = AsyncMock()

            await jobs_handler(app, mock_message)
            mock_message.reply.assert_called_once()
            reply_text = mock_message.reply.call_args.args[0]
            assert "active12" in reply_text
            assert "failed12" in reply_text
            # Verify secrets are redacted from error output
            assert "secret123" not in reply_text
            assert "[redacted]" in reply_text


@pytest.mark.asyncio
async def test_admin_recover_jobs_command():
    """Verify /recover_jobs admin command triggers reconciliation safely."""
    import asyncio
    app = Client("test_session_recover", api_id=123, api_hash="hash", bot_token="token")
    app.config = MagicMock()
    app.config.is_admin.return_value = True

    mock_qm = AsyncMock()
    mock_qm.reconcile_stale_jobs.return_value = 2
    mock_qm.active_count = 0
    mock_qm.queued_count = 0
    app.queue_manager = mock_qm

    register_admin(app)
    await asyncio.sleep(0.01)

    recover_handler = None
    for handler in app.dispatcher.groups.get(0, []):
        if hasattr(handler, "callback") and handler.callback.__name__ == "recover_jobs_command":
            recover_handler = handler.callback
            break

    assert recover_handler is not None

    mock_message = AsyncMock(spec=Message)
    mock_message.from_user = MagicMock()
    mock_message.from_user.id = 999
    mock_message.reply = AsyncMock()

    await recover_handler(app, mock_message)
    mock_qm.reconcile_stale_jobs.assert_called_once()
    mock_message.reply.assert_called_once()
    reply_text = mock_message.reply.call_args.args[0]
    assert "JOB RECOVERY COMPLETED" in reply_text
    assert "2" in reply_text
