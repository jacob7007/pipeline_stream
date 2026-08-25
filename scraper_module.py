import os
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests

import logger
from utils import (
    get_now_local,
    get_allowed_chat_ids,
    get_telegram_bot_token,
    broadcast_telegram,
    strip_timezone,
    parse_match_time,
    format_to_human_time,
    get_event_display_name,
    resolve_timezone,
    DEFAULT_HEADERS,
)

from translation_manager import find_existing_translation, resolve_missing_teams
from iframe_resolver import resolve_match_channels
from scrapers import SCRAPER_PLUGINS

# Parse SCRAPER_URLS from environment variable into (url, source_tz) tuples
SCRAPER_URLS_ENV = os.environ.get("SCRAPER_URLS", "").strip()
SCRAPER_URLS: list = []

if SCRAPER_URLS_ENV:
    raw_entries = []
    if SCRAPER_URLS_ENV.startswith("[") and SCRAPER_URLS_ENV.endswith("]"):
        try:
            parsed = json.loads(SCRAPER_URLS_ENV)
            if isinstance(parsed, list):
                raw_entries = parsed
        except Exception as e:
            logger.warning(f"Failed to parse SCRAPER_URLS as JSON: {e}")
    if not raw_entries:
        raw_entries = [u.strip() for u in SCRAPER_URLS_ENV.split(",") if u.strip()]

    for entry in raw_entries:
        if isinstance(entry, str) and entry.strip():
            SCRAPER_URLS.append((entry.strip(), None))
        elif isinstance(entry, dict) and entry.get("url", "").strip():
            url = entry["url"].strip()
            tz_val = entry.get("timezone") or entry.get("tz_offset")
            SCRAPER_URLS.append((url, tz_val))

SCRAPING_PROXY = os.environ.get("SCRAPING_PROXY")


def get_request_proxies() -> dict:
    if SCRAPING_PROXY:
        return {"http": SCRAPING_PROXY, "https": SCRAPING_PROXY}
    return None


def generate_stable_event_id(t1_code: str, t2_code: str, kickoff_iso: str) -> str:
    # Sanitize ### (unknown club sentinel) to "unk" so event_id is always URL-safe
    c1 = re.sub(r'#+', 'unk', (t1_code or "unk").strip().lower())
    c2 = re.sub(r'#+', 'unk', (t2_code or "unk").strip().lower())
    match = re.search(r'(\d{2})(\d{2})-(\d{2})-(\d{2})', kickoff_iso)
    if match:
        _, yy, mm, dd = match.groups()
        date_part = f"{yy}-{mm}-{dd}"
    else:
        date_part = "26-00-00"
    return f"{c1}-vs-{c2}-{date_part}"


def _fetch_single_url_matches(url: str, source_tz, clean_url: str, max_url_len: int) -> tuple[list, str]:
    site_tz = resolve_timezone(source_tz)
    site_now = datetime.now(site_tz).replace(tzinfo=None)
    default_date = (site_now + timedelta(days=1)).strftime("%Y-%m-%d") if "tomorrow" in url.lower() else site_now.strftime("%Y-%m-%d")

    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=20, proxies=get_request_proxies())
        resp.raise_for_status()
    except Exception as e:
        padded_url = clean_url.ljust(max_url_len)
        logger.error(f"Scraper: Fetching matches from: {padded_url}  :  ❌  Failed: {e}")
        return [], str(e)

    soup = BeautifulSoup(resp.text, "html.parser")
    handler = next((p for p in SCRAPER_PLUGINS if p.can_handle(soup)), None)
    if handler is None:
        padded_url = clean_url.ljust(max_url_len)
        logger.error(f"Scraper: Fetching matches from: {padded_url}  :  ❌  No plugin recognized HTML structure.")
        telegram_token = get_telegram_bot_token()
        alert_chat_ids = get_allowed_chat_ids()
        broadcast_telegram(
            telegram_token, alert_chat_ids,
            f"⚠️ Scraper: Unrecognized website structure\n\nURL: {url}\n\nNo scraper plugin matched."
        )
        return [], "No plugin recognized HTML structure"

    raw_matches = handler.parse_matches(soup, url, default_date, source_tz)
    plugin_name = handler.__name__.split(".")[-1]
    for match in raw_matches:
        match["plugin"] = plugin_name
    logger.info(f"Scraper: Fetching matches from: {clean_url}  :  {logger.COLOR_CYAN}➤{logger.COLOR_RESET}  Found {len(raw_matches)} matches.")
    return raw_matches, ""


def _fetch_and_parse_urls(urls_to_scrape: list) -> tuple[list, set]:
    matches_to_process = []
    unique_team_names = set()
    seen_matches = set()
    errors = []

    max_url_len = max((len(url.split("://")[-1].rstrip("/")) for url, _ in urls_to_scrape), default=0)

    for url, source_tz in urls_to_scrape:
        clean_url = url.split("://")[-1].rstrip("/")
        raw_matches, err = _fetch_single_url_matches(url, source_tz, clean_url, max_url_len)
        if err:
            errors.append(f"{clean_url}: {err}")

        for match_data in raw_matches:
            t1 = match_data["team1_name"].strip()
            t2 = match_data["team2_name"].strip()
            d_str = match_data.get("date_str", "").strip()

            # Deduplicate across multiple sources by normalized team names and date
            match_key = (t1.lower(), t2.lower(), d_str)
            if match_key in seen_matches:
                continue

            seen_matches.add(match_key)
            unique_team_names.add(t1)
            unique_team_names.add(t2)
            matches_to_process.append(match_data)

    if not matches_to_process and errors:
        raise ConnectionError("; ".join(errors))

    logger.info(f"Scraper: Processing {len(matches_to_process)} total matches...")
    return matches_to_process, unique_team_names



def _build_match_event(match_data: dict, team_translations: dict, matches_cache: dict, now_dt: datetime, proxies: dict) -> tuple:
    t1_name, t2_name = match_data["team1_name"], match_data["team2_name"]
    t1_info = team_translations.get(t1_name) or {"nameEn": "Unknown", "code": "###"}
    t2_info = team_translations.get(t2_name) or {"nameEn": "Unknown", "code": "###"}
    t1_code = t1_info.get("code", "###")
    t2_code = t2_info.get("code", "###")

    formatted_time = parse_match_time(match_data["date_str"], match_data["time_str"], source_tz=match_data.get("source_tz"))
    event_id = generate_stable_event_id(t1_code, t2_code, formatted_time)

    match_url = match_data["match_url"]
    cached_match = matches_cache.get(event_id) if event_id else None
    status_class = match_data["status_class"]
    # Only force finished if explicitly marked manually by user via Telegram
    if cached_match and cached_match.get("status_class") in ["finished", "manually-finished"]:
        status_class = "finished"

    kickoff_dt = datetime.min
    try:
        kickoff_dt = datetime.fromisoformat(strip_timezone(formatted_time))
    except Exception as e:
        logger.warning(f"Failed to parse kickoff time for match '{event_id}': {e}")

    is_far_future = False
    if status_class == "not-started" and kickoff_dt != datetime.min:
        time_until_kickoff = (kickoff_dt - now_dt).total_seconds()
        if time_until_kickoff > 24 * 3600:
            return None, {}
        if time_until_kickoff > 1 * 3600:
            is_far_future = True

    channels = resolve_match_channels(
        match_url=match_url,
        status_class=status_class,
        is_far_future=is_far_future,
        plugin_name=match_data.get("plugin", ""),
        proxies=proxies
    )

    team1_en = t1_info.get("nameEn", "")
    team1_ar = t1_info.get("primary_arabic") or match_data["team1_name"]
    team2_en = t2_info.get("nameEn", "")
    team2_ar = t2_info.get("primary_arabic") or match_data["team2_name"]
    team1_img = match_data["team1_orig_img"] or t1_info.get("logo_url", "")
    team2_img = match_data["team2_orig_img"] or t2_info.get("logo_url", "")

    is_ended = status_class in ["finished", "manually-finished"]
    existing_links = "" if is_ended else (cached_match.get("links", "") if cached_match else "")

    event = {
        "event_id": event_id,
        "team1": {
            "nameAr": team1_ar,
            "nameEn": team1_en,
            "img": team1_img
        },
        "team2": {
            "nameAr": team2_ar,
            "nameEn": team2_en,
            "img": team2_img
        },
        "time": formatted_time,
        "duration": 180,
        "channels": channels,
        "link": existing_links,
        "status_class": status_class,
        "match_url": match_url
    }

    cache_entry = {
        event_id: {
            "event_id": event_id,
            "event_name": get_event_display_name(event),
            "team1_en": team1_en,
            "team2_en": team2_en,
            "team1_ar": team1_ar,
            "team2_ar": team2_ar,
            "team1_img": team1_img,
            "team2_img": team2_img,
            "links": existing_links,
            "kickoff_time": format_to_human_time(formatted_time),
            "duration": 180,
            "status_class": status_class,
            "is_ended": is_ended,
            "last_updated": now_dt.isoformat()
        }
    }
    return event, cache_entry


def _process_matches(matches_to_process: list, team_translations: dict, matches_cache: dict, now_dt: datetime, proxies: dict) -> tuple:
    parsed_matches = []
    updated_matches_cache = {}
    if matches_to_process:
        logger.info(f"Scraper: Resolving stream channels for {len(matches_to_process)} matches...")

    for match_data in matches_to_process:
        event, cache_entry = _build_match_event(match_data, team_translations, matches_cache, now_dt, proxies)
        if event is not None:
            parsed_matches.append(event)
            # Prioritize 'live' status entries when merging cache entries for shared events
            for ev_id, entry in cache_entry.items():
                if ev_id in updated_matches_cache:
                    existing_status = updated_matches_cache[ev_id].get("status_class")
                    if existing_status == "live" and entry.get("status_class") != "live":
                        continue
                updated_matches_cache[ev_id] = entry

    return parsed_matches, updated_matches_cache


def scrape_live_matches(team_translations: dict = None, matches_cache: dict = None) -> tuple:
    if team_translations is None:
        team_translations = {}
    if matches_cache is None:
        matches_cache = {}

    if not SCRAPER_URLS:
        logger.error("SCRAPER_URLS environment variable is not set. Cannot run competitor scraper.")
        return [], [], {}, []

    matches_to_process, unique_team_names = _fetch_and_parse_urls(SCRAPER_URLS)
    if not matches_to_process:
        return [], [], {}, []

    print()
    logger.success(f"Translation: Loaded {len(team_translations)} team translations from cache.")

    missing_team_names = []
    for name in unique_team_names:
        existing = find_existing_translation(name, team_translations)
        if existing:
            team_translations[name] = existing
        else:
            missing_team_names.append(name)

    new_translations_list, alias_updates = resolve_missing_teams(missing_team_names, team_translations, matches_to_process)
    logger.success("Translation: Translation completed.")
    print()

    now_dt = get_now_local()
    parsed_matches, updated_matches_cache = _process_matches(
        matches_to_process, team_translations, matches_cache, now_dt, get_request_proxies()
    )

    return parsed_matches, new_translations_list, updated_matches_cache, alias_updates

