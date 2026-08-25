import re
import json
import base64
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
import logger
from utils import DEFAULT_HEADERS

# ---------------------------------------------------------------------------
# Plugin: FooTyy & TodayM Widget Family
# Handles FooTyy portal pages (footyy.footyy.com) and the underlying TodayM
# widget architecture (todaym0.blogspot.com) with Blogma stream channels.
# ---------------------------------------------------------------------------

_BLOCKED_DOMAINS = [
    "blogger.com", "blogspot.com", "google", "facebook", "twitter", "cloudflare"
]


def can_handle(soup: BeautifulSoup) -> bool:
    """Identifies FooTyy or TodayM matches widget markup."""
    has_widget_script = bool(soup.select_one("script[data-matches-widget]"))
    has_widget_iframe = bool(soup.select_one("iframe[src*='todaym'], iframe[src*='egy4']"))
    has_footyy_shell = bool(
        soup.title and "footyy" in soup.title.get_text().lower() and soup.select_one(".post-body iframe[src]")
    )
    return has_widget_script or has_widget_iframe or has_footyy_shell


def _fetch_embedded_widget_soup(soup: BeautifulSoup, source_url: str, proxies: dict = None) -> BeautifulSoup | None:
    """Fetches widget HTML if the page embeds the widget inside an iframe."""
    iframe_elem = soup.select_one("iframe[src*='todaym'], iframe[src*='egy4'], .post-body iframe[src]")
    if not iframe_elem:
        return None

    src = iframe_elem.get("src") or iframe_elem.get("data-src", "")
    if not src:
        return None

    # Strip redirect wrappers like https://href.li/?https://...
    if "href.li/?" in src:
        src = src.split("href.li/?", 1)[1]

    target_url = urljoin(source_url, src)
    try:
        resp = requests.get(target_url, headers=DEFAULT_HEADERS, timeout=12, proxies=proxies)
        if resp.status_code == 200:
            return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.warning(f"Plugin (from_footyy): Failed to fetch embedded widget frame '{target_url}': {e}")
    return None


def _calculate_status(start_iso: str, duration_min: int, ended: bool) -> str:
    """Calculates live, not-started, or finished status based on UTC start time and duration."""
    if ended:
        return "finished"
    if not start_iso:
        return "not-started"

    try:
        clean_iso = start_iso.replace("Z", "+00:00")
        start_dt = datetime.fromisoformat(clean_iso)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)

        now_utc = datetime.now(timezone.utc)
        end_dt = start_dt + timedelta(minutes=duration_min)

        if now_utc < start_dt:
            return "not-started"
        if start_dt <= now_utc < end_dt:
            return "live"
        return "finished"
    except Exception as e:
        logger.warning(f"Plugin (from_footyy): Failed to evaluate match status for time '{start_iso}': {e}")
        return "not-started"


def _parse_iso_match_time(iso_str: str, default_date: str) -> tuple[str, str]:
    """Extracts date (YYYY-MM-DD) and 12-hour time (e.g. '08:00 PM') from an ISO 8601 string."""
    if not iso_str:
        return default_date, "12:00 AM"
    try:
        clean_iso = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%I:%M %p")
    except Exception:
        return default_date, "12:00 AM"


def parse_matches(soup: BeautifulSoup, source_url: str, default_date: str, source_tz: str | int = None) -> list:
    widget_soup = soup
    widget_script = soup.select_one("script[data-matches-widget]")
    if not widget_script:
        fetched_soup = _fetch_embedded_widget_soup(soup, source_url)
        if fetched_soup:
            widget_soup = fetched_soup
            widget_script = widget_soup.select_one("script[data-matches-widget]")

    if not widget_script or not widget_script.string:
        logger.warning(f"Plugin (from_footyy): No matches widget JSON found at {source_url}")
        return []

    try:
        raw_matches = json.loads(widget_script.string.strip())
    except Exception as e:
        logger.error(f"Plugin (from_footyy): Failed to parse matches widget JSON: {e}")
        return []

    results = []
    # Source timestamps in data-matches-widget are ISO 8601 UTC strings
    match_source_tz = source_tz if source_tz is not None else "UTC"

    for m in raw_matches:
        if not isinstance(m, dict):
            continue

        team1 = m.get("team1") or {}
        team2 = m.get("team2") or {}
        t1_name = (team1.get("nameAr") or team1.get("nameEn") or "Unknown Team 1").strip()
        t2_name = (team2.get("nameAr") or team2.get("nameEn") or "Unknown Team 2").strip()
        t1_img = team1.get("img", "")
        t2_img = team2.get("img", "")

        time_raw = m.get("time", "")
        duration = int(m.get("duration") or 130)
        ended = bool(m.get("ended", False))

        date_str, time_str = _parse_iso_match_time(time_raw, default_date)
        status_class = _calculate_status(time_raw, duration, ended)

        match_url = (m.get("link") or "").strip()
        if match_url and not match_url.startswith(("http://", "https://")):
            match_url = urljoin(source_url, match_url)

        results.append({
            "team1_name": t1_name,
            "team2_name": t2_name,
            "team1_orig_img": t1_img,
            "team2_orig_img": t2_img,
            "date_str": date_str,
            "time_str": time_str,
            "match_url": match_url,
            "status_class": status_class,
            "source_tz": match_source_tz,
        })

    return results


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16) -> tuple[bytes, bytes]:
    """OpenSSL EVP_BytesToKey key derivation matching CryptoJS default AES encryption."""
    d = b''
    d_i = b''
    while len(d) < key_len + iv_len:
        d_i = hashlib.md5(d_i + password + salt).digest()
        d += d_i
    return d[:key_len], d[key_len:key_len + iv_len]


def _cryptojs_aes_decrypt(ciphertext_b64: str, passphrase: str) -> str:
    """Decrypts OpenSSL-compatible AES-256-CBC ciphertext generated by CryptoJS."""
    raw = base64.b64decode(ciphertext_b64)
    if not raw.startswith(b'Salted__'):
        raise ValueError("Invalid CryptoJS ciphertext header (missing Salted__).")
    salt = raw[8:16]
    encrypted = raw[16:]
    key, iv = _evp_bytes_to_key(passphrase.encode("utf-8"), salt, 32, 16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded_data = decryptor.update(encrypted) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    data = unpadder.update(padded_data) + unpadder.finalize()
    return data.decode("utf-8")


def _resolve_blogma_encrypted_stream(url: str, proxies: dict = None) -> dict | None:
    """
    Decrypts Blogma helper pages (sewzzy, swxzyy, nazity, etc.) on the backend to bypass
    browser-side anti-embed checks and extract native Shaka DASH manifest & ClearKeys.
    """
    headers = {
        **DEFAULT_HEADERS,
        "Referer": "https://m.blogma.sbs/"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10, proxies=proxies)
        if resp.status_code != 200 or "__payload" not in resp.text:
            return None

        m_payload = re.search(r'var\s+__payload\s*=\s*\"([^\"]+)\"', resp.text)
        m_ka = re.search(r'var\s+__ka\s*=\s*(\[[^\]]+\])', resp.text)
        m_kb = re.search(r'var\s+__kb\s*=\s*(\[[^\]]+\])', resp.text)
        m_ma = re.search(r'var\s+__ma\s*=\s*(\d+)', resp.text)
        m_mb = re.search(r'var\s+__mb\s*=\s*(\d+)', resp.text)

        if not (m_payload and m_ka and m_kb and m_ma and m_mb):
            return None

        payload = m_payload.group(1)
        ka = json.loads(m_ka.group(1))
        kb = json.loads(m_kb.group(1))
        ma = int(m_ma.group(1))
        mb = int(m_mb.group(1))

        # Reconstruct obfuscated passphrases via bitwise XOR with mask integers
        kA = ''.join([chr(int(x, 16) ^ ma) for x in ka])
        kB = ''.join([chr(int(x, 16) ^ mb) for x in kb])

        decB = _cryptojs_aes_decrypt(payload, kB)
        decA = _cryptojs_aes_decrypt(decB, kA)

        # Handle multi-channel definitions (e.g. nazity ?id=spt1)
        parsed_url = urlparse(url)
        qs = parse_qs(parsed_url.query)
        target_id = qs.get("id", [None])[0]

        canais_match = re.search(r'(?:canais|channels|streams)\s*=\s*(\{[\s\S]*?\n\s*\};)', decA)
        if canais_match and target_id:
            raw_canais = canais_match.group(1)
            for cm in re.finditer(r'([a-zA-Z0-9_-]+)\s*:\s*\{\s*url\s*:\s*[\'\"]([^\'\"]+)[\'\"]\s*,\s*(?:clearkey|clearKeys|keys)\s*:\s*(\{[\s\S]*?\})\s*\}', raw_canais):
                cid = cm.group(1)
                if cid == target_id:
                    curl = cm.group(2)
                    if curl.startswith("//"):
                        curl = "https:" + curl
                    ck_raw = cm.group(3)
                    keys = {}
                    for km in re.finditer(r'[\'\"]([a-fA-F0-9]+)[\'\"]\s*:\s*[\'\"]([a-fA-F0-9]+)[\'\"]', ck_raw):
                        keys[km.group(1)] = km.group(2)
                    return {"type": "shaka", "manifest": curl, "keys": keys}

        # Handle single manifest definitions
        m_manifest = re.search(r'(?:manifestUri|manifest|file|source|url)\s*[:=]\s*[\'\"]([^\'\"]+)[\'\"]', decA)
        m_keys = re.search(r'(?:clearKeys|clearkey|keys)\s*[:=]\s*(\{[\s\S]*?\n\s*\})', decA)

        if m_manifest:
            manifest_url = m_manifest.group(1)
            if manifest_url.startswith("//"):
                manifest_url = "https:" + manifest_url

            if manifest_url.endswith(".m3u8") or ".m3u8?" in manifest_url:
                return {"type": "hls", "url": manifest_url}

            keys = {}
            if m_keys:
                ck_raw = m_keys.group(1)
                for km in re.finditer(r'[\'\"]([a-fA-F0-9]+)[\'\"]\s*:\s*[\'\"]([a-fA-F0-9]+)[\'\"]', ck_raw):
                    keys[km.group(1)] = km.group(2)

            return {"type": "shaka", "manifest": manifest_url, "keys": keys}

    except Exception as e:
        logger.warning(f"Plugin (from_footyy): Failed to decrypt Blogma DRM stream from '{url}': {e}")

    return None


_BLOGMA_URL_PREFIX = {
    "ok": "https://games.ok.ru/videoembed/",
    "ty": "https://player.twitch.tv/?channel=",
    "tu": "https://www.youtube.com/embed/",
    "bn": "/p/",
    "b": "https://merithotdog.net/e/",
    "bb": "https://merithotdog.net/e/",
    "n": "https://nazity.blogspot.com/p/d.html?id=",
    "m": "https://mazarok.blogspot.com/p/d.html?id=",
    "nn": "https://nazqma.blogspot.com/p/d.html?id=",
    "mm": "https://mmzxrt.blogspot.com/p/d.html?id=",
    "i": "https://iazyew.blogspot.com/p/i.html?src=",
    "ii": "https://iakazz.blogspot.com/p/i.html?src=",
    "o": "https://kkzawe.blogspot.com/p/ddd.html?id=",
    "oo": "https://mnwzty.blogspot.com/p/ddd.html?id=",
    "sc": "https://mkaasii.blogspot.com/p/",
    "sw": "https://swxzyy.blogspot.com/p/",
    "ov": "https://azmizaz.blogspot.com/p/",
    "sx": "https://sxamiya.blogspot.com/p/",
    "se": "https://sewzzy.blogspot.com/p/"
}


def _resolve_channel_url(ch: dict) -> str:
    """Translates a channel dictionary into a direct embed iframe URL."""
    ctype = ch.get("type", "").strip().lower()
    curl = ch.get("url", "").strip()
    if not curl:
        return ""

    if ctype == "ok":
        return f"https://games.ok.ru/videoembed/{curl}?nochat=1&autoplay=0"
    if ctype == "tu":
        return f"https://www.youtube.com/embed/{curl}?rel=0"
    if ctype == "ty":
        return f"https://player.twitch.tv/?channel={curl}&parent=footyy.com&muted=false"
    if ctype in ("ch", "chm"):
        return f"https://cdn17.sporthub10.com/ch/{curl}.php"
    if ctype in ("sp", "spm"):
        return f"https://w1.sportsonlinee.click/channels/hd/{curl}.php"
    if curl.startswith(("http://", "https://")):
        return curl
    return ""


def _extract_channels_from_script(script_text: str) -> list[dict]:
    """Extracts parsed channel objects from player JavaScript initialization."""
    match = re.search(r'channels\s*=\s*(\[.*?\]);', script_text, re.DOTALL)
    if not match:
        return []

    items = re.findall(r'\{([^}]+)\}', match.group(1))
    channel_list = []
    for it in items:
        entry = {}
        for field in ("id", "name", "type", "url"):
            m_field = re.search(rf'[\'"]?{field}[\'"]?\s*:\s*[\'"]?([^\'\",]+)[\'"]?', it)
            if m_field:
                entry[field] = m_field.group(1).strip()
        if entry:
            channel_list.append(entry)
    return channel_list


def _find_best_channel_iframe(channels: list, proxies: dict = None) -> str:
    """Prioritizes embeddable HTML player pages over raw stream files and verifies accessibility."""
    candidates = []
    for ch in channels:
        resolved = _resolve_channel_url(ch)
        if not resolved:
            continue
        if any(bad in resolved for bad in _BLOCKED_DOMAINS):
            continue

        is_html_player = not resolved.endswith(".m3u8") and ".m3u8?" not in resolved
        candidates.append((is_html_player, resolved))

    candidates.sort(key=lambda x: (not x[0]))

    for _, candidate_url in candidates:
        try:
            resp = requests.get(candidate_url, headers=DEFAULT_HEADERS, timeout=5, proxies=proxies)
            if resp.status_code == 200:
                return candidate_url
        except Exception:
            return candidate_url

    return ""


def _format_channel_entry(entry: dict, idx: int, proxies: dict = None) -> dict | None:
    """Formats a single raw channel entry into a standardized player channel dict."""
    c_type = entry.get("type", "").strip().lower()
    c_url = entry.get("url", "").strip()
    if not c_url:
        return None
    c_id = int(entry.get("id", idx))
    c_name = entry.get("name", f"Live {c_id}")

    # Expand shorthand Blogma prefix if present
    full_url = c_url
    if c_type in _BLOGMA_URL_PREFIX and not c_url.startswith(("http://", "https://")):
        full_url = _BLOGMA_URL_PREFIX[c_type] + c_url
        if c_type in ("sc", "sw", "ov", "sx", "se") and not full_url.endswith(".html"):
            full_url += ".html"

    # Resolve Blogma encrypted DRM pages (Shaka ClearKey DASH)
    is_blogma_helper = (
        any(h in full_url for h in ["blogspot.com/p/", "blogma"])
        or c_type in ("bx", "b", "bb", "n", "m", "nn", "mm", "o", "oo", "sc", "sw", "ov", "sx", "se")
    )
    if is_blogma_helper and full_url.startswith(("http://", "https://")):
        drm_stream = _resolve_blogma_encrypted_stream(full_url, proxies=proxies)
        if drm_stream:
            quality_label = "FHD DRM" if drm_stream.get("type") == "shaka" else "HLS"
            return {
                "id": c_id,
                "name": c_name,
                "quality": quality_label,
                **drm_stream
            }

    if c_type == "ok":
        return {
            "id": c_id, "name": c_name, "quality": "OK.ru", "type": "iframe",
            "url": f"https://games.ok.ru/videoembed/{c_url}?nochat=1&autoplay=0",
            "sandbox": "allow-scripts allow-same-origin allow-presentation allow-forms"
        }
    if c_type == "hs" or full_url.endswith(".m3u8") or ".m3u8?" in full_url:
        return {
            "id": c_id, "name": c_name, "quality": "HLS", "type": "hls", "url": full_url
        }
    if c_type in ("i", "ii") and full_url.startswith(("http://", "https://")):
        return {
            "id": c_id, "name": c_name, "quality": "1080p" if "475" in full_url else "HD", "type": "iframe",
            "url": full_url, "sandbox": "allow-scripts allow-same-origin allow-presentation allow-forms"
        }
    if full_url.startswith(("http://", "https://")):
        return {
            "id": c_id, "name": c_name, "quality": "HD", "type": "iframe",
            "url": full_url, "sandbox": "allow-scripts allow-same-origin allow-presentation allow-forms"
        }
    return None


def extract_channels(match_url: str, proxies: dict = None) -> list[dict]:
    """Extracts all multi-stream channels from match page."""
    if not match_url or not match_url.startswith(("http://", "https://")) or "#match-" in match_url:
        return []
    try:
        headers = {
            **DEFAULT_HEADERS,
            "Referer": "https://footyy.footyy.com/"
        }
        resp = requests.get(match_url, headers=headers, timeout=12, proxies=proxies)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        for s in soup.find_all("script"):
            if s.string and ("var channels" in s.string or "channels =" in s.string):
                raw_channels = _extract_channels_from_script(s.string)
                formatted = []
                for idx, ch in enumerate(raw_channels, start=1):
                    entry = _format_channel_entry(ch, idx, proxies=proxies)
                    if entry:
                        formatted.append(entry)
                if formatted:
                    return formatted
    except Exception as e:
        logger.warning(f"Plugin (from_footyy): Failed to extract channels from {match_url}: {e}")
    return []


def extract_iframe(match_url: str, proxies: dict = None, context: dict = None) -> str:
    if not match_url or not match_url.startswith(("http://", "https://")) or "#match-" in match_url:
        return ""

    try:
        headers = {
            **DEFAULT_HEADERS,
            "Referer": "https://footyy.footyy.com/"
        }
        resp = requests.get(match_url, headers=headers, timeout=12, proxies=proxies)
        if resp.status_code != 200:
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")
        for s in soup.find_all("script"):
            if s.string and ("var channels" in s.string or "channels =" in s.string):
                channels = _extract_channels_from_script(s.string)
                if channels:
                    resolved_iframe = _find_best_channel_iframe(channels, proxies=proxies)
                    if resolved_iframe:
                        return resolved_iframe

        # Fallback: inspect raw <iframe> tags if present
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src") or iframe.get("data-src")
            if not src:
                continue
            if any(domain in src for domain in _BLOCKED_DOMAINS):
                continue
            return src
    except Exception as e:
        logger.warning(f"Plugin (from_footyy): Failed to extract iframe from {match_url}: {e}")
    return ""

