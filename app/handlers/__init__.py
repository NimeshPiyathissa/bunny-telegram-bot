from pyrogram import Client


def register_handlers(app: Client):
    """Register all bot handlers."""
    from app.handlers.start import register as reg_start
    from app.handlers.download import register as reg_download
    from app.handlers.quality import register as reg_quality
    from app.handlers.progress import register as reg_progress
    from app.handlers.settings import register as reg_settings
    from app.handlers.admin import register as reg_admin

    reg_start(app)
    reg_download(app)
    reg_quality(app)
    reg_progress(app)
    reg_settings(app)
    reg_admin(app)
