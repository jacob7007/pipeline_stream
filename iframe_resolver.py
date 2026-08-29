import requests
import logger
from scrapers import SCRAPER_PLUGINS
from utils import DEFAULT_HEADERS

# Build normalized plugin registry using short module names
PLUGIN_REGISTRY = {p.__name__.split(".")[-1]: p for p in SCRAPER_PLUGINS}

# Known block/anti-embed phrases that indicate a stream returned 200 OK but is actually denied.
# Checked case-insensitively against the first ~4KB of the response body.
_BLOCK_PHRASES = [
    "not allowed",
    "domain protected",
    "domain is not allowed",
    "unauthorized domain",
    "access denied",
    "embed not allowed",
    "video disabled",
    "stream is offline",
    "video has been blocked",
    "stream is protected",
    "invalid referer",
    "anti-embed",
    # Arabic equivalents
    "غير مسموح",
    "البث محمي",
    "تم حظر",
    "نطاق غير مصرح",
]


def _is_blocked_by_content(response_text: str) -> bool:
    """Returns True if the response body signals an anti-embed or domain block."""
    sample = response_text[:4096].lower()
    return any(phrase in sample for phrase in _BLOCK_PHRASES)


def _is_blocked_by_headers(headers: dict) -> bool:
    """Returns True if HTTP response headers forbid embedding in an iframe."""
    xfo = headers.get("X-Frame-Options", "").strip().upper()
    if xfo in ("DENY", "SAMEORIGIN"):
        return True

    csp = headers.get("Content-Security-Policy", "")
    if "frame-ancestors" in csp:
        # Allow only if the policy explicitly includes wildcard or all origins
        if "frame-ancestors 'none'" in csp or (
            "frame-ancestors" in csp
            and "frame-ancestors *" not in csp
            and "frame-ancestors https:" not in csp
        ):
            return True

    return False


def is_stream_playable(ch: dict, proxies: dict = None) -> bool:
    """
    Verifies a channel is reachable and genuinely playable — not just HTTP 200.
    Four layers of validation:
      1. Shaka DASH: manifest reachable + ClearKeys present for encrypted streams + MPD content check
      2. HLS: URL reachable + response starts with #EXTM3U (not an HTML error page)
      3. Iframe: URL reachable + no anti-embed headers + no block-phrase body text
    """
    if not isinstance(ch, dict):
        return False
    ctype = ch.get("type", "").strip().lower()

    if ctype == "shaka":
        manifest = ch.get("manifest", "")
        keys = ch.get("keys", {})
        if not manifest or not manifest.startswith(("http://", "https://")):
            return False
        # Encrypted DASH streams must have at least one ClearKey pair
        if ("cenc.mpd" in manifest or "/enc/" in manifest) and not keys:
            return False
        try:
            r = requests.get(
                manifest,
                headers={**DEFAULT_HEADERS, "Range": "bytes=0-4096"},
                timeout=4,
                proxies=proxies,
            )
            if r.status_code not in (200, 206):
                return False
            # Confirm the manifest is actually an MPD XML document, not an HTML error page
            if r.text.lstrip().startswith(("<!DOCTYPE", "<html")):
                return False
            return True
        except Exception:
            return False

    elif ctype == "hls":
        url = ch.get("url", "")
        if not url or not url.startswith(("http://", "https://")):
            return False
        try:
            r = requests.get(
                url,
                headers={**DEFAULT_HEADERS, "Range": "bytes=0-4096"},
                timeout=4,
                proxies=proxies,
            )
            if r.status_code not in (200, 206):
                return False
            # A valid HLS manifest must start with the #EXTM3U tag
            if not r.text.lstrip().startswith("#EXTM3U"):
                return False
            return True
        except Exception:
            return False

    elif ctype == "iframe":
        url = ch.get("url", "")
        if not url or not url.startswith(("http://", "https://")):
            return False
        try:
            r = requests.get(url, headers=DEFAULT_HEADERS, timeout=4, proxies=proxies)
            if r.status_code not in (200, 206, 301, 302):
                return False
            # Drop iframes whose server forbids embedding via HTTP headers
            if _is_blocked_by_headers(dict(r.headers)):
                return False
            # Drop iframes that returned 200 but show a domain-block error page
            if _is_blocked_by_content(r.text):
                return False
            return True
        except Exception:
            return False

    return True


def get_channel_priority(ch: dict) -> int:
    """
    Returns priority tier for channel ordering (lower number = higher priority):
    Tier 1: OK.ru
    Tier 2: YouTube
    Tier 3: SIR TV / YasirTV Player embeds
    Tier 4: FHD DRM (Shaka ClearKey DASH)
    Tier 5: Native HLS (.m3u8)
    Tier 6: Other web iframes / embeds
    """
    ctype = (ch.get("type") or "").strip().lower()
    quality = (ch.get("quality") or "").strip().lower()
    url = (ch.get("url") or ch.get("manifest") or "").lower()

    if quality == "ok.ru" or "ok.ru" in url:
        return 1
    if quality == "youtube" or "youtube.com" in url or "youtu.be" in url:
        return 2
    if "yasirtv.com" in url or "sir-tv" in url or "tvsir" in url:
        return 3
    if ctype == "shaka" or "drm" in quality:
        return 4
    if ctype == "hls" or "hls" in quality:
        return 5
    return 6


def resolve_match_channels(
    match_url: str,
    status_class: str,
    is_far_future: bool,
    plugin_name: str,
    proxies: dict = None,
    context: dict = None,
) -> list[dict]:
    """
    Extracts and validates multi-stream channels for a match from the appropriate scraper plugin.
    Returns only verified, working channels sorted by priority:
    1. OK.ru -> 2. YouTube -> 3. SIR TV / YasirTV -> 4. FHD DRM -> 5. HLS -> 6. Others
    """
    if not match_url or status_class == "finished" or is_far_future:
        return []

    plugin = PLUGIN_REGISTRY.get(plugin_name)
    if plugin is None:
        logger.error(f"No plugin found in registry for: '{plugin_name}'")
        return []

    raw_channels = []
    if hasattr(plugin, "extract_channels"):
        try:
            raw_channels = plugin.extract_channels(match_url, proxies=proxies) or []
        except Exception as ex:
            logger.warning(f"Plugin '{plugin_name}' failed to extract channels: {ex}")

    # Validate each channel — drops dead streams, blocked iframes, and unkeyed DRM
    valid_channels = [ch for ch in raw_channels if is_stream_playable(ch, proxies=proxies)]

    # Sort by priority tier, then original channel order as tiebreaker
    valid_channels.sort(key=lambda ch: (
        get_channel_priority(ch),
        int(ch.get("id", 999)) if str(ch.get("id", "")).isdigit() else 999,
    ))

    # Re-number to sequential Live 1, Live 2, ... labels
    for idx, ch in enumerate(valid_channels, start=1):
        ch["id"] = idx
        ch["name"] = f"Live {idx}"

    return valid_channels


# Alias for backward compatibility
resolve_match_iframe = resolve_match_channels
