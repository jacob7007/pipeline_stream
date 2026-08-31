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
    """Extracts the raw channel object array from a FooTyy player page's inline JavaScript.
    Handles standard channels as well as responsive Desktop (DType/DUrl) and Mobile (MType/MUrl) variants."""
    match = re.search(r'channels\s*=\s*(\[.*?\]);', script_text, re.DOTALL)
    if not match:
        return []

    items = re.findall(r'\{([^}]+)\}', match.group(1))
    channel_list = []
    for it in items:
        entry = {}
        # Extract all known fields including Desktop/Mobile responsive variants (DType/DUrl, MType/MUrl)
        for field in ("id", "name", "type", "url", "DType", "DUrl", "MType", "MUrl"):
            m_field = re.search(rf'[\'\"]?{field}[\'\"]?\s*:\s*[\'\"]?([^\'\"\",]+)[\'\"]?', it)
            if m_field:
                entry[field] = m_field.group(1).strip()

        # If explicit type/url are present, use standard entry
        if entry.get("type") or entry.get("url"):
            channel_list.append(entry)
        else:
            d_type = entry.get("DType", "")
            d_url = entry.get("DUrl", "")
            m_type = entry.get("MType", "")
            m_url = entry.get("MUrl", "")

            # If both Desktop and Mobile are specified and point to different stream targets, extract both!
            d_full = _build_full_url(d_type, d_url)
            m_full = _build_full_url(m_type, m_url)

            if d_url and m_url and d_full != m_full:
                base_name = entry.get("name", f"Live {entry.get('id', '')}").strip()
                channel_list.append({
                    "id": entry.get("id"),
                    "name": f"{base_name} (Desktop)",
                    "type": d_type,
                    "url": d_url
                })
                channel_list.append({
                    "id": entry.get("id"),
                    "name": f"{base_name} (Mobile)",
                    "type": m_type,
                    "url": m_url
                })
            elif d_url or m_url:
                entry["type"] = d_type or m_type
                entry["url"] = d_url or m_url
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
    """Returns True if this channel is a Blogma encrypted DRM proxy that needs server-side decryption.
    The 'bx' type is overloaded by FooTyy — used for both Blogma encrypted pages and direct iframe
    embeds. Only treat a URL as Blogma if it actually points to a Blogma/blogspot helper page or
    uses a type code with a known Blogma prefix mapping (non-bx)."""
    # Types that ALWAYS map to Blogma helper pages (they have prefix entries or are blogspot proxies)
    if c_type in _BLOGMA_ENCRYPTED_TYPES and c_type != "bx":
        return True
    # URL-based detection for bx or any other type
    if "blogspot.com/p/" in full_url or "blogma" in full_url:
        return True
    return False


def _try_resolve_embed_to_hls(url: str, proxies: dict = None) -> str | None:
    """Extracts direct native HLS manifest URLs from embed wrappers using universal cipher decoders."""
    if not url or not url.startswith(("http://", "https://")):
        return None

    from iframe_validator import unwrap_redirector_url
    clean_url = unwrap_redirector_url(url)

    try:
        headers = {**DEFAULT_HEADERS, "Referer": "https://footyy.footyy.com/"}
        r = requests.get(clean_url, headers=headers, timeout=8, proxies=proxies)
        if r.status_code != 200:
            # Fallback without Referer
            r = requests.get(clean_url, headers=DEFAULT_HEADERS, timeout=8, proxies=proxies)
        if r.status_code != 200:
            return None
        text = r.text

        # 1. Universal Pattern A: XOR 0x4F + ROT13 dynamic script (?x=atob(...))
        m_script = re.search(r's\.src\s*=\s*String\.fromCharCode\(63,120,61\)\s*\+\s*atob\([\"\']([^\"\']+)[\"\']\)', text)
        if m_script:
            import base64
            param_x = base64.b64decode(m_script.group(1)).decode('utf-8')
            js_url = clean_url.rstrip('/') + '/?x=' + param_x
            r_js = requests.get(js_url, headers={**DEFAULT_HEADERS, "Referer": clean_url}, timeout=6, proxies=proxies)
            if r_js.status_code == 200:
                m_dc = re.search(r'var\s+STREAM_SRC\s*=\s*dc\([\"\']([^\"\']+)[\"\']\)', r_js.text)
                if m_dc:
                    b = base64.b64decode(m_dc.group(1))
                    x = ''.join(chr(byte ^ 0x4F) for byte in b)
                    out = []
                    for c in x:
                        if 'a' <= c <= 'z':
                            out.append(chr((ord(c) - ord('a') + 13) % 26 + ord('a')))
                        elif 'A' <= c <= 'Z':
                            out.append(chr((ord(c) - ord('A') + 13) % 26 + ord('A')))
                        else:
                            out.append(c)
                    resolved = ''.join(out)
                    if resolved.startswith(("http://", "https://")):
                        return resolved

        # 2. Universal Pattern B: 6-layer unshuffle/XOR obfuscated stream engine
        m_dd = re.search(r'const\s+_dd\s*=\s*[\"\']([a-fA-F0-9]+)[\"\']', text)
        m_dk = re.search(r'const\s+_dk\s*=\s*(\d+)', text)
        m_dri = re.search(r'const\s+_dri\s*=\s*(\[[0-9,\s]+\])', text)
        if m_dd and m_dk and m_dri:
            import base64
            import json
            encoded = m_dd.group(1)
            xor_key = int(m_dk.group(1))
            rev_indices = json.loads(m_dri.group(1))
            chars = list(encoded)
            unshuffled = [None] * len(chars)
            for i in range(len(chars)):
                unshuffled[rev_indices[i]] = chars[i]
            xor_encoded = ''.join(unshuffled)
            hex_encoded = ''.join(chr(int(xor_encoded[i:i+2], 16) ^ xor_key) for i in range(0, len(xor_encoded), 2))
            rot13_str = ''.join(chr(int(hex_encoded[i:i+2], 16)) for i in range(0, len(hex_encoded), 2))
            rot13_dec = []
            for c in rot13_str:
                if 'a' <= c <= 'z':
                    rot13_dec.append(chr((ord(c) - ord('a') + 13) % 26 + ord('a')))
                elif 'A' <= c <= 'Z':
                    rot13_dec.append(chr((ord(c) - ord('A') + 13) % 26 + ord('A')))
                else:
                    rot13_dec.append(c)
            b64 = ''.join(rot13_dec)[::-1]
            resolved = base64.b64decode(b64).decode('utf-8')
            if resolved.startswith(("http://", "https://")):
                return resolved

        # 3. Direct base64 atob payloads or m3u8 in HTML
        import base64
        from urllib.parse import unquote
        b64_match = re.search(r'atob\s*\(\s*["\']([A-Za-z0-9+/=]{40,})["\']', text)
        if b64_match:
            try:
                decoded = base64.b64decode(b64_match.group(1)).decode("utf-8", errors="replace")
                text = unquote(decoded)
            except Exception:
                pass

        m_m3u8 = re.search(r'(?:source|src|file)\s*[:=]\s*["\'](https?://[^\s"\']+\.m3u8[^\s"\']*)["\']', text, re.IGNORECASE)
        if m_m3u8:
            return m_m3u8.group(1)

        m_zone = re.search(r'(?:source|src|file)\s*[:=]\s*["\'](https?://[^\s"\']*zonetake[^\s"\']*)["\']', text, re.IGNORECASE)
        if m_zone:
            return m_zone.group(1)

    except Exception:
        pass
    return None


def _format_channel_entry(entry: dict, idx: int, proxies: dict = None) -> dict | None:
    """
    Converts one raw JS channel dict into a standardised player channel dict.
    Returns None if the entry cannot be resolved to a usable stream.
    """
    from iframe_validator import unwrap_redirector_url

    c_type = entry.get("type", "").strip().lower()
    c_url = entry.get("url", "").strip()
    if not c_url:
        return None

    c_id = int(entry.get("id", idx))
    c_name = entry.get("name", f"Live {c_id}")
    full_url = unwrap_redirector_url(_build_full_url(c_type, c_url))

    # Blogma helper pages need server-side AES decryption to get the real stream
    if _is_blogma_helper(c_type, full_url) and full_url.startswith(("http://", "https://")):
        drm_stream = resolve_blogma_stream(full_url, proxies=proxies)
        if drm_stream:
            quality_label = "DRM" if drm_stream.get("type") == "shaka" else "HLS"
            return {"id": c_id, "name": c_name, "quality": quality_label, **drm_stream}
        # If decryption failed on a blogspot helper proxy, do not treat dead proxy as valid iframe
        if "blogspot.com" in full_url:
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
            "sandbox": "allow-scripts allow-same-origin allow-presentation allow-forms",
        }

    if c_type == "ok":
        return {
            "id": c_id, "name": c_name, "quality": "OK.ru", "type": "iframe",
            "url": f"https://games.ok.ru/videoembed/{c_url}?nochat=1&autoplay=1",
            "sandbox": "allow-scripts allow-same-origin allow-presentation allow-forms",
        }

    if c_type == "hs" or full_url.endswith(".m3u8") or ".m3u8?" in full_url:
        return {"id": c_id, "name": c_name, "quality": "HLS", "type": "hls", "url": full_url}

    # Try resolving embed wrappers to native HLS streams (e.g. embed1.top, maslaz, gozo) to play ad-free
    direct_hls = _try_resolve_embed_to_hls(full_url, proxies=proxies)
    if direct_hls:
        return {"id": c_id, "name": c_name, "quality": "HLS", "type": "hls", "url": direct_hls}

    if full_url.startswith(("http://", "https://")):
        return {
            "id": c_id, "name": c_name, "quality": "iFrame", "type": "iframe",
            "url": full_url,
            "sandbox": "allow-scripts allow-same-origin allow-presentation allow-forms",
        }

    return None


def extract_channels(match_url: str, proxies: dict = None) -> list[dict]:
    """
    Fetches a FooTyy match page, extracts the channels JS array, formats every entry,
    and returns all resolved channel dicts. Validation and priority sorting happen in channel_resolver.
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
