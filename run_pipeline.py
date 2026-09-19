import sys
import os
import json
import base64
import argparse
from urllib.parse import unquote
from datetime import datetime

from utils import (
    load_env,
    configure_utf8,
    format_to_human_time,
    get_allowed_chat_ids,
    get_spreadsheet_name,
    get_telegram_bot_token,
    get_cloudflare_api_url,
    get_cloudflare_sync_token,
    get_default_player_url,
    get_status_priority,
    get_match_lookahead_hours,
    parse_user_styled_time,
    parse_iso_time,
    resolve_timezone,
    get_now_local,
    is_match_expired,
    is_match_starting_soon,
    sanitize_sheet_image_url,
    PLACEHOLDER_IMAGE_URL,
    broadcast_telegram,
    PipelineAbortError
)

load_env()

import sheets_client
import cloudflare_client
import logger
import channels_engine
import scraper_engine
import translation_manager
import patcher
from normalization import are_english_teams_equivalent, are_arabic_names_equivalent

configure_utf8()

SPREADSHEET_NAME = get_spreadsheet_name()
CLOUDFLARE_API_URL = get_cloudflare_api_url()
CLOUDFLARE_SYNC_TOKEN = get_cloudflare_sync_token()
DEFAULT_PLAYER_URL = get_default_player_url()


# ============================================================================
# Step 1: Verification & Service Checks
# ============================================================================

def _verify_sheets_status(client, spreadsheet_name: str):
    """Verifies that the Google Sheets dashboard spreadsheet is accessible."""
    logger.info("Checking the dashboard sheets...")
    sheets_ok, sheets_err = sheets_client.check_sheets_status(client, spreadsheet_name)
    if not sheets_ok:
        logger.error(f"Sheets: Unexpected error opening spreadsheet '{spreadsheet_name}': {sheets_err}")
        raise PipelineAbortError(
            "INACCESSIBLE : DASHBOARD SHEETS",
            f"Google Sheets ({spreadsheet_name}) is not accessible: {sheets_err}"
        )
    logger.success("Dashboard sheets is active and accessible.")


def _verify_cloudflare_api(cloudflare_api_url: str):
    """Verifies that the Cloudflare Worker API is active and accessible via probe GET /matches and GET /channels."""
    if not cloudflare_api_url:
        logger.error("Cloudflare: CLOUDFLARE_API_URL is missing or empty.")
        raise PipelineAbortError(
            "MISSING CONFIG : CLOUDFLARE API",
            "CLOUDFLARE_API_URL environment variable is not configured."
        )

    logger.info("Checking Cloudflare API endpoints /matches, /channels...")
    is_ok, err_msg = cloudflare_client.check_api_status(cloudflare_api_url)
    if not is_ok:
        logger.error(f"Cloudflare API health check failed: {err_msg}")
        raise PipelineAbortError(
            "INACCESSIBLE : CLOUDFLARE API",
            f"Failed to connect to Cloudflare Worker API: {err_msg}"
        )
    logger.success("Cloudflare API endpoints are active and accessible.")


def verify_services(client, spreadsheet_name: str, cloudflare_api_url: str):
    """
    Step 1: Verifies that Google Sheets dashboard and Cloudflare API are active and accessible.
    Raises PipelineAbortError if any check fails.
    """
    _verify_sheets_status(client, spreadsheet_name)
    print()
    _verify_cloudflare_api(cloudflare_api_url)


# ============================================================================
# Step 2: Scraping Competitors & Translating Matches
# ============================================================================

def _display_scraped_events(scraped_events: list):
    """Renders aligned preview of scraped match events."""
    if not scraped_events:
        return
    print()
    max_t1_len = max((len(ev['team1'].get('nameEn') or ev['team1']['nameAr']) for ev in scraped_events), default=15)
    max_t2_len = max((len(ev['team2'].get('nameEn') or ev['team2']['nameAr']) for ev in scraped_events), default=15)

    max_date_len = max((len(format_to_human_time(ev['time'])) for ev in scraped_events), default=14)
    max_status_len = 8
    now_dt = get_now_local()

    for idx, ev in enumerate(scraped_events, 1):
        t1 = ev['team1'].get('nameEn') or ev['team1']['nameAr']
        t2 = ev['team2'].get('nameEn') or ev['team2']['nameAr']
        status = ev.get('status_class', 'upcoming').upper()
        ch_count = len(ev.get('channels', []))
        is_soon = is_match_starting_soon(ev.get('time', ''), now_dt, status_class=ev.get('status_class', ''))
        ch_label = f"{ch_count} Channel" if ch_count <= 1 else f"{ch_count} Channels"

        if status == "FINISHED":
            status_styled = f"{logger.COLOR_DARK_GRAY}{status:<{max_status_len}}{logger.COLOR_RESET}"
            stream_part = f"{logger.COLOR_DARK_GRAY}{ch_label}{logger.COLOR_RESET}"
        elif status == "LIVE":
            if ch_count > 0:
                status_styled = f"{logger.COLOR_GREEN}{logger.COLOR_BOLD}{status:<{max_status_len}}{logger.COLOR_RESET}"
            else:
                status_styled = f"{logger.COLOR_YELLOW}SOON    {logger.COLOR_RESET}"
            stream_part = f"{logger.COLOR_GREEN}{logger.COLOR_BOLD}{ch_label}{logger.COLOR_RESET}"
        elif is_soon:
            status_styled = f"{logger.COLOR_YELLOW}{status:<{max_status_len}}{logger.COLOR_RESET}"
            stream_part = f"{logger.COLOR_GREEN}{logger.COLOR_BOLD}{ch_label}{logger.COLOR_RESET}"
        else:
            status_styled = f"{logger.COLOR_YELLOW}{status:<{max_status_len}}{logger.COLOR_RESET}"
            stream_part = f"{logger.COLOR_DARK_GRAY}{ch_label}{logger.COLOR_RESET}"

        aligned_teams = f"{t1:<{max_t1_len}} - {t2:<{max_t2_len}}"
        kickoff_str = format_to_human_time(ev['time'])
        aligned_date = f"{kickoff_str:<{max_date_len}}"
        print(f"  [{idx:2d}] {aligned_teams}  |  {aligned_date}  |  {status_styled}  |  {stream_part}")


def _persist_scraper_translations(client, spreadsheet_name: str, new_translations: list, alias_updates: list):
    """Persists translation additions and alias updates to Google Sheets."""
    if new_translations:
        print()
        logger.info(f"Sheets: Saving {len(new_translations)} new translations back...")
        try:
            translation_manager.save_new_team_translations_separated(client, new_translations, spreadsheet_name)
        except Exception as e:
            logger.error(f"Sheets: Error saving translations: {e}")
    if alias_updates:
        alias_only_count = sum(1 for u in alias_updates if (len(u) < 3 or u[2] == 1))
        if alias_only_count > 0:
            print()
            logger.info(f"Sheets: Saving {alias_only_count} alias update{'s' if alias_only_count != 1 else ''} back...")
        try:
            translation_manager.update_team_aliases(client, alias_updates, spreadsheet_name)
        except Exception as e:
            logger.error(f"Sheets: Error saving alias updates: {e}")


def scrape_matches_step(
    client,
    spreadsheet_name: str
) -> tuple[list, dict, dict]:
    """
    Step 2: Scrapes live matches from competitors, renders display preview, and persists translations.
    Returns (scraped_events, team_translations, updated_matches_cache).
    """
    team_translations = translation_manager.load_team_translations(client, spreadsheet_name)
    matches_cache = sheets_client.fetch_matches_cache(client, spreadsheet_name)

    try:
        scraped_events, new_translations, updated_matches_cache, alias_updates = scraper_engine.scrape_live_matches(
            team_translations=team_translations,
            matches_cache=matches_cache,
            sheets_client=client,
            spreadsheet_name=spreadsheet_name,
        )
    except ConnectionError as ce:
        logger.error(f"Scraper: Connection failure fetching match sources: {ce}")
        raise PipelineAbortError(
            "SCRAPER : CONNECTION ERROR",
            f"Failed to connect to scraper sources: {ce}"
        )
    except Exception as e:
        logger.error(f"Scraper: Unexpected error during scraping: {e}")
        raise PipelineAbortError(
            "MATCH SCRAPING FAILED",
            f"Error during scraping: {e}"
        )

    if not scraped_events:
        logger.item("Scraper: 0 matches currently scheduled on competitor websites.")
        return [], team_translations, updated_matches_cache or matches_cache

    def _scraped_sort_key(ev):
        prio = get_status_priority(
            ev.get("status_class", "upcoming"),
            has_stream=bool(ev.get("channels") or ev.get("link"))
        )
        dt = parse_iso_time(ev.get("time", ""))
        t_val = dt.timestamp() if dt != datetime.min else 0.0
        time_key = -t_val if (prio == 0 or ev.get("status_class") == "finished") else t_val
        return (-prio, time_key)

    scraped_events.sort(key=_scraped_sort_key)
    lookahead_h = get_match_lookahead_hours()
    hours_str = f"{int(lookahead_h)}h" if lookahead_h.is_integer() else f"{lookahead_h}h"
    print()
    logger.item(f"Scraper: Scraped {len(scraped_events)} matches within the next {hours_str}:")
    _display_scraped_events(scraped_events)

    _persist_scraper_translations(client, spreadsheet_name, new_translations, alias_updates)
    return scraped_events, team_translations, updated_matches_cache


# ============================================================================
# Step 3: Formatting & Syncing to Cloudflare KV and Google Sheets
# ============================================================================

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


def sync_data_step(
    client,
    spreadsheet_name: str,
    matches_cache: dict,
    cloudflare_api_url: str,
    sync_token: str
) -> bool:
    """
    Step 3: Generates Cloudflare matches & channels feeds, updates Sheets cache, and pushes to KV.
    """
    try:
        active_matches_list = assemble_matches_feed(matches_cache)
        channels_map = assemble_channels_map(matches_cache)

        logger.info(f"Active matches formatted for Cloudflare feed ({len(active_matches_list)} matches):")
        display_data_matches(active_matches_list)

        sheets_client.save_matches_cache(client, matches_cache, spreadsheet_name)

        matches_sync_ok = cloudflare_client.sync_matches(active_matches_list, cloudflare_api_url, sync_token)
        if not matches_sync_ok:
            raise PipelineAbortError(
                "CLOUDFLARE MATCHES SYNC FAILED",
                "Failed to synchronize matches feed to Cloudflare Worker."
            )

        channels_sync_ok = cloudflare_client.sync_channels(channels_map, cloudflare_api_url, sync_token)
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
    sheets_client_instance=None,
    spreadsheet_name: str = None,
    cloudflare_api_url: str = None,
    sync_token: str = None,
    **kwargs
) -> bool:
    """
    Fast direct sync: Reads _cache_matches from Google Sheets, verifies Cloudflare API health,
    and pushes matches feed & channels map directly to Cloudflare in ~1s without scraping.
    """
    client = sheets_client_instance or kwargs.get("sheets_client")
    spreadsheet_name = spreadsheet_name or get_spreadsheet_name()
    cloudflare_api_url = cloudflare_api_url or get_cloudflare_api_url()
    sync_token = sync_token or get_cloudflare_sync_token()

    if not cloudflare_api_url or not sync_token:
        logger.error("Sync: Missing CLOUDFLARE_API_URL or CLOUDFLARE_SYNC_TOKEN.")
        return False

    logger.step_header("SYNC", "Fast Direct Cloudflare Sync from Google Sheets")

    # Probe Cloudflare API health & capture live remote state
    logger.info("Checking Cloudflare API endpoints /matches, /channels...")
    is_ok, err_msg = cloudflare_client.check_api_status(cloudflare_api_url)
    if not is_ok:
        logger.error(f"Sync: Cloudflare API health check failed: {err_msg}")
        return False
    logger.success("Cloudflare API endpoints are active and accessible.")

    # Initialize Google Sheets client
    if client is None:
        try:
            client = sheets_client.get_gspread_client()
            logger.success("Google Sheets client authorized.")
        except Exception as e:
            logger.error(f"Sync: Failed to authorize Google Sheets client: {e}")
            return False

    # Fetch cache directly from Sheets
    try:
        matches_cache = sheets_client.fetch_matches_cache(client, spreadsheet_name)
    except Exception as e:
        logger.error(f"Sync: Failed to load cache from Google Sheets: {e}")
        return False

    if not matches_cache:
        logger.warning("Sync: No matches found in cache sheet.")
        return True

    # Execute sync directly
    try:
        print()
        sync_data_step(
            client=client,
            spreadsheet_name=spreadsheet_name,
            matches_cache=matches_cache,
            cloudflare_api_url=cloudflare_api_url,
            sync_token=sync_token,
        )
        return True
    except PipelineAbortError as pe:
        logger.error(f"Sync Aborted: {pe.reason}")
        return False
    except Exception as e:
        logger.error(f"Sync: Unexpected error: {e}")
        return False


# ============================================================================
# CLI & Pipeline Execution
# ============================================================================

def _parse_cli_args():
    """Parses command line arguments for the pipeline runner."""
    parser = argparse.ArgumentParser(description="Football Stream Automation Pipeline")
    parser.add_argument("--sheet", type=str, default=SPREADSHEET_NAME, help="Google Sheets spreadsheet name or ID")
    parser.add_argument("--sync", action="store_true", help="Direct fast sync from Google Sheets without scraping")
    parser.add_argument("--telegram-report-chat-id", type=str, default="", help="Telegram chat ID for alert notifications")
    return parser.parse_args()


def _init_clients():
    """Initializes and authenticates the Google Sheets API client."""
    try:
        client = sheets_client.get_gspread_client()
        logger.success("Google Sheets client authorized.")
        return client
    except Exception as e:
        logger.error(f"Error initializing Google Sheets client: {e}")
        raise PipelineAbortError("GOOGLE SHEETS CLIENT INITIALIZATION FAILED", str(e))


def _send_domain_alerts(alerts: list, telegram_token: str, chat_ids: list, default_player_url: str = None) -> None:
    """Sends a single batch Telegram notification for all '--' iframe domains found this run.
    Each alert is enriched with the match player URL.
    """
    if not alerts or not telegram_token or not chat_ids:
        return

    base_url = (default_player_url or DEFAULT_PLAYER_URL or "").rstrip("?/")
    lines = []
    for i, alert in enumerate(alerts, start=1):
        domain       = alert.get("domain", "unknown")
        match_name   = alert.get("match_name", "Unknown Match")
        channel_name = alert.get("channel_name", "Unknown Channel")
        event_id     = alert.get("event_id", "")

        post_url = f"{base_url}/?match={event_id}" if (event_id and base_url) else ""

        line = f"{i}. {domain}\n   Match: {match_name}\n   Channel: {channel_name}"
        if post_url:
            line += f"\n   🔗 {post_url}"
        lines.append(line)

    body = "\n\n".join(lines)
    message = (
        "⚠️ Unverified iframe domains — Human Review Needed\n\n"
        f"{body}\n\n"
        "Open _cache_domains in Sheets and set each to OK or NO."
    )
    broadcast_telegram(telegram_token, chat_ids, message)
    logger.info(f"Telegram: Sent domain alert for {len(alerts)} unverified domain(s).")


def main():
    args = _parse_cli_args()
    telegram_token = get_telegram_bot_token()
    allowed_chat_ids = get_allowed_chat_ids(args.telegram_report_chat_id)

    try:
        if not CLOUDFLARE_API_URL or not CLOUDFLARE_SYNC_TOKEN:
            logger.error("Missing required environment variables: CLOUDFLARE_API_URL or CLOUDFLARE_SYNC_TOKEN")
            raise PipelineAbortError(
                "MISSING REQUIRED ENVIRONMENT VARIABLES",
                "Check CLOUDFLARE_API_URL and CLOUDFLARE_SYNC_TOKEN in .env"
            )

        if args.sync:
            print()
            client = _init_clients()
            ok = run_sync(
                sheets_client_instance=client,
                spreadsheet_name=args.sheet,
                cloudflare_api_url=CLOUDFLARE_API_URL,
                sync_token=CLOUDFLARE_SYNC_TOKEN,
            )
            print()
            print()
            sys.exit(0 if ok else 1)

        # Step 1: Verifying services
        print()
        logger.step_header("1/4", "Verifying Services")
        client = _init_clients()
        print()
        verify_services(client, args.sheet, CLOUDFLARE_API_URL)

        # Step 2: Scraping matches (and inside: Step 3: Resolving Streams)
        logger.step_header("2/4", "Scraping Matches")
        scraped_events, team_translations, matches_cache = scrape_matches_step(
            client, args.sheet
        )

        # Step 4: Syncing to Cloudflare
        logger.step_header("4/4", "Syncing to Cloudflare")
        sync_data_step(
            client, args.sheet, matches_cache,
            CLOUDFLARE_API_URL, CLOUDFLARE_SYNC_TOKEN
        )

        # Flush any new domain validator results to the _cache_domains sheet.
        channels_engine.flush_domain_cache(client, args.sheet)

        # Send one batch Telegram alert for all '--' (inconclusive) domains discovered this run.
        _send_domain_alerts(
            channels_engine.get_pending_alerts(),
            telegram_token,
            allowed_chat_ids,
            default_player_url=DEFAULT_PLAYER_URL,
        )
        print()
        print()

    except PipelineAbortError as err:
        if err.details:
            logger.error(f"Details: {err.details}")
        if telegram_token and allowed_chat_ids:
            details_part = f"\n\nDetails: {err.details}" if err.details else ""
            alert_text = f"🚨 Pipeline Alert!\n\nReason: {err.reason}{details_part}"
            broadcast_telegram(telegram_token, allowed_chat_ids, alert_text)
        logger.pipeline_end(err.reason, is_error=True)
        sys.exit(1)
    except KeyboardInterrupt:
        print()
        logger.error("Pipeline interrupted by user (SIGINT).")
        logger.pipeline_end("MANUAL STOP (CTRL + C)", is_error=True)
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected pipeline failure: {e}")
        logger.pipeline_end(f"PIPELINE CRASHED: {e}", is_error=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
