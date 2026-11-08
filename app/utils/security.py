import ipaddress
import logging
import re
import socket
import time
from collections import defaultdict
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Allowed URL schemes
ALLOWED_SCHEMES = {"http", "https"}

# Allowed domains for Bunny Stream
ALLOWED_DOMAINS = {
    "iframe.mediadelivery.net",
    "video.bunnycdn.com",
    "vz-cdn.net",
}

# Blocked IP ranges (SSRF protection)
BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("10.0.0.0/8"),        # Private A
    ipaddress.ip_network("172.16.0.0/12"),     # Private B
    ipaddress.ip_network("192.168.0.0/16"),    # Private C
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local
    ipaddress.ip_network("0.0.0.0/8"),         # Current network
    ipaddress.ip_network("100.64.0.0/10"),     # Shared address
    ipaddress.ip_network("198.18.0.0/15"),     # Benchmarking
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 private
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]

# Cloud metadata endpoints
BLOCKED_HOSTS = {
    "metadata.google.internal",
    "metadata.google.com",
    "169.254.169.254",
}


class RateLimiter:
    """Per-user rate limiter."""

    def __init__(self, max_requests: int = 5, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[int, list[float]] = defaultdict(list)

    def is_allowed(self, user_id: int) -> bool:
        now = time.monotonic()
        # Clean old entries
        self._requests[user_id] = [
            t for t in self._requests[user_id]
            if now - t < self.window_seconds
        ]
        if len(self._requests[user_id]) >= self.max_requests:
            return False
        self._requests[user_id].append(now)
        return True

    def remaining(self, user_id: int) -> int:
        now = time.monotonic()
        self._requests[user_id] = [
            t for t in self._requests[user_id]
            if now - t < self.window_seconds
        ]
        return max(0, self.max_requests - len(self._requests[user_id]))


# Global rate limiter
rate_limiter = RateLimiter(max_requests=10, window_seconds=60)


def validate_url(url: str) -> Tuple[bool, str]:
    """Validate a URL for safety and supported patterns.

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not url or not isinstance(url, str):
        return False, "URL is empty or invalid."

    url = url.strip()

    # Length check
    if len(url) > 2048:
        return False, "URL is too long."

    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Could not parse URL."

    # Scheme check
    if parsed.scheme not in ALLOWED_SCHEMES:
        return False, f"URL scheme '{parsed.scheme}' is not supported. Only HTTP/HTTPS allowed."

    # Host check
    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no hostname."

    # Block metadata endpoints
    if hostname.lower() in BLOCKED_HOSTS:
        return False, "This URL target is not allowed."

    # Check against blocked IPs
    try:
        ips = socket.getaddrinfo(hostname, None)
        for _, _, _, _, addr in ips:
            ip = ipaddress.ip_address(addr[0])
            for network in BLOCKED_NETWORKS:
                if ip in network:
                    return False, "This URL resolves to a blocked address."
    except socket.gaierror:
        return False, "Could not resolve hostname."
    except (ValueError, OSError):
        pass  # Non-IP hostname, will be checked by domain allowlist

    return True, ""


def is_bunny_url(url: str) -> bool:
    """Check if URL is a recognized Bunny Stream URL."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in ALLOWED_DOMAINS
        )
    except Exception:
        return False


def parse_bunny_embed_url(url: str) -> Optional[dict]:
    """Parse a Bunny Stream embed URL to extract library ID and video GUID.

    Expected format:
    https://iframe.mediadelivery.net/embed/{library_id}/{video_guid}?params
    """
    try:
        parsed = urlparse(url)
        if parsed.hostname != "iframe.mediadelivery.net":
            return None

        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 3 or path_parts[0] != "embed":
            return None

        library_id = path_parts[1]
        video_guid = path_parts[2]

        # Validate library_id is numeric
        if not library_id.isdigit():
            return None

        # Validate video_guid format (UUID-like)
        uuid_pattern = re.compile(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I
        )
        if not uuid_pattern.match(video_guid):
            return None

        return {
            "library_id": library_id,
            "video_guid": video_guid,
            "original_url": url,
            "params": dict(
                part.split("=", 1) for part in (parsed.query or "").split("&")
                if "=" in part
            ),
        }
    except Exception as e:
        logger.warning(f"Failed to parse Bunny URL: {e}")
        return None


def sanitize_subprocess_arg(arg: str) -> str:
    """Sanitize an argument for subprocess use."""
    # Remove null bytes and control characters
    arg = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', arg)
    return arg
