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
    "b-cdn.net",
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


def normalize_url(raw_url: str) -> str:
    """Normalize extracted URL safely while preserving query parameters and tokens.

    Strips trailing sentence punctuation, unclosed brackets, and surrounding quotes.
    """
    if not raw_url or not isinstance(raw_url, str):
        return ""

    url = raw_url.strip()
    # Strip wrapping quotes, brackets, angle brackets
    url = url.strip('\'"`<>')

    # If wrapped in balanced parentheses, brackets or braces, strip outer pair
    while (
        (url.startswith("(") and url.endswith(")")) or
        (url.startswith("[") and url.endswith("]")) or
        (url.startswith("{") and url.endswith("}")) or
        (url.startswith("<") and url.endswith(">"))
    ):
        url = url[1:-1].strip()

    # Strip trailing punctuation that is not part of query parameters/path
    # (e.g. sentences ending in '.', ',', ';', '!', or unclosed ')')
    while url:
        if url.endswith(('"', "'", '>', '`')):
            url = url[:-1]
        elif url.endswith(('.', ',', ';', ':', '!', '?')):
            url = url[:-1]
        elif url.endswith(')') and url.count('(') < url.count(')'):
            url = url[:-1]
        elif url.endswith(']') and url.count('[') < url.count(']'):
            url = url[:-1]
        elif url.endswith('}') and url.count('{') < url.count('}'):
            url = url[:-1]
        else:
            break

    return url.strip()


def extract_urls(text: Optional[str], entities: Optional[list] = None) -> list[str]:
    """Extract and normalize all URLs from text and Telegram message entities.

    Preserves query parameters, tokens, and order while removing duplicates.
    Supports plain text URLs, surrounding text, and Telegram URL/TEXT_LINK entities.
    """
    found_urls: list[str] = []
    seen: set = set()

    def _add_url(u: Optional[str]):
        if not u:
            return
        cleaned = normalize_url(u)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            found_urls.append(cleaned)

    # 1. Extract from Telegram entities if present
    if entities:
        for ent in entities:
            # Check type safely whether it's an enum, object, or mock
            ent_type = getattr(ent, "type", None)
            ent_type_str = getattr(ent_type, "name", str(ent_type)).lower()

            if "text_link" in ent_type_str and getattr(ent, "url", None):
                _add_url(ent.url)
            elif "url" in ent_type_str and text:
                offset = getattr(ent, "offset", 0)
                length = getattr(ent, "length", 0)
                if length > 0:
                    raw_u = text[offset : offset + length]
                    _add_url(raw_u)

    # 2. Extract from plain text using regex
    if text:
        url_pattern = re.compile(r'https?://[^\s<>"]+', re.IGNORECASE)
        for match in url_pattern.finditer(text):
            _add_url(match.group(0))

    return found_urls


def sanitize_url_for_logging(url: str) -> str:
    """Sanitize URL to prevent leaking sensitive tokens or query parameters in logs."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        # Keep param keys but redact values
        parts = []
        for pair in parsed.query.split("&"):
            if "=" in pair:
                k, _ = pair.split("=", 1)
                parts.append(f"{k}=[redacted]")
            else:
                parts.append(pair)
        redacted_query = "&".join(parts)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{redacted_query}"
    except Exception:
        return "[redacted-url]"


def sanitize_url_for_display(url: str, max_length: int = 80) -> str:
    """Format URL safely for Telegram HTML UI display without exposing full token."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        display = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if len(display) > max_length:
            return display[:max_length - 3] + "..."
        return display
    except Exception:
        return url[:max_length]

