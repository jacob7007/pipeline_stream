import requests
import logger
import sheets_module
import iframe_validator
from scrapers import SCRAPER_PLUGINS
from utils import DEFAULT_HEADERS, format_to_human_time, get_now_local, resolve_timezone

# Build normalized plugin registry using short module names
PLUGIN_REGISTRY = {p.__name__.split(".")[-1]: p for p in SCRAPER_PLUGINS}

# ---------------------------------------------------------------------------
# Module-level state — initialised by init_domain_cache() at pipeline start.
# ---------------------------------------------------------------------------
_domain_cache: dict = {}       # {domain: {"status": "OK"|"NO"|"--", "failure_reason": ..., "last_tested": ...}}
_domain_cache_dirty: bool = False   # True when any new probe result was written this run
_pending_alerts: list = []     # "--" results waiting for end-of-run Telegram dispatch


# ---------------------------------------------------------------------------
# Domain cache lifecycle — called from run_pipeline.py
# ---------------------------------------------------------------------------

def init_domain_cache(sheets_client, spreadsheet_name: str) -> None:
    """Loads the _cache_domains sheet into memory at the start of the pipeline run."""
    global _domain_cache, _domain_cache_dirty, _pending_alerts
    _domain_cache = sheets_module.load_domain_cache(sheets_client, spreadsheet_name)
    _domain_cache_dirty = False
    _pending_alerts = []


def flush_domain_cache(sheets_client, spreadsheet_name: str) -> None:
    """Writes the in-memory domain cache back to Sheets — only if a probe ran this run."""
    global _domain_cache_dirty
    if not _domain_cache_dirty:
        return
    sheets_module.save_domain_cache(sheets_client, _domain_cache, spreadsheet_name)
    _domain_cache_dirty = False


def get_pending_alerts() -> list:
    """Returns all '--' domain alert dicts collected during this run for Telegram dispatch."""
    return list(_pending_alerts)


# ---------------------------------------------------------------------------
# Domain extraction
# ---------------------------------------------------------------------------

def _extract_domain(url: str) -> str:
    """Extracts the registered domain from a URL using tldextract, with urllib fallback."""
    try:
        import tldextract
        ext = tldextract.extract(url)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}".lower()
    except Exception:
        pass
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        parts = host.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# HTTP-level checks (fast, same request as reachability)
# ---------------------------------------------------------------------------

def _is_blocked_by_headers(headers: dict) -> bool:
    """Returns True if HTTP response headers forbid embedding in an iframe.
    X-Frame-Options and CSP frame-ancestors are enforced by the browser itself —
    if these headers are set, the iframe will always fail regardless of sandbox."""
    if not isinstance(headers, dict):
        return False

    h_lower = {str(k).lower(): str(v) for k, v in headers.items()}
    xfo = h_lower.get("x-frame-options", "").strip().upper()
    if xfo in ("DENY", "SAMEORIGIN"):
        return True

    csp = h_lower.get("content-security-policy", "").lower()
    if "frame-ancestors" in csp:
        if "frame-ancestors 'none'" in csp or (
            "frame-ancestors" in csp
            and "frame-ancestors *" not in csp
            and "frame-ancestors https:" not in csp
        ):
            return True

    return False


def _is_blocked_by_content(response_text: str) -> bool:
    """Returns True if the response body signals an anti-embed or domain block."""
    sample = (response_text or "")[:4096].lower()
    return any(phrase in sample for phrase in iframe_validator._SANDBOX_ERROR_PHRASES)


# ---------------------------------------------------------------------------
# Main validation entry point
# ---------------------------------------------------------------------------

def is_stream_playable(ch: dict, proxies: dict = None, match_context: dict = None) -> bool:
    """
    Verifies a channel is reachable and genuinely playable — not just HTTP 200.
    Validation strategy per channel type:
      1. Shaka DASH: manifest reachable + ClearKeys present for encrypted streams + MPD content check
      2. HLS: URL reachable + response starts with #EXTM3U (not an HTML error page)
      3. Iframe: HTTP reachability + header check + content check + domain cache lookup + browser validation
         (Playwright browser only runs for domains not yet in the _cache_domains sheet)

    match_context (optional): {"match_name": str, "channel_name": str, "event_id": str, "blog_post_id": str}
        Used to populate Telegram alerts when a probe returns "--".
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
        raw_url = ch.get("url", "")
        if not raw_url or not raw_url.startswith(("http://", "https://")):
            return False

        url = iframe_validator.unwrap_redirector_url(raw_url)
        ch["url"] = url

        # --- Step 1: HTTP reachability + header check + content block check (fast, ~100ms) ---
        try:
            r = requests.get(url, headers=DEFAULT_HEADERS, timeout=6, proxies=proxies)
            if r.status_code not in (200, 206, 301, 302):
                return False
            if _is_blocked_by_headers(dict(r.headers)):
                return False
            if _is_blocked_by_content(r.text):
                return False
        except Exception:
            return False

        # --- Step 2: Domain cache lookup ---
        domain = _extract_domain(url)
        cached = _domain_cache.get(domain) if domain else None

        if cached:
            status = cached.get("status", "")
            if status == "OK":
                return True
            if status == "NO":
                return False
            if status == "--":
                # Inconclusive from a previous probe — include with sandbox, no re-probe.
                return True
            # Unrecognised status value — fall through to probe.

        # --- Step 3: Browser probe for unknown domains ---
        match_name = (match_context or {}).get("match_name", "")
        match_suffix = f"  |  {match_name}." if match_name else "."

        print()
        logger.info(f"Scraper: '{domain}'{match_suffix}")

        result = iframe_validator.probe_url(url)
        probe_status = result.get("status", "--")
        probe_reason = result.get("failure_reason", "UNKNOWN")
        error_phrase = result.get("error_phrase", "")

        # Persist result to in-memory cache (flushed to Sheets at end of run).
        global _domain_cache_dirty
        now_str = format_to_human_time(
            get_now_local().replace(tzinfo=resolve_timezone(None)).isoformat()
        )
        if domain:
            _domain_cache[domain] = {
                "status": probe_status,
                "failure_reason": probe_reason,
                "last_tested": now_str,
            }
            _domain_cache_dirty = True

        if probe_status == "NO":
            err_detail = f' - "{error_phrase}"' if error_phrase else ""
            logger.error(f"Scraper: '{domain}' blocked{err_detail}.")
            return False

        if probe_status == "--":
            logger.success(f"Scraper: '{domain}' not blocked by sandbox.")
            logger.success("Scraper: Telegram alert sent.")
            _pending_alerts.append({
                "domain":       domain,
                "url":          url,
                "match_name":   match_name or "Unknown Match",
                "channel_name": (match_context or {}).get("channel_name", "Unknown Channel"),
                "event_id":     (match_context or {}).get("event_id", ""),
                "blog_post_id": (match_context or {}).get("blog_post_id", ""),
            })
            return True

        if probe_status == "OK":
            logger.success(f"Scraper: '{domain}' not blocked by sandbox.")
            return True

    return True


# ---------------------------------------------------------------------------
# Channel priority ordering
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main resolver — extracts, validates and sorts channels for a match
# ---------------------------------------------------------------------------

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

    context (optional): passed from the pipeline; may contain match_name, event_id, blog_post_id, etc.
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

    # Build match_context for browser probe alerts, enriched per channel below.
    match_name = (context or {}).get("match_name", "")
    event_id = (context or {}).get("event_id", "")
    blog_post_id = (context or {}).get("blog_post_id", "")

    # Validate each channel. For iframes, pass match_context so probe alerts are informative.
    valid_channels = []
    for ch in raw_channels:
        channel_name = ch.get("name", "")
        match_context = {
            "match_name":   match_name,
            "channel_name": channel_name,
            "event_id":     event_id,
            "blog_post_id": blog_post_id,
        }
        if is_stream_playable(ch, proxies=proxies, match_context=match_context):
            valid_channels.append(ch)

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
