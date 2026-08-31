import os
import re
import urllib.parse
import requests
import logger
from utils import DEFAULT_HEADERS

# ---------------------------------------------------------------------------
# Configurable sandbox rejection and fatal error phrases — checked case-insensitively.
# ---------------------------------------------------------------------------
_SANDBOX_ERROR_PHRASES = [
    # Anti-sandbox & Anti-embed Rejections
    "sandbox not allowed",
    "not allowed",
    "embed not allowed",
    "domain not allowed",
    "domain is not allowed",
    "unauthorized domain",
    "domain protected",
    "anti-embed",
    "invalid referer",
    "this content cannot be displayed in a frame",
    "cannot be displayed in a frame",
    "access denied",

    # HTTP / Server Status Error Text in Rendered DOM
    "403 forbidden",
    "403 forbidden nginx",
    "404 not found",
    "page not found",
    "the resource requested could not be found",
    "account suspended",
    "domain suspended",

    # Player Crash & Manifest Failures
    "could not play video",
    "there was a problem trying to load the video",
    "manifestloaderror",
    "networkerror_manifestloaderror",
    "cannot load m3u8",
    "error loading player",
    "error loading stream",
    "the media could not be loaded",
    "stream is offline",
    "stream offline",
    "channel offline",
    "video has been blocked",
    "video disabled",
    "video is unavailable",
    "this video is unavailable",

    # Arabic Error Phrases
    "غير مسموح",          # not allowed
    "البث محمي",          # stream protected
    "تم حظر",             # has been blocked
    "نطاق غير مصرح",       # unauthorized domain
    "غير مصرح به",        # unauthorized
    "البث متوقف",          # stream stopped / offline
    "القناة متوقفة",       # channel stopped
    "البث غير متوفر",      # stream unavailable
    "عذرا ، البث غير متوفر", # sorry, stream unavailable
    "تم إيقاف البث",       # stream was stopped
]

# Timeouts in milliseconds (configurable via env vars).
_DEFAULT_TIMEOUT_MS = int(os.environ.get("PROBE_TIMEOUT_MS", 14_000))
_SETTLE_MS = int(os.environ.get("PROBE_SETTLE_MS", 4_000))


def unwrap_redirector_url(url: str) -> str:
    """Strips redirector wrappers (e.g. href.li, anonym.to, dereferer.me)."""
    if not url:
        return ""
    m = re.match(r"^https?://(?:www\.)?(?:href\.li|anonym\.to|dereferer\.me)/\?(https?://.+)$", url, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return url


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
    known sandbox rejection and fatal error phrases.
    Returns the matched error phrase string if found, otherwise None.
    """
    for frame in page.frames:
        try:
            text = (frame.inner_text("body", timeout=500) or "").lower()
            for phrase in _SANDBOX_ERROR_PHRASES:
                if phrase in text:
                    return phrase
        except Exception:
            pass
        try:
            content = (frame.content() or "").lower()
            for phrase in _SANDBOX_ERROR_PHRASES:
                if phrase in content:
                    return phrase
        except Exception:
            pass
    return None


def probe_url(url: str, timeout_ms: int = None) -> dict:
    """
    Tests whether a candidate iframe URL works inside a sandboxed iframe.

    Returns:
        {"status": "NO", "failure_reason": "...", "error_phrase": "..."}  -> If blocked by sandbox / HTTP error / fatal error.
        {"status": "--", "failure_reason": "UNVERIFIED", "error_phrase": None} -> If rendered without sandbox errors.
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
            return {"status": "NO", "failure_reason": f"HTTP_{r_pre.status_code}", "error_phrase": f"HTTP {r_pre.status_code}"}
        sample_text = r_pre.text[:2048].lower()
        for phrase in _SANDBOX_ERROR_PHRASES:
            if phrase in sample_text:
                return {"status": "NO", "failure_reason": "PRECHECK_ERROR_PHRASE", "error_phrase": phrase}
    except Exception:
        pass

    # Lazy import Playwright
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        logger.error("iframe_validator: Playwright is not installed. Run: playwright install chromium")
        return {"status": "--", "failure_reason": "PLAYWRIGHT_NOT_INSTALLED", "error_phrase": None}

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
                return {"status": "--", "failure_reason": "TIMEOUT", "error_phrase": "page load timeout"}

            # Allow settle time for delayed sandbox-detection scripts to fire
            try:
                page.wait_for_timeout(_SETTLE_MS)
            except Exception:
                pass

            # 1. Main frame HTTP status error check
            if http_error_code:
                browser.close()
                return {"status": "NO", "failure_reason": f"HTTP_FRAME_ERROR_{http_error_code}", "error_phrase": f"HTTP {http_error_code}"}

            # 2. Sandbox rejection or fatal error phrase in rendered DOM
            matched_phrase = _check_frames_for_errors(page)
            if matched_phrase:
                browser.close()
                return {"status": "NO", "failure_reason": "SANDBOX_REJECTED", "error_phrase": matched_phrase}

            # 3. Not blocked by sandbox -> Unverified candidate (ready for manual review)
            browser.close()
            return {"status": "--", "failure_reason": "UNVERIFIED", "error_phrase": None}

    except Exception as ex:
        logger.warning(f"iframe_validator: Unhandled probe error for {url}: {ex}")
        return {"status": "--", "failure_reason": "PROBE_ERROR", "error_phrase": None}
