import json
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests
import logger
from utils import DEFAULT_HEADERS


def can_handle(soup: BeautifulSoup) -> bool:
    """Identifies FooTyy or TodayM matches widget markup."""
    has_widget_script = bool(soup.select_one("script[data-matches-widget]"))
    has_widget_iframe = bool(soup.select_one("iframe[src*='todaym'], iframe[src*='egy4']"))
    has_footyy_shell = bool(
        soup.title and "footyy" in soup.title.get_text().lower() and soup.select_one(".post-body iframe[src]")
    )
    return has_widget_script or has_widget_iframe or has_footyy_shell


def _fetch_embedded_widget_soup(soup: BeautifulSoup, source_url: str, proxies: dict = None) -> BeautifulSoup | None:
    """Fetches widget HTML when the page loads it inside a nested iframe."""
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
        logger.warning(f"Plugin (footyy/matches): Failed to fetch embedded widget frame '{target_url}': {e}")
    return None


def _calculate_status(start_iso: str, duration_min: int, ended: bool) -> str:
    """Returns live, not-started, or finished based on UTC start time and match duration."""
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
        logger.warning(f"Plugin (footyy/matches): Failed to evaluate match status for time '{start_iso}': {e}")
        return "not-started"


def _parse_iso_match_time(iso_str: str, default_date: str) -> tuple[str, str]:
    """Extracts date (YYYY-MM-DD) and 12-hour time from an ISO 8601 UTC string."""
    if not iso_str:
        return default_date, "12:00 AM"
    try:
        clean_iso = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%I:%M %p")
    except Exception:
        return default_date, "12:00 AM"


def parse_matches(soup: BeautifulSoup, source_url: str, default_date: str, source_tz: str | int = None) -> list:
    """Parses the FooTyy data-matches-widget JSON into the pipeline's raw match format."""
    widget_soup = soup
    widget_script = soup.select_one("script[data-matches-widget]")
    if not widget_script:
        fetched_soup = _fetch_embedded_widget_soup(soup, source_url)
        if fetched_soup:
            widget_soup = fetched_soup
            widget_script = widget_soup.select_one("script[data-matches-widget]")

    if not widget_script or not widget_script.string:
        logger.warning(f"Plugin (footyy/matches): No matches widget JSON found at {source_url}")
        return []

    try:
        raw_matches = json.loads(widget_script.string.strip())
    except Exception as e:
        logger.error(f"Plugin (footyy/matches): Failed to parse matches widget JSON: {e}")
        return []

    results = []
    # FooTyy widget timestamps are ISO 8601 UTC — treat as UTC unless caller overrides
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
