import json
import time
import base64
from urllib.parse import unquote
from datetime import datetime

import sheets_module
import logger
import patcher
import cloudflare_module
from normalization import are_english_teams_equivalent, are_arabic_names_equivalent
from utils import (
    get_status_priority,
    parse_user_styled_time,
    parse_iso_time,
    format_to_human_time,
    resolve_timezone,
    get_now_local,
    is_match_expired,
    is_match_starting_soon,
    sanitize_sheet_image_url,
    PLACEHOLDER_IMAGE_URL,
    PipelineAbortError,
)


def _get_channels_count(channels_raw) -> int:
    """Extracts channel count from raw list, JSON string, or Base64 payload."""
    if not channels_raw:
        return 0
    if isinstance(channels_raw, list):
        return len(channels_raw)
    if isinstance(channels_raw, str):
        s = channels_raw.strip()
        if not s:
            return 0
        try:
            p = json.loads(s)
            if isinstance(p, list):
                return len(p)
        except Exception:
            pass
        try:
            d = base64.b64decode(s).decode("utf-8")
            p = json.loads(unquote(d))
            if isinstance(p, list):
                return len(p)
        except Exception:
            pass
    return 0


def assemble_matches_feed(matches_cache: dict) -> list[dict]:
    """Builds and sorts the standardized matches array for Cloudflare KV from matches_cache."""
    feed_list = []
    now_dt = get_now_local()
    for ev_id, match in matches_cache.items():
        t1_ar = match.get("team1_ar", "").strip()
        t1_en = match.get("team1_en", "").strip()
        t2_ar = match.get("team2_ar", "").strip()
        t2_en = match.get("team2_en", "").strip()

        if not t1_ar and not t1_en and "event_name" in match:
            ev_name = match.get("event_name", "").strip()
            if " vs " in ev_name:
                parts = ev_name.split(" vs ", 1)
                t1_en = parts[0].strip()
                t2_en = parts[1].strip()

        if not t1_ar and not t1_en and not t2_ar and not t2_en:
            continue

        raw_time = str(match.get("kickoff_time", "")).strip()
        duration = int(match.get("duration", 140))

        # Do not include expired matches (> 3 hours post-match TTL) in the feed
        if is_match_expired(raw_time, duration, now_dt, grace_minutes=180):
            continue

        time_iso = raw_time
        if raw_time and "T" not in raw_time:
            try:
                dt = parse_user_styled_time(raw_time)
                if dt != datetime.min:
                    time_iso = dt.replace(tzinfo=resolve_timezone(None)).isoformat()
            except Exception:
                time_iso = raw_time

        status_class = match.get("status_class", "upcoming").strip().lower()
        if status_class not in ["live", "upcoming", "finished"]:
            status_class = "upcoming"
        is_ended = (status_class == "finished")
        link = match.get("link", "")

        event_slug = match.get("event_id") or ev_id

        feed_list.append({
            "id": event_slug,
            "team1": {
                "nameAr": t1_ar or t1_en,
                "nameEn": t1_en or t1_ar,
                "img": sanitize_sheet_image_url(match.get("team1_img", "")) or PLACEHOLDER_IMAGE_URL
            },
            "team2": {
                "nameAr": t2_ar or t2_en,
                "nameEn": t2_en or t2_ar,
                "img": sanitize_sheet_image_url(match.get("team2_img", "")) or PLACEHOLDER_IMAGE_URL
            },
            "time": time_iso,
            "duration": duration,
            "channels": _get_channels_count(match.get("channels", "")),
            "link": link,
            "ended": is_ended,
            "_status_class": status_class,
        })

    # Deduplicate entries that share kickoff time and equivalent team names
    deduped_feed = []
    for item in feed_list:
        is_dup = False
        for exist in deduped_feed:
            if exist.get("time") == item.get("time"):
                e_t1 = exist["team1"].get("nameEn") or exist["team1"].get("nameAr", "")
                e_t2 = exist["team2"].get("nameEn") or exist["team2"].get("nameAr", "")
                i_t1 = item["team1"].get("nameEn") or item["team1"].get("nameAr", "")
                i_t2 = item["team2"].get("nameEn") or item["team2"].get("nameAr", "")
                if (are_english_teams_equivalent(i_t1, e_t1) and are_english_teams_equivalent(i_t2, e_t2)) or \
                   (are_arabic_names_equivalent(i_t1, e_t1) and are_arabic_names_equivalent(i_t2, e_t2)):
                    is_dup = True
                    if not exist.get("link") and item.get("link"):
                        exist["link"] = item["link"]
                    if item.get("channels", 0) > exist.get("channels", 0):
                        exist["channels"] = item["channels"]
                    break
        if not is_dup:
            deduped_feed.append(item)

    def _feed_sort_key(m):
        prio = get_status_priority(
            m["_status_class"],
            has_stream=(m.get("channels", 0) > 0)
        )
        dt = parse_iso_time(m.get("time", ""))
        t_val = dt.timestamp() if dt != datetime.min else 0.0
        time_key = -t_val if (prio == 0 or m.get("ended")) else t_val
        return (-prio, time_key)

    deduped_feed.sort(key=_feed_sort_key)
    return deduped_feed


def assemble_channels_map(matches_cache: dict) -> dict[str, list[dict]]:
    """
    Builds the Option 1 Key-Value map for POST /sync/channels:
    {
        "event_slug_1": [ { "id": 1, "name": "Live 1", ... }, ... ],
        "event_slug_2": [ ... ]
    }
    Only includes active (non-expired) matches from cache.
    """
    channels_map = {}
    now_dt = get_now_local()

    for ev_id, match in matches_cache.items():
        raw_time = str(match.get("kickoff_time", "")).strip()
        duration = int(match.get("duration", 140))
        if is_match_expired(raw_time, duration, now_dt, grace_minutes=180):
            continue

        event_slug = match.get("event_id") or ev_id
        raw_channels = match.get("channels", "")

        decoded_channels = []
        if isinstance(raw_channels, list):
            decoded_channels = raw_channels
        elif isinstance(raw_channels, str) and raw_channels:
            decoded_channels = patcher.decode_channels_payload(raw_channels)

        channels_map[event_slug] = decoded_channels or []

    return channels_map


def _set_match_links(matches_list: list[dict], player_base_url: str):
    """
    Injects {player_base_url}?match={id} for all matches in the feed.
    As long as a match is in the cache/feed, it has its player link.
    """
    base_url = (player_base_url or "").rstrip("?")
    for match in matches_list:
        match_id = match.get("id", "")
        if match_id and base_url:
            match["link"] = f"{base_url}?match={match_id}"
        else:
            match["link"] = match.get("link", "")


def display_data_matches(active_matches_list: list):
    """Renders an aligned CLI preview table of matches formatted for the data website / Cloudflare feed."""
    if not active_matches_list:
        return
    print()
    max_t1_len = max((len(m['team1'].get('nameEn') or m['team1']['nameAr']) for m in active_matches_list), default=15)
    max_t2_len = max((len(m['team2'].get('nameEn') or m['team2']['nameAr']) for m in active_matches_list), default=15)
    max_date_len = max((len(format_to_human_time(m.get('time', ''))) for m in active_matches_list), default=14)
    max_status_len = 8
    now_dt = get_now_local()

    for idx, m in enumerate(active_matches_list, start=1):
        t1 = m['team1'].get('nameEn') or m['team1']['nameAr']
        t2 = m['team2'].get('nameEn') or m['team2']['nameAr']
        aligned_teams = f"{t1:<{max_t1_len}} - {t2:<{max_t2_len}}"
        date_str = format_to_human_time(m.get('time', ''))
        aligned_date = f"{date_str:<{max_date_len}}"

        status_val = m.get("_status_class", "upcoming")
        cnt = m.get('channels', 0)
        is_ended = status_val == "finished" or m.get("ended")
        is_soon = is_match_starting_soon(m.get('time', ''), now_dt, status_class=status_val)
        ch_label = f"{cnt} Channel" if cnt <= 1 else f"{cnt} Channels"

        if is_ended:
            status = "FINISHED"
            status_styled = f"{logger.COLOR_DARK_GRAY}{status:<{max_status_len}}{logger.COLOR_RESET}"
            stream_part = f"{logger.COLOR_DARK_GRAY}{ch_label}{logger.COLOR_RESET}"
        elif status_val == "live":
            status = "LIVE"
            if cnt > 0:
                status_styled = f"{logger.COLOR_GREEN}{logger.COLOR_BOLD}{status:<{max_status_len}}{logger.COLOR_RESET}"
            else:
                status_styled = f"{logger.COLOR_YELLOW}SOON    {logger.COLOR_RESET}"
            stream_part = f"{logger.COLOR_GREEN}{logger.COLOR_BOLD}{ch_label}{logger.COLOR_RESET}"
        elif is_soon:
            status = "UPCOMING"
            status_styled = f"{logger.COLOR_YELLOW}{status:<{max_status_len}}{logger.COLOR_RESET}"
            stream_part = f"{logger.COLOR_GREEN}{logger.COLOR_BOLD}{ch_label}{logger.COLOR_RESET}"
        else:
            status = "UPCOMING"
            status_styled = f"{logger.COLOR_YELLOW}{status:<{max_status_len}}{logger.COLOR_RESET}"
            stream_part = f"{logger.COLOR_DARK_GRAY}{ch_label}{logger.COLOR_RESET}"

        print(f"  [{idx:2d}] {aligned_teams}  |  {aligned_date}  |  {status_styled}  |  {stream_part}")
    print()


def run(
    sheets_client,
    spreadsheet_name: str,
    matches_cache: dict,
    cloudflare_api_url: str,
    sync_token: str,
    player_base_url: str
) -> bool:
    """
    Sync Step: Generates Cloudflare matches & channels feeds, injects player links, updates Sheets cache, and pushes to KV.
    """
    try:
        active_matches_list = assemble_matches_feed(matches_cache)
        channels_map = assemble_channels_map(matches_cache)
        _set_match_links(active_matches_list, player_base_url)

        # Reflect updated player links back into matches_cache
        for item in active_matches_list:
            ev_id = item.get("id")
            if ev_id and ev_id in matches_cache:
                matches_cache[ev_id]["link"] = item.get("link", "")

        logger.info(f"Active matches formatted for Cloudflare feed ({len(active_matches_list)} matches):")
        display_data_matches(active_matches_list)

        sheets_module.save_matches_cache(sheets_client, matches_cache, spreadsheet_name)

        matches_sync_ok = cloudflare_module.sync_matches(active_matches_list, cloudflare_api_url, sync_token)
        if not matches_sync_ok:
            raise PipelineAbortError(
                "CLOUDFLARE MATCHES SYNC FAILED",
                "Failed to synchronize matches feed to Cloudflare Worker."
            )

        channels_sync_ok = cloudflare_module.sync_channels(channels_map, cloudflare_api_url, sync_token)
        if not channels_sync_ok:
            raise PipelineAbortError(
                "CLOUDFLARE CHANNELS SYNC FAILED",
                "Failed to synchronize channels map to Cloudflare Worker."
            )

        return True
    except PipelineAbortError:
        raise
    except Exception as e:
        raise PipelineAbortError("DATABASE SYNC OR CLOUDFLARE UPDATE FAILED", f"Error syncing feed: {e}")


def run_sync(
    sheets_client=None,
    spreadsheet_name: str = None,
    cloudflare_api_url: str = None,
    sync_token: str = None,
    player_base_url: str = None,
) -> bool:
    """
    Fast direct sync: Reads _cache_matches from Google Sheets, verifies Cloudflare API health,
    and pushes matches feed & channels map directly to Cloudflare in ~1s without scraping.
    """
    from utils import (
        get_spreadsheet_name,
        get_cloudflare_api_url,
        get_cloudflare_sync_token,
        get_player_base_url,
    )
    spreadsheet_name = spreadsheet_name or get_spreadsheet_name()
    cloudflare_api_url = cloudflare_api_url or get_cloudflare_api_url()
    sync_token = sync_token or get_cloudflare_sync_token()
    player_base_url = player_base_url or get_player_base_url()

    if not cloudflare_api_url or not sync_token:
        logger.error("Sync: Missing CLOUDFLARE_API_URL or CLOUDFLARE_SYNC_TOKEN.")
        return False

    logger.step_header("SYNC", "Fast Direct Cloudflare Sync from Google Sheets")

    # Probe Cloudflare API health & capture live remote state
    logger.info("Checking Cloudflare API endpoints /matches, /channels...")
    is_ok, err_msg = cloudflare_module.check_api_status(cloudflare_api_url)
    if not is_ok:
        logger.error(f"Sync: Cloudflare API health check failed: {err_msg}")
        return False
    logger.success("Cloudflare API endpoints are active and accessible.")

    # Initialize Google Sheets client
    if sheets_client is None:
        try:
            sheets_client = sheets_module.get_gspread_client()
            logger.success("Google Sheets client authorized.")
        except Exception as e:
            logger.error(f"Sync: Failed to authorize Google Sheets client: {e}")
            return False

    # Fetch cache directly from Sheets
    try:
        matches_cache = sheets_module.fetch_matches_cache(sheets_client, spreadsheet_name)
    except Exception as e:
        logger.error(f"Sync: Failed to load cache from Google Sheets: {e}")
        return False

    if not matches_cache:
        logger.warning("Sync: No matches found in cache sheet.")
        return True

    # Execute sync directly
    try:
        print()
        run(
            sheets_client=sheets_client,
            spreadsheet_name=spreadsheet_name,
            matches_cache=matches_cache,
            cloudflare_api_url=cloudflare_api_url,
            sync_token=sync_token,
            player_base_url=player_base_url,
        )
        logger.pipeline_end("Fast direct sync completed successfully", is_error=False)
        return True
    except PipelineAbortError as pe:
        logger.error(f"Sync Aborted: {pe.reason}")
        return False
    except Exception as e:
        logger.error(f"Sync: Unexpected error: {e}")
        return False
