import re
from bs4 import BeautifulSoup
import requests
import logger
from utils import DEFAULT_HEADERS

# Domains that are never streaming iframes — always filter them out
_NON_STREAMING_DOMAINS = ["blogger.com", "google", "facebook", "twitter", "youtube", "cloudflare"]


def extract_iframe(match_url: str, proxies: dict = None) -> str:
    """Extracts Sir-TV streaming iframe endpoint, resolving DMCA redirects and token handshakes."""
    try:
        resp = requests.get(match_url, headers=DEFAULT_HEADERS, timeout=12, proxies=proxies)
        if resp.status_code != 200:
            return ""

        text = resp.text

        # 1. Check for modern DMCA redirect scripts (e.g. hes-goals.mov, tvsir, sirtv, yasirtv)
        if "location.replace" in text and ("PLAYER_HOST" in text or "URLSearchParams" in text):
            m_match = re.search(r'[?&](?:m|match)=(\d+)', match_url)
            match_id = m_match.group(1) if m_match else ""
            if not match_id:
                m_path = re.search(r'/(\d{4,9})\b', match_url)
                if m_path:
                    match_id = m_path.group(1)

            if match_id:
                m_host = re.search(r'PLAYER_HOST\s*=\s*[\'\"]([^\'\"]+)[\'\"]', text)
                host = m_host.group(1) if m_host else "yassirtv.com"
                m_pid = re.search(r'[?&]p=(\d+)', match_url)
                pid = m_pid.group(1) if m_pid else "87350"

                target_url = f"https://{host}/hard/2908c7d4425d{pid}.html?match={match_id}"
                r_target = requests.get(target_url, headers={**DEFAULT_HEADERS, "Referer": match_url}, timeout=10, proxies=proxies)
                if r_target.status_code == 200:
                    # Look for __playerSrc or playerv5.php endpoint
                    m_key = re.search(r'&key=([a-fA-F0-9]+)', r_target.text)
                    key = m_key.group(1) if m_key else "9f39972b67d6ce22189507d008acwc26"
                    m_base = re.search(r'[\'\"](https://[a-zA-Z0-9.-]+\.yasirtv\.com/playerv5\.php\?match=)[\'\"]', r_target.text)
                    if m_base:
                        return f"{m_base.group(1)}{match_id}&key={key}"
                    m_full = re.search(r'[\'\"](https?://[^\'\"]+/playerv5\.php\?match=\d+&key=[a-fA-F0-9]+)[\'\"]', r_target.text)
                    if m_full:
                        return m_full.group(1)

        # 2. Check for explicit __playerSrc or player.src in page
        m_psrc = re.search(r'__playerSrc\s*=\s*[\'\"]([^\'\"]+)[\'\"]', text)
        if m_psrc and m_psrc.group(1).startswith(("http://", "https://")):
            return m_psrc.group(1)

        # 3. Static iframe inspection
        soup = BeautifulSoup(text, "html.parser")
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src") or iframe.get("data-src")
            if not src:
                continue
            if any(domain in src for domain in _NON_STREAMING_DOMAINS):
                continue
            return src
    except Exception as e:
        logger.warning(f"Plugin (yallashoot/sir_tv): Failed to extract iframe from {match_url}: {e}")
    return ""


def extract_channels(match_url: str, proxies: dict = None) -> list[dict]:
    """Extracts stream channel list specifically for Sir-TV match URLs."""
    iframe_url = extract_iframe(match_url, proxies=proxies)
    if not iframe_url:
        return []
    return [{
        "id": 1,
        "name": "Live 1",
        "quality": "iFrame",
        "type": "iframe",
        "url": iframe_url,
        "sandbox": "allow-scripts allow-same-origin allow-presentation allow-forms",
    }]
