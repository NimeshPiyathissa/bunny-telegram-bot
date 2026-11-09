"""Bunny Stream Telegram Downloader package."""

from app.utils.crypto_patch import setup_crypto_patch

# Ensure MTProto encryption acceleration is loaded
setup_crypto_patch()

