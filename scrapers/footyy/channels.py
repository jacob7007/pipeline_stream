import re
from bs4 import BeautifulSoup
import requests
import logger
from utils import DEFAULT_HEADERS
from .decryptor import resolve_blogma_stream


# Shorthand type codes used by FooTyy's channel JS objects, mapped to full Blogma/CDN URLs
_BLOGMA_URL_PREFIX = {
    "ok":  "https://games.ok.ru/videoembed/",
    "ty":  "https://player.twitch.tv/?channel=",
    "tu":  "https://www.youtube.com/embed/",
    "bn":  "/p/",
    "b":   "https://merithotdog.net/e/",
    "bb":  "https://merithotdog.net/e/",
    "n":   "https://nazity.blogspot.com/p/d.html?id=",
    "m":   "https://mazarok.blogspot.com/p/d.html?id=",
    "nn":  "https://nazqma.blogspot.com/p/d.html?id=",
    "mm":  "https://mmzxrt.blogspot.com/p/d.html?id=",
    "i":   "https://iazyew.blogspot.com/p/i.html?src=",
    "ii":  "https://iakazz.blogspot.com/p/i.html?src=",
    "o":   "https://kkzawe.blogspot.com/p/ddd.html?id=",
    "oo":  "https://mnwzty.blogspot.com/p/ddd.html?id=",
    "sc":  "https://mkaasii.blogspot.com/p/",
    "sw":  "https://swxzyy.blogspot.com/p/",
    "ov":  "https://azmizaz.blogspot.com/p/",
    "sx":  "https://sxamiya.blogspot.com/p/",
    "se":  "https://sewzzy.blogspot.com/p/",
}

# Blogma types that serve as encrypted DRM proxy pages rather than direct iframes
_BLOGMA_ENCRYPTED_TYPES = {"bx", "b", "bb", "n", "m", "nn", "mm", "o", "oo", "sc", "sw", "ov", "sx", "se"}

# Blogma types that need a .html suffix appended when building the full URL
_BLOGMA_HTML_SUFFIX_TYPES = {"sc", "sw", "ov", "sx", "se"}


def _extract_channels_from_script(script_text: str) -> list[dict]:
    """Extracts the raw channel object array from a FooTyy player page's inline JavaScript."""
    match = re.search(r'channels\s*=\s*(\[.*?\]);', script_text, re.DOTALL)
    if not match:
        return []

    items = re.findall(r'\{([^}]+)\}', match.group(1))
    channel_list = []
    for it in items:
        entry = {}
        for field in ("id", "name", "type", "url"):
            m_field = re.search(rf'[\'"]?{field}[\'"]?\s*:\s*[\'"]?([^\'"",]+)[\'"]?', it)
            if m_field:
                entry[field] = m_field.group(1).strip()
        if entry:
            channel_list.append(entry)
    return channel_list


def _build_full_url(c_type: str, c_url: str) -> str:
    """Expands a shorthand Blogma channel URL using the prefix map and adds .html when required."""
    if c_type in _BLOGMA_URL_PREFIX and not c_url.startswith(("http://", "https://")):
        full = _BLOGMA_URL_PREFIX[c_type] + c_url
        if c_type in _BLOGMA_HTML_SUFFIX_TYPES and not full.endswith(".html"):
            full += ".html"
        return full
    return c_url


def _is_blogma_helper(c_type: str, full_url: str) -> bool:
    """Returns True if this channel is a Blogma encrypted DRM proxy that needs server-side decryption."""
    return (
        c_type in _BLOGMA_ENCRYPTED_TYPES
        or "blogspot.com/p/" in full_url
        or "blogma" in full_url
    )


def _format_channel_entry(entry: dict, idx: int, proxies: dict = None) -> dict | None:
    """
    Converts one raw JS channel dict into a standardised player channel dict.
    Returns None if the entry cannot be resolved to a usable stream.
    """
    c_type = entry.get("type", "").strip().lower()
    c_url = entry.get("url", "").strip()
    if not c_url:
        return None

    c_id = int(entry.get("id", idx))
    c_name = entry.get("name", f"Live {c_id}")
    full_url = _build_full_url(c_type, c_url)

    # Blogma helper pages need server-side AES decryption to get the real stream
    if _is_blogma_helper(c_type, full_url) and full_url.startswith(("http://", "https://")):
        drm_stream = resolve_blogma_stream(full_url, proxies=proxies)
        if drm_stream:
            quality_label = "FHD DRM" if drm_stream.get("type") == "shaka" else "HLS"
            return {"id": c_id, "name": c_name, "quality": quality_label, **drm_stream}
        return None

    if c_type in ("yt", "youtube") or "youtube.com" in full_url or "youtu.be" in full_url:
        yt_id = c_url
        if "watch?v=" in yt_id:
            yt_id = yt_id.split("watch?v=")[1].split("&")[0]
        elif "youtu.be/" in yt_id:
            yt_id = yt_id.split("youtu.be/")[1].split("?")[0]
        elif "embed/" in yt_id:
            yt_id = yt_id.split("embed/")[1].split("?")[0]
        return {
            "id": c_id, "name": c_name, "quality": "YouTube", "type": "iframe",
            "url": f"https://www.youtube.com/embed/{yt_id}?autoplay=1",
            "sandbox": "allow-scripts allow-same-origin allow-presentation",
        }

    if c_type == "ok":
        return {
            "id": c_id, "name": c_name, "quality": "OK.ru", "type": "iframe",
            "url": f"https://games.ok.ru/videoembed/{c_url}?nochat=1&autoplay=0",
            "sandbox": "allow-scripts allow-same-origin allow-presentation",
        }

    if c_type == "hs" or full_url.endswith(".m3u8") or ".m3u8?" in full_url:
        return {"id": c_id, "name": c_name, "quality": "HLS", "type": "hls", "url": full_url}

    if "yasirtv.com" in full_url or "tvsir" in full_url or "sir-tv" in full_url:
        return {
            "id": c_id, "name": c_name, "quality": "1080p", "type": "iframe",
            "url": full_url,
            "sandbox": "allow-scripts allow-same-origin allow-presentation",
        }

    if full_url.startswith(("http://", "https://")):
        return {"id": c_id, "name": c_name, "quality": "HD", "type": "iframe", "url": full_url}

    return None


def extract_channels(match_url: str, proxies: dict = None) -> list[dict]:
    """
    Fetches a FooTyy match page, extracts the channels JS array, formats every entry,
    and returns all resolved channel dicts. Validation and priority sorting happen in iframe_resolver.
    """
    if not match_url or not match_url.startswith(("http://", "https://")) or "#match-" in match_url:
        return []

    headers = {**DEFAULT_HEADERS, "Referer": "https://footyy.footyy.com/"}
    try:
        resp = requests.get(match_url, headers=headers, timeout=12, proxies=proxies)
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        for script in soup.find_all("script"):
            if script.string and ("var channels" in script.string or "channels =" in script.string):
                raw_channels = _extract_channels_from_script(script.string)
                formatted = [
                    _format_channel_entry(ch, idx, proxies=proxies)
                    for idx, ch in enumerate(raw_channels, start=1)
                ]
                result = [ch for ch in formatted if ch is not None]
                if result:
                    return result

    except Exception as e:
        logger.warning(f"Plugin (footyy/channels): Failed to extract channels from {match_url}: {e}")

    return []
