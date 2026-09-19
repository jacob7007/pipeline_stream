import os
import json
import re
from datetime import datetime, timedelta
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
    is_match_in_24h_window,
    is_match_starting_soon,
    get_match_default_duration_minutes,
    DEFAULT_HEADERS,
    PLACEHOLDER_IMAGE_URL,
    sanitize_sheet_image_url,
)

from translation_manager import find_existing_translation, resolve_missing_teams
import channels_engine
from channels_engine import resolve_match_channels
import patcher
from scrapers import SCRAPER_PLUGINS, ScraperPluginError
from normalization import (
    are_arabic_names_equivalent,
    are_english_teams_equivalent,
    slugify_team_name,
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


def generate_stable_event_id(t1_name_en: str, t2_name_en: str, kickoff_iso: str) -> str:
    # Use name slugs with full date DD-MM-YYYY format for globally unique stable event slugs
    s1 = slugify_team_name(t1_name_en)
    s2 = slugify_team_name(t2_name_en)
    match = re.search(r'(\d{4})-(\d{2})-(\d{2})', str(kickoff_iso or ""))
    if match:
        yyyy, mm, dd = match.group(1), match.group(2), match.group(3)
        date_part = f"{dd}-{mm}-{yyyy}"
    else:
        date_part = "00-00-0000"
    return f"{s1}-vs-{s2}-{date_part}"



def migrate_matches_cache_translations(matches_cache: dict, team_translations: dict) -> dict:
    """
    Re-evaluates cached match entries against the latest team translations.
    Updates event IDs, team names, and logo URLs if teams were previously Unknown or had placeholder codes.
    Merges duplicate entries in-place.
    """
    if not matches_cache or not team_translations:
        return matches_cache

    migrated_count = 0

    for old_event_id, cached_match in list(matches_cache.items()):
        t1_ar = cached_match.get("team1_ar", "").strip()
        t2_ar = cached_match.get("team2_ar", "").strip()
        if not t1_ar and not t2_ar:
            continue

        t1_info = find_existing_translation(t1_ar, team_translations) if t1_ar else None
        t2_info = find_existing_translation(t2_ar, team_translations) if t2_ar else None

        # Cross-type guard: Clubs and National teams cannot play each other
        if t1_info and t2_info:
            t1_is_club = t1_info.get("type") == "club" and t1_info.get("nameEn") not in ("Unknown", "", None)
            t2_is_club = t2_info.get("type") == "club" and t2_info.get("nameEn") not in ("Unknown", "", None)
            t1_is_nat = t1_info.get("type") == "national" or bool(t1_info.get("code"))
            t2_is_nat = t2_info.get("type") == "national" or bool(t2_info.get("code"))
            if t1_is_club and t2_is_nat:
                t2_info = {"nameEn": "Unknown", "code": "", "type": "club", "primary_arabic": t2_ar, "logo_url": ""}
            elif t2_is_club and t1_is_nat:
                t1_info = {"nameEn": "Unknown", "code": "", "type": "club", "primary_arabic": t1_ar, "logo_url": ""}

        t1_en = t1_info.get("nameEn", "") if t1_info else cached_match.get("team1_en", "")
        t2_en = t2_info.get("nameEn", "") if t2_info else cached_match.get("team2_en", "")
        if t1_info and (t1_info.get("type") == "national" or t1_info.get("code")):
            t1_img = t1_info.get("logo_url") or cached_match.get("team1_img", "").strip() or PLACEHOLDER_IMAGE_URL
        else:
            t1_img = cached_match.get("team1_img", "").strip() or PLACEHOLDER_IMAGE_URL

        if t2_info and (t2_info.get("type") == "national" or t2_info.get("code")):
            t2_img = t2_info.get("logo_url") or cached_match.get("team2_img", "").strip() or PLACEHOLDER_IMAGE_URL
        else:
            t2_img = cached_match.get("team2_img", "").strip() or PLACEHOLDER_IMAGE_URL

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
            date_match = re.search(r'(\d{2})-(\d{2})-(\d{4})$', old_event_id)
            if date_match:
                dd, mm, yyyy = date_match.groups()
                kickoff_iso = f"{yyyy}-{mm}-{dd}"
            else:
                legacy_match = re.search(r'(\d{2})-(\d{2})-(\d{2})$', old_event_id)
                if legacy_match:
                    yy, mm, dd = legacy_match.groups()
                    kickoff_iso = f"20{yy}-{mm}-{dd}"

        new_event_id = generate_stable_event_id(t1_en, t2_en, kickoff_iso)

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
                if not existing.get("link") and cached_match.get("link"):
                    existing["link"] = cached_match["link"]
                del matches_cache[old_event_id]
                logger.info(f"Translation: Merged duplicate match '{old_event_id}' into '{new_event_id}' ({new_event_name})")
            else:
                matches_cache[new_event_id] = cached_match
                del matches_cache[old_event_id]
                logger.info(f"Translation: Migrated match cache ID '{old_event_id}' -> '{new_event_id}' ({new_event_name})")

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
            if not target.get("link") and match.get("link"):
                target["link"] = match["link"]
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


def _fetch_single_url_matches(url: str, source_tz, clean_url: str, max_url_len: int) -> tuple[list, str, str]:
    site_tz = resolve_timezone(source_tz)
    site_now = datetime.now(site_tz).replace(tzinfo=None)
    default_date = (site_now + timedelta(days=1)).strftime("%Y-%m-%d") if "tomorrow" in url.lower() else site_now.strftime("%Y-%m-%d")

    padded_url = clean_url.ljust(max_url_len)

    # Generic pre-flight: query plugins to identify candidate plugin from URL before HTTP GET
    candidate_plugin = next((p for p in SCRAPER_PLUGINS if hasattr(p, "can_handle_url") and p.can_handle_url(url)), None)
    inferred_source = (
        candidate_plugin.get_source_name(url) if hasattr(candidate_plugin, "get_source_name")
        else getattr(candidate_plugin, "PLUGIN_NAME", candidate_plugin.__name__.split(".")[-1])
    ) if candidate_plugin else ""

    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=20, proxies=get_request_proxies())
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Scraper: Fetching matches from: {padded_url}  :  ❌  Failed: {e}")
        return [], str(e), inferred_source

    soup = BeautifulSoup(resp.text, "html.parser")
    handler = next((p for p in SCRAPER_PLUGINS if p.can_handle(soup)), candidate_plugin)
    if handler is None:
        logger.error(f"Scraper: Fetching matches from: {padded_url}  :  ❌  No plugin recognized HTML structure.")
        telegram_token = get_telegram_bot_token()
        alert_chat_ids = get_allowed_chat_ids()
        broadcast_telegram(
            telegram_token, alert_chat_ids,
            f"⚠️ Scraper: Unrecognized website structure\n\nURL: {url}\n\nNo scraper plugin matched."
        )
        return [], "No plugin recognized HTML structure", inferred_source

    source_name = (
        handler.get_source_name(url) if hasattr(handler, "get_source_name")
        else getattr(handler, "PLUGIN_NAME", handler.__name__.split(".")[-1])
    )
    try:
        raw_matches = handler.parse_matches(soup, url, default_date, source_tz)
    except ScraperPluginError as spe:
        logger.warning(f"Scraper: Fetching matches from: {padded_url}  :  ⚠️  {spe.message}")
        return [], spe.message, source_name
    except Exception as e:
        logger.error(f"Scraper: Parsing matches from {padded_url} failed: {e}")
        return [], str(e), source_name

    for match in raw_matches:
        match["plugin"] = source_name
    logger.info(f"Scraper: Fetching matches from: {padded_url}  :  {logger.COLOR_CYAN}➤{logger.COLOR_RESET}  Found {len(raw_matches)} matches.")
    return raw_matches, "", source_name


def _is_duplicate_raw_match(match_data: dict, existing_matches: list) -> bool:
    """Checks if a match was already scraped from another source using date and team equivalence."""
    d_new = match_data.get("date_str", "").strip()
    t1_new = match_data["team1_name"].strip()
    t2_new = match_data["team2_name"].strip()
    for ex in existing_matches:
        if ex.get("date_str", "").strip() == d_new:
            ex_t1 = ex["team1_name"].strip()
            ex_t2 = ex["team2_name"].strip()
            match_t1_t1 = are_arabic_names_equivalent(t1_new, ex_t1) or are_english_teams_equivalent(t1_new, ex_t1)
            match_t2_t2 = are_arabic_names_equivalent(t2_new, ex_t2) or are_english_teams_equivalent(t2_new, ex_t2)
            match_t1_t2 = are_arabic_names_equivalent(t1_new, ex_t2) or are_english_teams_equivalent(t1_new, ex_t2)
            match_t2_t1 = are_arabic_names_equivalent(t2_new, ex_t1) or are_english_teams_equivalent(t2_new, ex_t1)
            if (match_t1_t1 and match_t2_t2) or (match_t1_t2 and match_t2_t1):
                return True
    return False


def _fetch_and_parse_urls(urls_to_scrape: list) -> tuple[list, set, dict]:
    matches_to_process = []
    unique_team_names = set()
    errors = []
    plugin_health = {}

    max_url_len = max((len(url.split("://")[-1].rstrip("/")) for url, _ in urls_to_scrape), default=0)

    for url, source_tz in urls_to_scrape:
        clean_url = url.split("://")[-1].rstrip("/")
        raw_matches, err, plugin_name = _fetch_single_url_matches(url, source_tz, clean_url, max_url_len)
        if plugin_name:
            if err:
                errors.append(f"{clean_url}: {err}")
                if plugin_name not in plugin_health:
                    plugin_health[plugin_name] = False
            else:
                plugin_health[plugin_name] = True

        site_matches = []
        for match_data in raw_matches:
            t1 = match_data["team1_name"].strip()
            t2 = match_data["team2_name"].strip()

            # Deduplicate multiple occurrences within the same website page
            if _is_duplicate_raw_match(match_data, site_matches):
                continue

            site_matches.append(match_data)
            unique_team_names.add(t1)
            unique_team_names.add(t2)
            matches_to_process.append(match_data)

    if not matches_to_process and errors:
        raise ConnectionError("; ".join(errors))

    return matches_to_process, unique_team_names, plugin_health



def _build_match_event(match_data: dict, team_translations: dict, matches_cache: dict, now_dt: datetime, proxies: dict) -> tuple:
    t1_name, t2_name = match_data["team1_name"], match_data["team2_name"]
    t1_info = team_translations.get(t1_name) or {"nameEn": "Unknown", "code": "", "type": "club"}
    t2_info = team_translations.get(t2_name) or {"nameEn": "Unknown", "code": "", "type": "club"}

    # Cross-type guard: Clubs and National teams cannot play each other
    t1_is_club = t1_info.get("type") == "club" and t1_info.get("nameEn") not in ("Unknown", "", None)
    t2_is_club = t2_info.get("type") == "club" and t2_info.get("nameEn") not in ("Unknown", "", None)
    t1_is_nat = t1_info.get("type") == "national" or bool(t1_info.get("code"))
    t2_is_nat = t2_info.get("type") == "national" or bool(t2_info.get("code"))
    if t1_is_club and t2_is_nat:
        logger.warning(f"Translation: Cross-type collision '{t1_name}' (Club) vs '{t2_name}' (National). Demoting '{t2_name}' to club.")
        t2_info = {"nameEn": "Unknown", "code": "", "type": "club", "primary_arabic": t2_name, "logo_url": ""}
    elif t2_is_club and t1_is_nat:
        logger.warning(f"Translation: Cross-type collision '{t1_name}' (National) vs '{t2_name}' (Club). Demoting '{t1_name}' to club.")
        t1_info = {"nameEn": "Unknown", "code": "", "type": "club", "primary_arabic": t1_name, "logo_url": ""}

    team1_en = t1_info.get("nameEn", "")
    team2_en = t2_info.get("nameEn", "")

    formatted_time = parse_match_time(match_data["date_str"], match_data["time_str"], source_tz=match_data.get("source_tz"))
    event_id = generate_stable_event_id(team1_en, team2_en, formatted_time)


    match_url = match_data["match_url"]
    cached_match = matches_cache.get(event_id) if event_id else None
    status_class = match_data.get("status_class", "upcoming")
    if status_class not in ["live", "upcoming", "finished"]:
        status_class = "upcoming"

    # Only force finished if explicitly marked finished by user via Telegram
    if cached_match and cached_match.get("status_class") == "finished":
        status_class = "finished"

    # Ignore matches scheduled more than 24 hours into the future
    if not is_match_in_24h_window(formatted_time, now_dt):
        return None, {}

    # Check if match is starting soon (<= 60m) or live to decide whether to extract stream channels
    is_soon = is_match_starting_soon(formatted_time, now_dt, status_class=status_class, threshold_minutes=60)
    is_far_future = not is_soon and status_class == "upcoming"

    t1_display = team1_en or (t1_info.get("primary_arabic") or match_data["team1_name"])
    t2_display = team2_en or (t2_info.get("primary_arabic") or match_data["team2_name"])
    match_display_name = f"{t1_display} vs {t2_display}" if (t1_display and t2_display) else event_id

    channels = []
    if status_class != "finished" and match_url:
        channels = resolve_match_channels(
            match_url=match_url,
            status_class=status_class,
            is_far_future=is_far_future,
            plugin_name=match_data.get("plugin", ""),
            proxies=proxies,
            context={"match_name": match_display_name, "event_id": event_id},
        )

    team1_ar = t1_info.get("primary_arabic") or match_data["team1_name"]
    team2_ar = t2_info.get("primary_arabic") or match_data["team2_name"]

    # Logo resolution:
    # - National teams: prioritize high-quality vector flags from cache (FlagCDN), fallback to scraped image or placeholder
    # - Clubs: ALWAYS use scraped images (NEVER use cache URLs). Fallback to placeholder if missing.
    if t1_info.get("type") == "national" or t1_info.get("code"):
        team1_img = t1_info.get("logo_url") or match_data.get("team1_orig_img", "").strip() or PLACEHOLDER_IMAGE_URL
    else:
        team1_img = match_data.get("team1_orig_img", "").strip() or PLACEHOLDER_IMAGE_URL

    if t2_info.get("type") == "national" or t2_info.get("code"):
        team2_img = t2_info.get("logo_url") or match_data.get("team2_orig_img", "").strip() or PLACEHOLDER_IMAGE_URL
    else:
        team2_img = match_data.get("team2_orig_img", "").strip() or PLACEHOLDER_IMAGE_URL

    existing_link = cached_match.get("link", "") if cached_match else ""
    channels_payload = patcher.encode_channels_payload(channels) if channels else ""

    prev_cached_channels = cached_match.get("channels", "") if cached_match else ""
    if channels:
        channels_val = channels_payload
        effective_channels = channels
    elif prev_cached_channels and patcher.is_valid_base64_payload(prev_cached_channels):
        channels_val = prev_cached_channels
        effective_channels = patcher.decode_channels_payload(prev_cached_channels)
    else:
        channels_val = ""
        effective_channels = []

    plugin_name = match_data.get("plugin", "") or (cached_match.get("plugin", "") if cached_match else "")
    cached_sources = cached_match.get("sources", []) if cached_match else []
    sources = list(dict.fromkeys([p for p in (cached_sources + ([plugin_name] if plugin_name else [])) if p]))

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
        "duration": get_match_default_duration_minutes(),
        "channels": effective_channels,
        "link": existing_link,
        "status_class": status_class,
        "match_url": match_url,
        "plugin": plugin_name,
        "sources": sources,
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
            "link": existing_link,
            "channels": channels_val,
            "kickoff_time": format_to_human_time(formatted_time),
            "duration": get_match_default_duration_minutes(),
            "status_class": status_class,
            "plugin": plugin_name,
            "sources": sources,
            "last_updated": now_dt.isoformat()
        }
    }
    return event, cache_entry


def _merge_channel_lists(channels_a: list, channels_b: list) -> list:
    """Merges two channel lists, deduplicating by stream endpoint and sorting by priority."""
    if not channels_a:
        return channels_b or []
    if not channels_b:
        return channels_a or []

    merged = []
    seen_endpoints = set()

    for ch in (channels_a + channels_b):
        if not isinstance(ch, dict):
            continue
        ctype = ch.get("type", "").strip().lower()
        if ctype == "dash":
            endpoint = ("dash", ch.get("manifest", "").strip())
        elif ctype == "hls":
            endpoint = ("hls", ch.get("url", "").strip())
        elif ctype == "iframe":
            raw_u = ch.get("url", "").strip()
            m_match = re.search(r'[?&](?:m|match)=(\d+)', raw_u)
            if m_match:
                endpoint = ("iframe", raw_u.split("?")[0], m_match.group(1))
            else:
                endpoint = ("iframe", raw_u)
        else:
            endpoint = (ctype, ch.get("url", "").strip())

        if endpoint in seen_endpoints:
            continue
        seen_endpoints.add(endpoint)
        merged.append(dict(ch))

    merged.sort(key=lambda c: (
        channels_engine.get_channel_priority(c),
        int(c.get("id", 999)) if str(c.get("id", "")).isdigit() else 999
    ))

    for idx, c in enumerate(merged, start=1):
        c["id"] = idx
        c["name"] = f"Live {idx}"

    return merged


def _process_matches(
    matches_to_process: list,
    team_translations: dict,
    matches_cache: dict,
    now_dt: datetime,
    proxies: dict,
    plugin_health: dict = None
) -> tuple:
    parsed_matches_map = {}
    updated_matches_cache = {}

    unique_soon_matches = set()
    for m in matches_to_process:
        if m.get("match_url"):
            k_time = parse_match_time(m.get("date_str", ""), m.get("time_str", ""), source_tz=m.get("source_tz"))
            if is_match_starting_soon(k_time, now_dt, status_class=m.get("status_class", ""), threshold_minutes=60):
                t1 = m.get("team1_name", "").strip()
                t2 = m.get("team2_name", "").strip()
                unique_soon_matches.add((t1, t2, k_time))

    active_channel_matches = len(unique_soon_matches)

    if active_channel_matches > 0:
        match_str = f"{active_channel_matches} match live / starting soon" if active_channel_matches == 1 else f"{active_channel_matches} matches live / starting soon"
        logger.item(f"Scraper: Resolving stream channels for {match_str}...")
    elif matches_to_process:
        logger.info(f"Scraper: No matches are live or starting soon. {logger.COLOR_DARK_GRAY}Skipping stream channel resolution.{logger.COLOR_RESET}")

    for match_data in matches_to_process:
        event, cache_entry = _build_match_event(match_data, team_translations, matches_cache, now_dt, proxies)
        if event is not None:
            ev_id = event["event_id"]
            existing_target_id = None
            if ev_id in parsed_matches_map:
                existing_target_id = ev_id
            else:
                for exist_id, exist_ev in parsed_matches_map.items():
                    if exist_ev.get("time") == event.get("time"):
                        t1_a = event["team1"]["nameEn"] or event["team1"]["nameAr"]
                        t2_a = event["team2"]["nameEn"] or event["team2"]["nameAr"]
                        t1_b = exist_ev["team1"]["nameEn"] or exist_ev["team1"]["nameAr"]
                        t2_b = exist_ev["team2"]["nameEn"] or exist_ev["team2"]["nameAr"]
                        if (are_english_teams_equivalent(t1_a, t1_b) and are_english_teams_equivalent(t2_a, t2_b)) or \
                           (are_arabic_names_equivalent(t1_a, t1_b) and are_arabic_names_equivalent(t2_a, t2_b)):
                            existing_target_id = exist_id
                            break

            if existing_target_id:
                existing_ev = parsed_matches_map[existing_target_id]
                existing_ev["channels"] = _merge_channel_lists(existing_ev.get("channels", []), event.get("channels", []))
                merged_sources = list(dict.fromkeys(existing_ev.get("sources", []) + event.get("sources", [])))
                existing_ev["sources"] = merged_sources
                if event.get("status_class") == "live" and existing_ev.get("status_class") != "live":
                    existing_ev["status_class"] = "live"
                if not existing_ev.get("link") and event.get("link"):
                    existing_ev["link"] = event["link"]
            else:
                parsed_matches_map[ev_id] = event

            # Prioritize 'live' status entries when merging cache entries for shared events
            target_key = existing_target_id or ev_id
            for entry_id, entry in cache_entry.items():
                if target_key in updated_matches_cache:
                    existing_status = updated_matches_cache[target_key].get("status_class")
                    if existing_status == "live" and entry.get("status_class") != "live":
                        continue
                    entry["sources"] = list(dict.fromkeys(updated_matches_cache[target_key].get("sources", []) + entry.get("sources", [])))
                updated_matches_cache[target_key] = entry

            # Keep cache channels payload in sync with merged channels
            if target_key in updated_matches_cache and target_key in parsed_matches_map:
                merged_ch = parsed_matches_map[target_key].get("channels", [])
                if merged_ch:
                    updated_matches_cache[target_key]["channels"] = patcher.encode_channels_payload(merged_ch)
                else:
                    prev_c = matches_cache.get(target_key, {}).get("channels", "") if matches_cache else ""
                    if prev_c and patcher.is_valid_base64_payload(prev_c):
                        updated_matches_cache[target_key]["channels"] = prev_c
                        parsed_matches_map[target_key]["channels"] = patcher.decode_channels_payload(prev_c)
                    else:
                        updated_matches_cache[target_key]["channels"] = ""

    # Multi-source channel preservation:
    # If a match was in cache with channels from a plugin that glitched on this run,
    # preserve those cached channels alongside freshly scraped channels during the grace window
    if matches_cache and plugin_health:
        glitched_plugins = {p for p, is_ok in plugin_health.items() if is_ok is False}
        if glitched_plugins:
            for ev_id, ev in parsed_matches_map.items():
                cached_prev = matches_cache.get(ev_id)
                if not cached_prev:
                    for c_id, c_entry in matches_cache.items():
                        c_k = str(c_entry.get("kickoff_time", "")).strip()
                        ev_k = str(ev.get("time", "")).strip()
                        ev_human = format_to_human_time(ev_k)
                        time_matches = (c_k and (c_k == ev_human or c_k == ev_k)) or (
                            parse_user_styled_time(c_k) != datetime.min and parse_user_styled_time(c_k) == parse_user_styled_time(ev_k)
                        )
                        if time_matches:
                            t1_a = ev["team1"]["nameEn"] or ev["team1"]["nameAr"]
                            t2_a = ev["team2"]["nameEn"] or ev["team2"]["nameAr"]
                            t1_c = c_entry.get("team1_en") or c_entry.get("team1_ar", "")
                            t2_c = c_entry.get("team2_en") or c_entry.get("team2_ar", "")
                            if (are_english_teams_equivalent(t1_a, t1_c) and are_english_teams_equivalent(t2_a, t2_c)) or \
                               (are_arabic_names_equivalent(t1_a, t1_c) and are_arabic_names_equivalent(t2_a, t2_c)):
                                cached_prev = c_entry
                                break

                if cached_prev and cached_prev.get("channels"):
                    prev_sources = cached_prev.get("sources", [])
                    if not prev_sources and cached_prev.get("plugin"):
                        prev_sources = [cached_prev["plugin"]]

                    has_glitched_source = any(p in glitched_plugins for p in prev_sources)
                    if has_glitched_source:
                        cached_channels = patcher.decode_channels_payload(cached_prev["channels"])
                        if cached_channels:
                            combined_ch = _merge_channel_lists(ev.get("channels", []), cached_channels)
                            ev["channels"] = combined_ch
                            if ev_id in updated_matches_cache:
                                updated_matches_cache[ev_id]["channels"] = patcher.encode_channels_payload(combined_ch)
                                preserved_sources = list(dict.fromkeys(updated_matches_cache[ev_id].get("sources", []) + prev_sources))
                                updated_matches_cache[ev_id]["sources"] = preserved_sources
                                ev["sources"] = preserved_sources
                            logger.info(f"Scraper: Preserved cached channels from glitched source(s) for '{ev_id}'.")

    # Retain matches from previous cache that disappeared from competitor sources but haven't expired
    if matches_cache:
        for ev_id, cached_entry in matches_cache.items():
            if ev_id not in updated_matches_cache:
                k_time = cached_entry.get("kickoff_time", "")
                duration = int(cached_entry.get("duration", get_match_default_duration_minutes()))
                if is_match_expired(k_time, duration, now_dt):
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

                # Check if ANY of this match's source plugins glitched
                match_sources = cached_entry.get("sources", [])
                if not match_sources and cached_entry.get("plugin"):
                    match_sources = [cached_entry["plugin"]]

                is_plugin_glitched = bool(
                    plugin_health and any(plugin_health.get(p) is False for p in match_sources)
                )

                # Prune unbroadcasted matches that disappeared from all competitor scrapers past kickoff with no active stream
                has_active_link = bool(cached_entry.get("link") and str(cached_entry.get("link")).strip())
                has_active_stream = has_active_link

                dt_kickoff = parse_user_styled_time(k_time)
                if not has_active_stream and dt_kickoff != datetime.min and cached_entry.get("status_class") != "finished":
                    if now_dt >= dt_kickoff + timedelta(minutes=15):
                        if not is_plugin_glitched:
                            ev_name = cached_entry.get("event_name", ev_id)
                            logger.info(f"Cache: Pruned unbroadcasted match '{ev_name}' (disappeared from competitor sources past kickoff with no stream).")
                            continue
                        else:
                            ev_name = cached_entry.get("event_name", ev_id)
                            glitched_names = ", ".join([p for p in match_sources if plugin_health.get(p) is False])
                            logger.info(f"Cache: Retaining match '{ev_name}' despite missing from scrape (source plugin(s) '{glitched_names}' glitched).")

                retained_entry = dict(cached_entry)
                retained_entry["sources"] = match_sources
                if is_plugin_glitched:
                    retained_entry["source_glitched"] = True

                # If match duration has passed or it was marked finished, ensure it stays finished
                if retained_entry.get("status_class") == "finished" or is_match_ended(k_time, duration, now_dt):
                    retained_entry["status_class"] = "finished"

                updated_matches_cache[ev_id] = retained_entry

    return list(parsed_matches_map.values()), updated_matches_cache


def scrape_live_matches(
    team_translations: dict = None,
    matches_cache: dict = None,
    sheets_client=None,
    spreadsheet_name: str = None,
) -> tuple:
    if team_translations is None:
        team_translations = {}
    if matches_cache is None:
        matches_cache = {}

    if not SCRAPER_URLS:
        logger.error("SCRAPER_URLS environment variable is not set. Cannot run competitor scraper.")
        return [], [], {}, []

    matches_to_process, unique_team_names, plugin_health = _fetch_and_parse_urls(SCRAPER_URLS)
    if not matches_to_process:
        return [], [], {}, []

    print()
    if sheets_client:
        channels_engine.init_domain_cache(sheets_client, spreadsheet_name or "Streaming Dashboard")
    logger.success(f"Sheets: Loaded {len(team_translations)} team translations from cache.")
    print()

    missing_team_names = []
    for name in unique_team_names:
        existing = find_existing_translation(name, team_translations)
        if existing:
            team_translations[name] = existing
        else:
            missing_team_names.append(name)

    new_translations_list, alias_updates = resolve_missing_teams(missing_team_names, team_translations, matches_to_process)

    # Update logo URLs on existing cached club teams whenever fresh scraped images are found
    for m in matches_to_process:
        for t_name, orig_img in [
            (m.get("team1_name", ""), m.get("team1_orig_img", "")),
            (m.get("team2_name", ""), m.get("team2_orig_img", ""))
        ]:
            img = sanitize_sheet_image_url(orig_img)
            if not t_name or not img:
                continue
            team = find_existing_translation(t_name, team_translations)
            if team and team.get("type") != "national":
                if team.get("logo_url") != img:
                    team["logo_url"] = img
                    if team.get("row_num") and team.get("sheet_name"):
                        alias_updates.append((team["row_num"], team["sheet_name"], 5, img))

    # Re-evaluate cached matches with updated translations and purge duplicate IDs
    migrate_matches_cache_translations(matches_cache, team_translations)

    logger.success("Translation: Translation completed.")

    logger.step_header("3/4", "Resolving Streams")

    now_dt = get_now_local()
    parsed_matches, updated_matches_cache = _process_matches(
        matches_to_process, team_translations, matches_cache, now_dt, get_request_proxies(), plugin_health=plugin_health
    )

    return parsed_matches, new_translations_list, updated_matches_cache, alias_updates


