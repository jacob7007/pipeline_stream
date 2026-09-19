import os
import re
import urllib.parse
import requests
import logger
import sheets_client
from scrapers import SCRAPER_PLUGINS
from utils import DEFAULT_HEADERS, format_to_human_time, get_now_local, resolve_timezone

# Build normalized plugin registry supporting PLUGIN_NAME and module name
PLUGIN_REGISTRY = {}
for p in SCRAPER_PLUGINS:
    p_name = getattr(p, "PLUGIN_NAME", None)
    if p_name:
        PLUGIN_REGISTRY[p_name] = p
    PLUGIN_REGISTRY[p.__name__.split(".")[-1]] = p

# ---------------------------------------------------------------------------
# Module-level state — initialised by init_domain_cache() at pipeline start.
# ---------------------------------------------------------------------------
_domain_cache: dict = {}            # {domain: {"status": "OK"|"NO"|"--"}}
_p1_rules: list = []                # [(domain, quality_badge), ...] (loaded dynamically from Google Sheets)
_domain_cache_dirty: bool = False   # True when any new probe result was written this run
_pending_alerts: list = []          # "--" results waiting for end-of-run Telegram dispatch
_DYNAMIC_SANDBOX_ERRORS: list[str] = []

# Timeouts in milliseconds for Playwright probing (configurable via env vars).
_DEFAULT_TIMEOUT_MS = int(os.environ.get("PROBE_TIMEOUT_MS", 14_000))
_SETTLE_MS = int(os.environ.get("PROBE_SETTLE_MS", 4_000))


# ---------------------------------------------------------------------------
# Dynamic Sandbox Error Phrases Configuration
# ---------------------------------------------------------------------------

def set_sandbox_errors(errors: list[str]) -> None:
    """Updates the dynamic sandbox error phrases loaded from Google Sheets."""
    global _DYNAMIC_SANDBOX_ERRORS
    _DYNAMIC_SANDBOX_ERRORS = [phrase.strip().lower() for phrase in errors if phrase and phrase.strip()]


def get_sandbox_errors() -> list[str]:
    """Returns the current list of dynamic sandbox error phrases."""
    return list(_DYNAMIC_SANDBOX_ERRORS)


# ---------------------------------------------------------------------------
# Domain cache lifecycle — called from run_pipeline.py
# ---------------------------------------------------------------------------

def init_domain_cache(client, spreadsheet_name: str) -> None:
    """Loads the _cache_domains sheet into memory at the start of the pipeline run."""
    global _domain_cache, _p1_rules, _domain_cache_dirty, _pending_alerts
    _domain_cache, _p1_rules, sandbox_errors = sheets_client.load_domain_cache(client, spreadsheet_name)
    set_sandbox_errors(sandbox_errors)
    _domain_cache_dirty = False
    _pending_alerts = []


def set_p1_rules(rules: list) -> None:
    """Sets dynamic P1 rules (useful for testing)."""
    global _p1_rules
    _p1_rules = list(rules)


def flush_domain_cache(client, spreadsheet_name: str) -> None:
    """Writes the in-memory domain cache back to Sheets — only if a probe ran this run."""
    global _domain_cache_dirty
    if not _domain_cache_dirty:
        return
    sheets_client.save_domain_cache(client, _domain_cache, spreadsheet_name)
    _domain_cache_dirty = False


def get_pending_alerts() -> list:
    """Returns all '--' domain alert dicts collected during this run for Telegram dispatch."""
    return list(_pending_alerts)


# ---------------------------------------------------------------------------
# URL and Domain Extraction / Unwrapping Utilities
# ---------------------------------------------------------------------------

def unwrap_redirector_url(url: str) -> str:
    """Strips redirector wrappers (e.g. href.li, anonym.to, dereferer.me)."""
    if not url:
        return ""
    m = re.match(r"^https?://(?:www\.)?(?:href\.li|anonym\.to|dereferer\.me)/\?(https?://.+)$", url, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return url


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
    errors = get_sandbox_errors()
    return any(phrase in sample for phrase in errors)


# ---------------------------------------------------------------------------
# Browser-based Sandbox Iframe Probing (Playwright)
# ---------------------------------------------------------------------------

def _build_probe_html(url: str) -> str:
    """Returns a minimal HTML page embedding the candidate URL in a sandboxed iframe."""
    escaped = url.replace('"', "%22")
    return (
        "<!DOCTYPE html><html><head>"
        "<meta charset='utf-8'>"
        "<style>*{margin:0;padding:0}body,html{width:100%;height:100%}</style>"
        "</head><body>"
        f'<iframe id="probe" '
        f'sandbox="allow-scripts allow-same-origin allow-presentation allow-forms" '
        f'src="{escaped}" '
        f'allow="autoplay; fullscreen; picture-in-picture; encrypted-media" '
        f'style="width:100%;height:100vh;border:0">'
        f'</iframe>'
        "</body></html>"
    )


def _check_frames_for_errors(page) -> str | None:
    """
    Iterates all frames on the page and checks rendered text and content for
    dynamic sandbox error phrases loaded from Google Sheets.
    Returns the matched error phrase string if found, otherwise None.
    """
    if not _DYNAMIC_SANDBOX_ERRORS:
        return None

    for frame in page.frames:
        try:
            text = (frame.inner_text("body", timeout=500) or "").lower()
            for phrase in _DYNAMIC_SANDBOX_ERRORS:
                if phrase in text:
                    return phrase
        except Exception:
            pass
        try:
            content = (frame.content() or "").lower()
            for phrase in _DYNAMIC_SANDBOX_ERRORS:
                if phrase in content:
                    return phrase
        except Exception:
            pass
    return None


def probe_url(url: str, timeout_ms: int = None) -> dict:
    """
    Tests whether a candidate iframe URL works inside a sandboxed iframe.

    Returns:
        {"status": "NO", "error_phrase": "..."}  -> If blocked by sandbox / HTTP error / error phrase.
        {"status": "--", "error_phrase": None}   -> If rendered without sandbox errors.
    """
    if timeout_ms is None:
        timeout_ms = _DEFAULT_TIMEOUT_MS

    clean_url = unwrap_redirector_url(url)
    if "games.ok.ru/videoembed" in clean_url and "autoplay=0" in clean_url:
        clean_url = clean_url.replace("autoplay=0", "autoplay=1")

    # Fast HTTP reachability & error pre-check (~200ms)
    try:
        r_pre = requests.get(clean_url, headers={**DEFAULT_HEADERS, "Referer": "https://footyy.footyy.com/"}, timeout=8)
        if r_pre.status_code in (404, 410, 500, 502, 503):
            return {"status": "NO", "error_phrase": f"HTTP {r_pre.status_code}"}
        sample_text = r_pre.text[:2048].lower()
        for phrase in _DYNAMIC_SANDBOX_ERRORS:
            if phrase in sample_text:
                return {"status": "NO", "error_phrase": phrase}
    except Exception:
        pass

    # Lazy import Playwright
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        logger.error("channels_engine: Playwright is not installed. Run: playwright install chromium")
        return {"status": "--", "error_phrase": None}

    http_error_code = None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-web-security",       # allows reading cross-origin frame DOM
                    "--no-sandbox",                 # required on Linux CI runners
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",      # avoids /dev/shm space issues on CI
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            def on_response(response):
                nonlocal http_error_code
                if response.url == clean_url and response.status >= 400:
                    http_error_code = response.status

            page.on("response", on_response)

            # Build the probe page as a data URI
            probe_html = _build_probe_html(clean_url)
            data_uri = "data:text/html;charset=utf-8," + urllib.parse.quote(probe_html)

            try:
                page.goto(data_uri, timeout=timeout_ms, wait_until="domcontentloaded")
            except PlaywrightTimeout:
                browser.close()
                return {"status": "--", "error_phrase": "page load timeout"}

            # Allow settle time for delayed sandbox-detection scripts to fire
            try:
                page.wait_for_timeout(_SETTLE_MS)
            except Exception:
                pass

            # 1. Main frame HTTP status error check
            if http_error_code:
                browser.close()
                return {"status": "NO", "error_phrase": f"HTTP {http_error_code}"}

            # 2. Sandbox rejection or error phrase in rendered DOM
            matched_phrase = _check_frames_for_errors(page)
            if matched_phrase:
                browser.close()
                return {"status": "NO", "error_phrase": matched_phrase}

            # 3. Not blocked by sandbox -> Unverified candidate (ready for manual review)
            browser.close()
            return {"status": "--", "error_phrase": None}

    except Exception as ex:
        logger.warning(f"channels_engine: Unhandled probe error for {url}: {ex}")
        return {"status": "--", "error_phrase": None}


# ---------------------------------------------------------------------------
# Main Stream Playability Check
# ---------------------------------------------------------------------------

def is_stream_playable(ch: dict, proxies: dict = None, match_context: dict = None) -> bool:
    """
    Verifies a channel is reachable and genuinely playable — not just HTTP 200.
    Validation strategy per channel type:
      1. DASH: manifest reachable + ClearKeys present for encrypted streams + MPD content check
      2. HLS: URL reachable + response starts with #EXTM3U (not an HTML error page)
      3. Iframe: HTTP reachability + header check + content check + domain cache lookup + browser validation
         (Playwright browser only runs for domains not yet in the _cache_domains sheet)

    match_context (optional): {"match_name": str, "channel_name": str, "event_id": str, "blog_post_id": str}
        Used to populate Telegram alerts when a probe returns "--".
    """
    if not isinstance(ch, dict):
        return False
    ctype = ch.get("type", "").strip().lower()

    if ctype == "dash":
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

        url = unwrap_redirector_url(raw_url)
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
            status = cached.get("status", "") if isinstance(cached, dict) else str(cached)
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

        result = probe_url(url)
        probe_status = result.get("status", "--")
        error_phrase = result.get("error_phrase", "")

        # Persist result to in-memory cache (flushed to Sheets at end of run).
        global _domain_cache_dirty
        if domain:
            _domain_cache[domain] = {
                "status": probe_status,
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

def get_channel_priority(ch: dict) -> tuple:
    """
    Returns priority rank tuple (tier, sub_rank) for channel ordering (lower = higher priority):
    Tier 1: Dynamic P1 domains from Google Sheets (sub-ranked by order in sheets)
    Tier 2: Other iFrames (any iframe not matching P1 sheet domains)
    Tier 3: Native HLS (.m3u8)
    Tier 4: DASH (.mpd)
    """
    ctype = (ch.get("type") or "").strip().lower()
    url = (ch.get("url") or ch.get("manifest") or "").lower()

    if ctype == "iframe":
        domain = _extract_domain(url)
        # Check dynamic P1 rules from Google Sheets (Zero hardcoded rules)
        for idx, rule in enumerate(_p1_rules):
            p1_dom = rule[0]
            p1_qual = rule[1]
            p1_sandbox = rule[2] if len(rule) > 2 else False
            if domain == p1_dom or domain.endswith("." + p1_dom) or p1_dom in url:
                if p1_qual:
                    ch["quality"] = p1_qual
                if p1_sandbox:
                    ch["sandbox"] = "allow-scripts allow-same-origin allow-presentation allow-forms"
                else:
                    ch.pop("sandbox", None)
                return (1, idx)
        return (2, 999)

    if ctype == "hls":
        return (3, 999)

    if ctype == "dash":
        return (4, 999)

    return (2, 999)


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
    1. Dynamic P1 Domains (from Sheets) -> 2. Other iFrames -> 3. HLS -> 4. DASH

    context (optional): passed from the pipeline; may contain match_name, event_id, blog_post_id, etc.
    """
    if not match_url or status_class == "finished" or is_far_future:
        return []

    engine_name = plugin_name.split("/")[0] if "/" in plugin_name else plugin_name
    plugin = PLUGIN_REGISTRY.get(plugin_name) or PLUGIN_REGISTRY.get(engine_name)
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
