import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests
import logger
from utils import DEFAULT_HEADERS

# ---------------------------------------------------------------------------
# Plugin: Yalla-Shoot Family
# Handles structural variants across the Yalla-Shoot ecosystem:
#
# Variant 1 — Classic AY_Match engine (Yalla-Shoot clones, LiveHD7, Kora-Star, Sir-TV)
#   Container : .AY_Match
#   Team 1    : .TM1 .TM_Name  /  Logo: .TM1 .TM_Logo img
#   Team 2    : .TM2 .TM_Name  /  Logo: .TM2 .TM_Logo img
#   Time      : .MT_Time or data-start
#
# Variant 2 — Modern match-container engine (Arabic sports portals, Blogger themes)
#   Container : .match-container
#   Team 1    : .right-team .team-name  /  Logo: .right-team .team-logo img
#   Team 2    : .left-team  .team-name  /  Logo: .left-team  .team-logo img
#   Time      : .match-time
# ---------------------------------------------------------------------------

# Status class names shared across both variants
_LIVE_CLASSES     = {"live", "live2", "started", "gools"}
_FINISHED_CLASSES = {"end", "finished"}
_UPCOMING_CLASSES = {"comming-soon", "not-started", "not-start", "comming"}

# Domains that are never streaming iframes — always filter them out for Yalla-Shoot family
_NON_STREAMING_DOMAINS = ["blogger.com", "google", "facebook", "twitter", "youtube", "cloudflare"]


# Uses compound structural fingerprints to recognize classic AY_Match or modern match-container markup
def can_handle(soup: BeautifulSoup) -> bool:
    variant_1 = bool(soup.select(".AY_Match, .AY_Inner, .TM_Name"))
    variant_2 = bool(soup.select(".match-container .right-team, .match-container .team-name"))
    return variant_1 or variant_2


def _determine_status(classes: set) -> str | None:
    if classes & _LIVE_CLASSES:
        return "live"
    if classes & _FINISHED_CLASSES:
        return "finished"
    if classes & _UPCOMING_CLASSES:
        return "not-started"
    return None


def _extract_image_src(img_elem) -> str:
    """Extracts first valid image source attribute from img tag."""
    if not img_elem:
        return ""
    for attr in ("data-src", "data-lazy-src", "data-original", "data-webp", "src"):
        src = img_elem.get(attr)
        if src and not src.startswith("data:image"):
            return src.strip()
    return ""


def _extract_team_info(match, is_variant_2: bool) -> tuple:
    if is_variant_2:
        team1_elem = match.select_one(".right-team .team-name, .right-team .name, .right-team h3")
        team2_elem = match.select_one(".left-team .team-name, .left-team .name, .left-team h3")
        t1_img_elem = match.select_one(".right-team .team-logo img, .right-team img")
        t2_img_elem = match.select_one(".left-team .team-logo img, .left-team img")
        time_elem = match.select_one(".match-time, .time")
    else:
        team1_elem = match.select_one(".TM1 .TM_Name, .TM1 .team-name, .TM1 .name, .TeamA_Name, .HTeam_Name")
        team2_elem = match.select_one(".TM2 .TM_Name, .TM2 .team-name, .TM2 .name, .TeamB_Name, .ATeam_Name")
        t1_img_elem = match.select_one(".TM1 .TM_Logo img, .TM1 img")
        t2_img_elem = match.select_one(".TM2 .TM_Logo img, .TM2 img")
        time_elem = match.select_one(".MT_Time, .match-time, .time")

    t1_name = team1_elem.get_text(strip=True) if team1_elem else "Unknown Team 1"
    t2_name = team2_elem.get_text(strip=True) if team2_elem else "Unknown Team 2"
    t1_orig_img = _extract_image_src(t1_img_elem)
    t2_orig_img = _extract_image_src(t2_img_elem)

    time_str = time_elem.get_text(strip=True) if time_elem else ""
    if not time_str and match.get("data-start"):
        m = re.search(r"(\d{1,2}:\d{2})", match.get("data-start", ""))
        if m:
            time_str = m.group(1)

    if not time_str:
        time_str = "12:00 AM"

    return t1_name, t2_name, t1_orig_img, t2_orig_img, time_str


def _extract_match_date(match, link_elem, default_date: str) -> str:
    """Extracts date (YYYY-MM-DD) from data-start, href, title, or default_date."""
    data_start = match.get("data-start", "")
    if data_start:
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", data_start)
        if date_match:
            return date_match.group(0)

    if link_elem:
        href_str = link_elem.get("href", "")
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", href_str)
        if date_match:
            return date_match.group(0)

        title_str = link_elem.get("title", "")
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", title_str)
        if date_match:
            return date_match.group(0)

    return default_date


def parse_matches(soup: BeautifulSoup, source_url: str, default_date: str, source_tz: str | int = None) -> list:
    match_elements = soup.select(".AY_Match, .match-container")
    real_matches = [m for m in match_elements if not m.select_one(".no-data__msg")]

    results = []
    for match in real_matches:
        classes = set(match.get("class", []))
        status_class = _determine_status(classes)
        if not status_class:
            continue

        link_elem = match.find("a", href=True)
        if not link_elem:
            continue

        match_url = urljoin(source_url, link_elem["href"])
        is_variant_2 = "match-container" in classes
        t1_name, t2_name, t1_orig_img, t2_orig_img, time_str = _extract_team_info(match, is_variant_2)
        date_str = _extract_match_date(match, link_elem, default_date)

        # If data-start has explicit tz offset, use it
        match_source_tz = source_tz
        data_start = match.get("data-start", "")
        if data_start:
            tz_match = re.search(r'([+-]\d{2}:\d{2})$', data_start.strip())
            if tz_match:
                match_source_tz = tz_match.group(1)

        results.append({
            "team1_name": t1_name,
            "team2_name": t2_name,
            "team1_orig_img": t1_orig_img,
            "team2_orig_img": t2_orig_img,
            "date_str": date_str,
            "time_str": time_str,
            "match_url": match_url,
            "status_class": status_class,
            "source_tz": match_source_tz,
        })

    return results


def extract_iframe(match_url: str, proxies: dict = None, context: dict = None) -> str:
    try:
        resp = requests.get(match_url, headers=DEFAULT_HEADERS, timeout=12, proxies=proxies)
        if resp.status_code != 200:
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src") or iframe.get("data-src")
            if not src:
                continue
            if any(domain in src for domain in _NON_STREAMING_DOMAINS):
                continue
            return src
    except Exception as e:
        logger.warning(f"Plugin (from_yallashoot): Failed to extract iframe from {match_url}: {e}")
    return ""

