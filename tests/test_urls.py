import pytest
from app.utils.security import (
    validate_url,
    is_bunny_url,
    parse_bunny_embed_url,
    normalize_url,
    extract_urls,
    sanitize_url_for_logging,
    sanitize_url_for_display,
)

PRIMARY_TEST_URL = (
    "https://iframe.mediadelivery.net/embed/346396/af5aa834-acd6-47bc-a800-32ad1a8f7d92"
    "?autoplay=false&loop=false&muted=false&preload=true&responsive=true"
)


def test_primary_bunny_url_parsing():
    """Verify primary Bunny Stream embed URL parsing."""
    result = parse_bunny_embed_url(PRIMARY_TEST_URL)
    assert result is not None
    assert result["library_id"] == "346396"
    assert result["video_guid"] == "af5aa834-acd6-47bc-a800-32ad1a8f7d92"
    assert result["params"]["autoplay"] == "false"
    assert result["params"]["loop"] == "false"
    assert result["params"]["muted"] == "false"
    assert result["params"]["preload"] == "true"
    assert result["params"]["responsive"] == "true"


def test_bunny_domain_recognition():
    """Verify supported and unsupported domain recognition."""
    assert is_bunny_url(PRIMARY_TEST_URL) is True
    assert is_bunny_url("https://video.bunnycdn.com/play/123/abc") is True
    assert is_bunny_url("https://pullzone.vz-cdn.net/video.mp4") is True
    assert is_bunny_url("https://youtube.com/watch?v=123") is False
    assert is_bunny_url("https://evil-mediadelivery.net/embed/1/2") is False
    assert is_bunny_url("not a url") is False


def test_invalid_bunny_embed_urls():
    """Verify malformed or invalid Bunny URLs return None."""
    # Non-digit library ID
    assert parse_bunny_embed_url(
        "https://iframe.mediadelivery.net/embed/abc/af5aa834-acd6-47bc-a800-32ad1a8f7d92"
    ) is None

    # Invalid UUID GUID
    assert parse_bunny_embed_url(
        "https://iframe.mediadelivery.net/embed/12345/not-a-valid-guid"
    ) is None

    # Missing path parts
    assert parse_bunny_embed_url(
        "https://iframe.mediadelivery.net/embed/12345"
    ) is None

    # Wrong host
    assert parse_bunny_embed_url(
        "https://example.com/embed/12345/af5aa834-acd6-47bc-a800-32ad1a8f7d92"
    ) is None

    # Empty string
    assert parse_bunny_embed_url("") is None


def test_url_validation_schemes():
    """Ensure only http and https schemes are permitted."""
    valid, _ = validate_url("https://iframe.mediadelivery.net/embed/123/uuid")
    assert valid is True

    valid, _ = validate_url("http://iframe.mediadelivery.net/embed/123/uuid")
    assert valid is True

    valid, err = validate_url("ftp://iframe.mediadelivery.net/embed/123/uuid")
    assert valid is False
    assert "not supported" in err.lower()

    valid, err = validate_url("file:///etc/passwd")
    assert valid is False

    valid, err = validate_url("javascript:alert(1)")
    assert valid is False


def test_url_length_limit():
    """Verify overly long URLs are rejected."""
    long_url = "https://iframe.mediadelivery.net/" + ("a" * 2500)
    valid, err = validate_url(long_url)
    assert valid is False
    assert "too long" in err.lower()


def test_normalize_url_variations():
    """Verify URL normalization removes surrounding quotes, angle brackets, and trailing sentence punctuation."""
    target = "https://iframe.mediadelivery.net/embed/346396/af5aa834-acd6-47bc-a800-32ad1a8f7d92?token=abc-123_456"

    # Trailing periods, commas, colons, exclamation marks
    assert normalize_url(target + ".") == target
    assert normalize_url(target + ",") == target
    assert normalize_url(target + "!") == target
    assert normalize_url(target + ";") == target

    # Unmatched parentheses and brackets
    assert normalize_url(f"({target})") == target
    assert normalize_url(f"[{target}]") == target
    assert normalize_url(f"<{target}>") == target
    assert normalize_url(f'"{target}"') == target
    assert normalize_url(f"'{target}'") == target

    # Preserves internal query params, equals, ampersands, dashes, and underscores
    assert normalize_url(target) == target

    # Empty or non-string input
    assert normalize_url("") == ""
    assert normalize_url(None) == ""  # type: ignore


def test_extract_urls_from_plain_text_and_surrounding_text():
    """Verify extraction of Bunny URLs with arbitrary surrounding text."""
    text = (
        "Hello bot! Please download this lecture for me:\n"
        "https://iframe.mediadelivery.net/embed/123/af5aa834-acd6-47bc-a800-32ad1a8f7d92?autoplay=false.\n"
        "Thank you so much!"
    )
    urls = extract_urls(text)
    assert len(urls) == 1
    assert urls[0] == "https://iframe.mediadelivery.net/embed/123/af5aa834-acd6-47bc-a800-32ad1a8f7d92?autoplay=false"


def test_extract_urls_from_telegram_entities():
    """Verify extraction from both URL and TEXT_LINK Telegram message entities."""
    from pyrogram.enums import MessageEntityType
    from pyrogram.types import MessageEntity

    text = "Check this direct link and also this custom hyperlinked video."
    ent_url = MessageEntity(
        type=MessageEntityType.URL,
        offset=11,
        length=11,  # "direct link"
    )
    ent_text_link = MessageEntity(
        type=MessageEntityType.TEXT_LINK,
        offset=37,
        length=25,
        url="https://iframe.mediadelivery.net/embed/555/af5aa834-acd6-47bc-a800-32ad1a8f7d92?token=secret999",
    )

    # When entity points to URL text
    full_text = "Here is https://iframe.mediadelivery.net/embed/111/af5aa834-acd6-47bc-a800-32ad1a8f7d92 and extra"
    ent_url_real = MessageEntity(
        type=MessageEntityType.URL,
        offset=8,
        length=80,
    )

    extracted = extract_urls(full_text, [ent_url_real, ent_text_link])
    assert len(extracted) == 2
    assert "111" in extracted[0]
    assert "555" in extracted[1]
    assert "token=secret999" in extracted[1]


def test_sanitize_url_for_logging():
    """Verify sensitive tokens and keys in query string are redacted in logs."""
    raw = "https://iframe.mediadelivery.net/embed/123/uuid?token=SECRET_TOKEN_XYZ&expires=1700000000&autoplay=true"
    sanitized = sanitize_url_for_logging(raw)
    assert "SECRET_TOKEN_XYZ" not in sanitized
    assert "token=[redacted]" in sanitized
    assert "expires=[redacted]" in sanitized
    assert "autoplay=[redacted]" in sanitized

    # URL without query string
    simple = "https://iframe.mediadelivery.net/embed/123/uuid"
    assert sanitize_url_for_logging(simple) == simple


def test_sanitize_url_for_display():
    """Verify display URL formatting safely trims and removes query tokens for UI presentation."""
    raw = "https://iframe.mediadelivery.net/embed/123/af5aa834-acd6-47bc-a800-32ad1a8f7d92?token=SUPER_LONG_TOKEN_STRING"
    display = sanitize_url_for_display(raw, max_length=65)
    assert "token=" not in display
    assert display.startswith("https://iframe.mediadelivery.net/embed/123/af5aa834-")

