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
_LIVE_CLASSES     = {"live", "live2", "started", "gools", "playing", "first-half", "second-half"}
_FINISHED_CLASSES = {"end", "finished", "ended", "ft", "match-ended"}
_UPCOMING_CLASSES = {"comming-soon", "commingsoon", "comingsoon", "coming-soon", "not-started", "not-start", "comming", "coming", "soon", "ns"}

# Domains that are never streaming iframes — always filter them out for Yalla-Shoot family
_NON_STREAMING_DOMAINS = ["blogger.com", "google", "facebook", "twitter", "youtube", "cloudflare"]


# Uses compound structural fingerprints to recognize classic AY_Match or modern match-container markup
def can_handle(soup: BeautifulSoup) -> bool:
    variant_1 = bool(soup.select(".AY_Match, .AY_Inner, .TM_Name"))
    variant_2 = bool(soup.select(".match-container .right-team, .match-container .team-name"))
    return variant_1 or variant_2


def _normalize_arabic_status(text: str) -> str:
    if not text:
        return ""
    # Normalize alefs (إ, أ, آ, ٱ -> ا), teh marbuta (ة -> ه), alif maksura (ى -> ي)
    text = re.sub(r'[إأآٱ]', 'ا', text)
    text = re.sub(r'ة', 'ه', text)
    text = re.sub(r'ى', 'ي', text)
    # Strip diacritics
    text = re.sub(r'[\u064B-\u065F\u0670]', '', text)
    return text.lower().strip()


def _determine_status(classes: set, match=None) -> str | None:
    if classes & _LIVE_CLASSES:
        return "live"
    if classes & _FINISHED_CLASSES:
        return "finished"
    if classes & _UPCOMING_CLASSES:
        return "not-started"

    # Fallback to inspecting status indicators or text within the match container
    if match:
        status_elem = match.select_one(".AlbaDate, .match-status, .status, .live-status, .match-state, .text-match, .EventResult")
        if status_elem:
            st_classes = set(status_elem.get("class", []))
            if st_classes & _LIVE_CLASSES:
                return "live"
            if st_classes & _FINISHED_CLASSES:
                return "finished"
            if st_classes & _UPCOMING_CLASSES:
                return "not-started"

            st_text = _normalize_arabic_status(status_elem.get_text(strip=True))
            if any(k in st_text for k in ("مباشر", "جاري", "جاريه", "شوط", "الان", "live")):
                return "live"
            if any(k in st_text for k in ("انتهت", "منتهيه", "نهايه", "نهائي", "ft", "ended", "finished")):
                return "finished"
            if any(k in st_text for k in ("لم تبدا", "قريب", "قادم", "بعد قليل", "soon", "ns")):
                return "not-started"

    return None


def _extract_image_src(img_elem) -> str:
    """Extracts first valid image source attribute from img tag."""
    if not img_elem:
        return ""
    for attr in ("data-loader-src", "data-src", "data-lazy-src", "data-original", "data-webp", "data-url", "data-img", "src"):
        src = img_elem.get(attr)
        if src and not src.startswith("data:image"):
            return src.strip()
    return ""


def _extract_team_info(match, is_variant_2: bool) -> tuple:
    if is_variant_2:
        team1_elem = match.select_one(".right-team .team-name, .right-team .name, .right-team h3, .team-1 .team-name, .team-1 .name")
        team2_elem = match.select_one(".left-team .team-name, .left-team .name, .left-team h3, .team-2 .team-name, .team-2 .name")
        t1_img_elem = match.select_one(".right-team .team-logo img, .right-team img, .team-1 .team-logo img, .team-1 img")
        t2_img_elem = match.select_one(".left-team .team-logo img, .left-team img, .team-2 .team-logo img, .team-2 img")
    else:
        team1_elem = match.select_one(".TM1 .TM_Name, .TM1 .team-name, .TM1 .name, .TeamA_Name, .HTeam_Name")
        team2_elem = match.select_one(".TM2 .TM_Name, .TM2 .team-name, .TM2 .name, .TeamB_Name, .ATeam_Name")
        t1_img_elem = match.select_one(".TM1 .TM_Logo img, .TM1 img")
        t2_img_elem = match.select_one(".TM2 .TM_Logo img, .TM2 img")

    # Look for explicit time elements first (.EventTime, .MT_Time, .match-time, etc.)
    time_elem = match.select_one(
        ".EventTime, .MT_Time, .match-time, .time, .match-timing .EventTime, "
        ".match_time, .matchTime, .match-date-time, [class*='EventTime'], [class*='match-time']"
    )

    t1_name = team1_elem.get_text(strip=True) if team1_elem else "Unknown Team 1"
    t2_name = team2_elem.get_text(strip=True) if team2_elem else "Unknown Team 2"
    t1_orig_img = _extract_image_src(t1_img_elem)
    t2_orig_img = _extract_image_src(t2_img_elem)

    time_str = ""
    if time_elem:
        raw_text = time_elem.get_text(strip=True)
        # Extract time pattern if mixed with status/result text
        m = re.search(r'(\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:AM|PM|am|pm|مساءً|مساء|صباحاً|صباح|ص|م))?)', raw_text)
        if m:
            time_str = m.group(1).strip()
        else:
            time_str = raw_text

    if not time_str:
        for attr in ("data-start", "data-time", "data-match-time"):
            val = match.get(attr, "")
            if val:
                m = re.search(r'(\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:AM|PM|am|pm|مساءً|مساء|صباحاً|صباح|ص|م))?)', val)
                if m:
                    time_str = m.group(1).strip()
                    break

    if not time_str:
        # Fallback: inspect any text inside .match-timing or .match-center
        center_elem = match.select_one(".match-timing, .match-center, .match-info")
        if center_elem:
            m = re.search(r'(\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:AM|PM|am|pm|مساءً|مساء|صباحاً|صباح|ص|م))?)', center_elem.get_text())
            if m:
                time_str = m.group(1).strip()

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
        status_class = _determine_status(classes, match)
        if not status_class:
            continue

        link_elem = match.find("a", href=True)
        if not link_elem and status_class != "finished":
            continue

        match_url = urljoin(source_url, link_elem["href"]) if link_elem else ""
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


def extract_channels(match_url: str, proxies: dict = None) -> list[dict]:
    """Extracts the single stream channel from a Yalla-Shoot match page as a standardised channel list."""
    iframe_url = extract_iframe(match_url, proxies=proxies)
    if not iframe_url:
        return []
    return [{"id": 1, "name": "Live 1", "quality": "HD", "type": "iframe", "url": iframe_url}]


