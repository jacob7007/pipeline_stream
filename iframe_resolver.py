import requests
from datetime import datetime
import logger
from scrapers import SCRAPER_PLUGINS
from utils import DEFAULT_HEADERS

# Build normalized plugin registry using short module names
PLUGIN_REGISTRY = {p.__name__.split(".")[-1]: p for p in SCRAPER_PLUGINS}

def is_stream_playable(ch: dict, proxies: dict = None) -> bool:
    """Verifies that a channel is reachable and valid via dynamic network health checks."""
    if not isinstance(ch, dict):
        return False
    ctype = ch.get("type", "").strip().lower()

    if ctype == "shaka":
        manifest = ch.get("manifest", "")
        keys = ch.get("keys", {})
        if not manifest or not manifest.startswith(("http://", "https://")):
            return False
        # If DASH stream is encrypted (cenc.mpd or /enc/), it MUST have non-empty ClearKeys
        if ("cenc.mpd" in manifest or "/enc/" in manifest) and (not keys or len(keys) == 0):
            return False
        try:
            r = requests.get(manifest, headers={**DEFAULT_HEADERS, "Range": "bytes=0-1024"}, timeout=4, proxies=proxies)
            return r.status_code in (200, 206)
        except Exception:
            return False

    elif ctype == "hls":
        url = ch.get("url", "")
        if not url or not url.startswith(("http://", "https://")):
            return False
        try:
            r = requests.get(url, headers={**DEFAULT_HEADERS, "Range": "bytes=0-1024"}, timeout=4, proxies=proxies)
            return r.status_code in (200, 206)
        except Exception:
            return False

    elif ctype == "iframe":
        url = ch.get("url", "")
        if not url or not url.startswith(("http://", "https://")):
            return False
        try:
            r = requests.get(url, headers=DEFAULT_HEADERS, timeout=4, proxies=proxies)
            return r.status_code in (200, 206, 301, 302)
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
    context: dict = None
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

    if not raw_channels and hasattr(plugin, "extract_iframe"):
        try:
            iframe_url = plugin.extract_iframe(match_url, proxies=proxies, context=context) or ""
            if iframe_url:
                raw_channels = [{
                    "id": 1, "name": "Live 1", "quality": "HD", "type": "iframe", "url": iframe_url
                }]
        except Exception as e:
            logger.error(f"Plugin '{plugin_name}' raised error extracting iframe for {match_url}: {e}")

    # Validate each channel so broken/dead/unkeyed streams are dropped
    valid_channels = [ch for ch in raw_channels if is_stream_playable(ch, proxies=proxies)]

    # Sort channels by requested priority tier
    valid_channels.sort(key=lambda ch: (
        get_channel_priority(ch),
        int(ch.get("id", 999)) if str(ch.get("id", "")).isdigit() else 999
    ))

    # Re-number sequential Live 1, Live 2 labels and IDs
    for idx, ch in enumerate(valid_channels, start=1):
        ch["id"] = idx
        ch["name"] = f"Live {idx}"

    return valid_channels


# Alias for backward compatibility
resolve_match_iframe = resolve_match_channels

