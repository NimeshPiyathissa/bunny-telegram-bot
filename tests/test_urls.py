import pytest
from app.utils.security import (
    validate_url,
    is_bunny_url,
    parse_bunny_embed_url,
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
