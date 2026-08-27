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
    parse_user_styled_time,
    format_to_human_time,
    get_event_display_name,
    resolve_timezone,
    is_match_expired,
    is_match_ended,
    DEFAULT_HEADERS,
)

from translation_manager import find_existing_translation, resolve_missing_teams
from iframe_resolver import resolve_match_channels
from scrapers import SCRAPER_PLUGINS
from normalization import (
    are_arabic_names_equivalent,
    are_english_teams_equivalent,
    get_arabic_match_fingerprint,
    normalize_arabic_text
)

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


def migrate_matches_cache_translations(matches_cache: dict, team_translations: dict, slots: list = None) -> dict:
    """
    Re-evaluates cached match entries against the latest team translations.
    Updates event IDs, team names, and logo URLs if teams were previously Unknown or had placeholder codes.
    Merges duplicate entries and updates assigned slot event IDs in-place.
    """
    if not matches_cache or not team_translations:
        return matches_cache

    slots = slots or []
    migrated_count = 0

    for old_event_id, cached_match in list(matches_cache.items()):
        t1_ar = cached_match.get("team1_ar", "").strip()
        t2_ar = cached_match.get("team2_ar", "").strip()
        if not t1_ar and not t2_ar:
            continue

        t1_info = find_existing_translation(t1_ar, team_translations) if t1_ar else None
        t2_info = find_existing_translation(t2_ar, team_translations) if t2_ar else None

        t1_en = t1_info.get("nameEn", "") if t1_info else cached_match.get("team1_en", "")
        t2_en = t2_info.get("nameEn", "") if t2_info else cached_match.get("team2_en", "")
        t1_code = t1_info.get("code", "###") if t1_info else "###"
        t2_code = t2_info.get("code", "###") if t2_info else "###"
        t1_img = t1_info.get("logo_url") if (t1_info and t1_info.get("logo_url")) else cached_match.get("team1_img", "")
        t2_img = t2_info.get("logo_url") if (t2_info and t2_info.get("logo_url")) else cached_match.get("team2_img", "")

        raw_k_time = str(cached_match.get("kickoff_time", "")).strip()
        kickoff_iso = ""
        if raw_k_time:
            try:
                dt = parse_user_styled_time(raw_k_time)
                if dt != datetime.min:
                    kickoff_iso = dt.isoformat()
            except Exception:
                kickoff_iso = ""

        if not kickoff_iso:
            date_match = re.search(r'(\d{2})-(\d{2})-(\d{2})$', old_event_id)
            if date_match:
                yy, mm, dd = date_match.groups()
                kickoff_iso = f"20{yy}-{mm}-{dd}"

        new_event_id = generate_stable_event_id(t1_code, t2_code, kickoff_iso)

        name_changed = (t1_en != cached_match.get("team1_en")) or (t2_en != cached_match.get("team2_en"))
        id_changed = (new_event_id != old_event_id)
        img_changed = (t1_img != cached_match.get("team1_img")) or (t2_img != cached_match.get("team2_img"))

        if not name_changed and not id_changed and not img_changed:
            continue

        t1_display = t1_en or t1_ar
        t2_display = t2_en or t2_ar
        new_event_name = f"{t1_display} vs {t2_display}" if (t1_display and t2_display) else new_event_id

        cached_match["event_id"] = new_event_id
        cached_match["event_name"] = new_event_name
        cached_match["team1_en"] = t1_en
        cached_match["team2_en"] = t2_en
        if t1_img:
            cached_match["team1_img"] = t1_img
        if t2_img:
            cached_match["team2_img"] = t2_img

        if id_changed:
            if new_event_id in matches_cache:
                # Merge into existing target entry if duplicate already exists
                existing = matches_cache[new_event_id]
                if not existing.get("links") and cached_match.get("links"):
                    existing["links"] = cached_match["links"]
                del matches_cache[old_event_id]
                logger.info(f"Translation: Merged duplicate match '{old_event_id}' into '{new_event_id}' ({new_event_name})")
            else:
                matches_cache[new_event_id] = cached_match
                del matches_cache[old_event_id]
                logger.info(f"Translation: Migrated match cache ID '{old_event_id}' -> '{new_event_id}' ({new_event_name})")

            # Update matching slot references so stream slots don't get displaced
            for slot in slots:
                if slot.get("event_id") == old_event_id:
                    slot["event_id"] = new_event_id
                    slot["event_name"] = new_event_name
                    slot_name = slot.get("slot") or f"#{slot.get('row_num', '')}"
                    logger.info(f"Slots: Updated slot {slot_name} event ID to '{new_event_id}'")

            migrated_count += 1
        else:
            matches_cache[old_event_id] = cached_match

    # Pass 2: Merge any remaining duplicate matches in cache that represent the same match
    cleaned_cache = {}
    for ev_id, match in matches_cache.items():
        k_time = match.get("kickoff_time", "")
        t1_en = match.get("team1_en", "") or match.get("team1_ar", "")
        t2_en = match.get("team2_en", "") or match.get("team2_ar", "")

        duplicate_target_id = None
        for exist_id, exist_match in cleaned_cache.items():
            if exist_match.get("kickoff_time") == k_time:
                ex_t1 = exist_match.get("team1_en", "") or exist_match.get("team1_ar", "")
                ex_t2 = exist_match.get("team2_en", "") or exist_match.get("team2_ar", "")
                if (are_english_teams_equivalent(t1_en, ex_t1) and are_english_teams_equivalent(t2_en, ex_t2)) or \
                   (are_arabic_names_equivalent(t1_en, ex_t1) and are_arabic_names_equivalent(t2_en, ex_t2)):
                    duplicate_target_id = exist_id
                    break

        if duplicate_target_id:
            target = cleaned_cache[duplicate_target_id]
            if not target.get("links") and match.get("links"):
                target["links"] = match["links"]
            if target.get("status_class") != "live" and match.get("status_class") == "live":
                target["status_class"] = "live"
            logger.info(f"Cache: Consolidated duplicate match '{ev_id}' into '{duplicate_target_id}'")
            migrated_count += 1
        else:
            cleaned_cache[ev_id] = match

    matches_cache.clear()
    matches_cache.update(cleaned_cache)

    if migrated_count > 0:
        logger.success(f"Translation: Migrated/cleaned {migrated_count} match cache entries with updated translations.")

    return matches_cache


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


def _is_duplicate_raw_match(match_data: dict, existing_matches: list) -> bool:
    """Checks if a match was already scraped from another source using date and Arabic team equivalence."""
    d_new = match_data.get("date_str", "").strip()
    t1_new = match_data["team1_name"].strip()
    t2_new = match_data["team2_name"].strip()
    for ex in existing_matches:
        if ex.get("date_str", "").strip() == d_new:
            ex_t1 = ex["team1_name"].strip()
            ex_t2 = ex["team2_name"].strip()
            if (are_arabic_names_equivalent(t1_new, ex_t1) and are_arabic_names_equivalent(t2_new, ex_t2)) or \
               (are_arabic_names_equivalent(t1_new, ex_t2) and are_arabic_names_equivalent(t2_new, ex_t1)):
                return True
    return False


def _fetch_and_parse_urls(urls_to_scrape: list) -> tuple[list, set]:
    matches_to_process = []
    unique_team_names = set()
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

            # Deduplicate across multiple sources by normalized team names and date
            if _is_duplicate_raw_match(match_data, matches_to_process):
                continue

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
    # Only force finished if explicitly marked finished by user via Telegram
    if cached_match and cached_match.get("status_class") == "finished":
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

    channels = []
    if status_class != "finished" and match_url:
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

    is_ended = status_class == "finished"
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
    parsed_matches_map = {}
    updated_matches_cache = {}
    if matches_to_process:
        logger.info(f"Scraper: Resolving stream channels for {len(matches_to_process)} matches...")

    for match_data in matches_to_process:
        event, cache_entry = _build_match_event(match_data, team_translations, matches_cache, now_dt, proxies)
        if event is not None:
            ev_id = event["event_id"]
            if ev_id in parsed_matches_map:
                existing_ev = parsed_matches_map[ev_id]
                if not existing_ev.get("channels") and event.get("channels"):
                    existing_ev["channels"] = event["channels"]
                if event.get("status_class") == "live" and existing_ev.get("status_class") != "live":
                    existing_ev["status_class"] = "live"
            else:
                parsed_matches_map[ev_id] = event

            # Prioritize 'live' status entries when merging cache entries for shared events
            for ev_id, entry in cache_entry.items():
                if ev_id in updated_matches_cache:
                    existing_status = updated_matches_cache[ev_id].get("status_class")
                    if existing_status == "live" and entry.get("status_class") != "live":
                        continue
                updated_matches_cache[ev_id] = entry

    # Retain matches from previous cache that disappeared from competitor sources but haven't expired (3h post-match TTL)
    if matches_cache:
        for ev_id, cached_entry in matches_cache.items():
            if ev_id not in updated_matches_cache:
                k_time = cached_entry.get("kickoff_time", "")
                duration = int(cached_entry.get("duration", 180))
                if is_match_expired(k_time, duration, now_dt, grace_minutes=180):
                    continue

                # Check if an equivalent match was already processed under another ID in this run
                c_t1 = cached_entry.get("team1_en") or cached_entry.get("team1_ar", "")
                c_t2 = cached_entry.get("team2_en") or cached_entry.get("team2_ar", "")
                is_duplicate = False
                for up_id, up_entry in updated_matches_cache.items():
                    u_k_time = up_entry.get("kickoff_time", "")
                    if u_k_time and k_time and u_k_time == k_time:
                        u_t1 = up_entry.get("team1_en") or up_entry.get("team1_ar", "")
                        u_t2 = up_entry.get("team2_en") or up_entry.get("team2_ar", "")
                        if (are_english_teams_equivalent(c_t1, u_t1) and are_english_teams_equivalent(c_t2, u_t2)) or \
                           (are_arabic_names_equivalent(c_t1, u_t1) and are_arabic_names_equivalent(c_t2, u_t2)):
                            is_duplicate = True
                            break

                if is_duplicate:
                    logger.info(f"Cache: Pruned obsolete/duplicate cache entry '{ev_id}' (superseded by active scrape).")
                    continue

                retained_entry = dict(cached_entry)
                # If match duration has passed (~135 min realistic duration) or it was marked finished, ensure it stays finished and link is cleared
                if retained_entry.get("status_class") == "finished" or is_match_ended(k_time, min(duration, 135), now_dt):
                    retained_entry["status_class"] = "finished"
                    retained_entry["is_ended"] = True
                    retained_entry["links"] = ""

                updated_matches_cache[ev_id] = retained_entry

    return list(parsed_matches_map.values()), updated_matches_cache


def scrape_live_matches(team_translations: dict = None, matches_cache: dict = None, slots: list = None) -> tuple:
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

    # Re-evaluate cached matches & slots with updated translations and purge duplicate IDs
    migrate_matches_cache_translations(matches_cache, team_translations, slots)

    now_dt = get_now_local()
    parsed_matches, updated_matches_cache = _process_matches(
        matches_to_process, team_translations, matches_cache, now_dt, get_request_proxies()
    )

    return parsed_matches, new_translations_list, updated_matches_cache, alias_updates


