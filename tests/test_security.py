import pytest
from app.utils.security import (
    validate_url,
    sanitize_subprocess_arg,
    RateLimiter,
)
from app.utils.formatters import sanitize_filename
from config import Config


def test_ssrf_protection_private_ips():
    """Verify private and internal network addresses are rejected."""
    bad_urls = [
        "http://127.0.0.1/secret",
        "http://localhost:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://10.0.0.5/api",
        "http://192.168.1.100/router",
        "http://172.16.0.10/internal",
        "http://[::1]/secret",
    ]
    for url in bad_urls:
        valid, msg = validate_url(url)
        assert valid is False, f"URL should be blocked: {url}"


def test_subprocess_arg_sanitization():
    """Verify control characters and null bytes are stripped."""
    dangerous_input = "test\x00arg\x07with\x1fcontrols"
    sanitized = sanitize_subprocess_arg(dangerous_input)
    assert "\x00" not in sanitized
    assert "\x07" not in sanitized
    assert "\x1f" not in sanitized
    assert sanitized == "testargwithcontrols"


def test_filename_sanitization():
    """Verify filenames are sanitized against path traversal and forbidden characters."""
    bad_name = '../../../etc/passwd:something*?<>|\\file.mp4'
    safe = sanitize_filename(bad_name)
    assert "../" not in safe
    assert ":" not in safe
    assert "*" not in safe
    assert "?" not in safe
    assert "<" not in safe
    assert ">" not in safe
    assert "|" not in safe
    assert "\\" not in safe

    # Length limit test
    long_name = "a" * 300 + ".mp4"
    truncated = sanitize_filename(long_name, max_length=100)
    assert len(truncated) <= 100
    assert truncated.endswith(".mp4")


def test_rate_limiter():
    """Verify sliding-window per-user rate limiting."""
    limiter = RateLimiter(max_requests=3, window_seconds=60)
    user_id = 998877

    # First 3 requests allowed
    assert limiter.is_allowed(user_id) is True
    assert limiter.is_allowed(user_id) is True
    assert limiter.is_allowed(user_id) is True

    # 4th request blocked
    assert limiter.is_allowed(user_id) is False
    assert limiter.remaining(user_id) == 0

    # Another user is independent
    assert limiter.is_allowed(112233) is True


def test_admin_authorization():
    """Verify role-based authorization for owner, admin, and regular user."""
    cfg = Config(
        OWNER_ID=10001,
        ADMIN_IDS=[10002, 10003]
    )

    # Owner checks
    assert cfg.is_owner(10001) is True
    assert cfg.is_admin(10001) is True

    # Admin checks
    assert cfg.is_owner(10002) is False
    assert cfg.is_admin(10002) is True
    assert cfg.is_admin(10003) is True

    # Regular user checks
    assert cfg.is_owner(99999) is False
    assert cfg.is_admin(99999) is False
