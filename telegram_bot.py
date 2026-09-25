import os
import sys
import subprocess
from datetime import datetime, timezone
import requests

from utils import (
    load_env,
    send_telegram_message,
    get_allowed_chat_ids,
    get_telegram_bot_token,
    get_spreadsheet_name,
    get_cloudflare_api_url,
    get_match_player_url,
    format_to_human_time,
    get_status_priority,
    parse_iso_time
)

load_env()

import sheets_client
import scraper_engine
import translation_manager
import logger
import patcher
from run_pipeline import run_sync, assemble_matches_feed, assemble_channels_map

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _poll_telegram_updates(bot_token: str) -> list:
    """Polls the Telegram Bot API for unread updates."""
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Telegram: getUpdates failed with status {resp.status_code}: {resp.text}")
            return []
        return resp.json().get("result", [])
    except Exception as e:
        logger.error(f"Telegram: Error polling updates: {e}")
        return []


def _acknowledge_updates(bot_token: str, max_update_id: int):
    """Acknowledges received updates so Telegram does not redeliver them."""
    if max_update_id == -1:
        return
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        requests.get(f"{url}?offset={max_update_id + 1}", timeout=10)
        logger.info(f"Telegram Bot: Acknowledged updates up to ID {max_update_id}")
    except Exception as e:
        logger.error(f"Telegram Bot: Failed to acknowledge updates: {e}")


def _find_matching_event(scraped_events: list, query: str) -> dict | None:
    """Finds a scraped event matching team name in Arabic or English."""
    q = query.lower()
    for ev in scraped_events:
        t1_en = ev['team1'].get('nameEn', '').lower()
        t1_ar = ev['team1'].get('nameAr', '').lower()
        t2_en = ev['team2'].get('nameEn', '').lower()
        t2_ar = ev['team2'].get('nameAr', '').lower()
        if q in t1_en or q in t1_ar or q in t2_en or q in t2_ar:
            return ev
    return None


def _handle_end_command(arg: str, chat_id: int, bot_token: str, spreadsheet_name: str, clients: dict):
    """Handles /end command to manually mark a match finished and sync."""
    if not arg:
        send_telegram_message(bot_token, chat_id, "Usage: /end <team_name>")
        return

    if clients["sheets"] is None:
        clients["sheets"] = sheets_client.get_gspread_client()

    team_translations = translation_manager.load_team_translations(clients["sheets"], spreadsheet_name)
    matches_cache = sheets_client.fetch_matches_cache(clients["sheets"], spreadsheet_name)

    scraped_events, _, _, _ = scraper_engine.scrape_live_matches(
        team_translations=team_translations, matches_cache=matches_cache
    )

    found_match = _find_matching_event(scraped_events, arg)
    if not found_match:
        send_telegram_message(bot_token, chat_id, f"Could not find any match featuring '{arg}'.")
        return

    event_id = found_match["event_id"]
    t1_en = found_match['team1'].get('nameEn', '')
    t1_ar = found_match['team1'].get('nameAr', '')
    t2_en = found_match['team2'].get('nameEn', '')
    t2_ar = found_match['team2'].get('nameAr', '')
    match_name = f"{t1_en or t1_ar} vs {t2_en or t2_ar}"
    kickoff_time = format_to_human_time(found_match.get("time", ""))

    matches_cache = sheets_client.fetch_matches_cache(clients["sheets"], spreadsheet_name)
    if event_id in matches_cache:
        matches_cache[event_id].update({
            "status_class": "finished",
            "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        })
    else:
        ch_payload = patcher.encode_channels_payload(found_match.get("channels", [])) if found_match.get("channels") else ""
        matches_cache[event_id] = {
            "event_id": event_id,
            "event_name": match_name,
            "team1_en": t1_en,
            "team2_en": t2_en,
            "team1_ar": t1_ar,
            "team2_ar": t2_ar,
            "team1_img": found_match['team1'].get('img', ''),
            "team2_img": found_match['team2'].get('img', ''),
            "link": get_match_player_url(event_id),
            "channels": ch_payload,
            "kickoff_time": kickoff_time,
            "duration": int(found_match.get("duration", 140)),
            "status_class": "finished",
            "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        }
    sheets_client.save_matches_cache(clients["sheets"], matches_cache, spreadsheet_name)

    msg = f"Match '{match_name}' marked as ended. Triggering fast sync now..."
    send_telegram_message(bot_token, chat_id, msg)
    logger.success(f"Telegram Bot: Match '{match_name}' marked as finished in cache.")
    
    # Run fast direct sync
    ok = run_sync(sheets_client_instance=clients["sheets"], spreadsheet_name=spreadsheet_name)
    if ok:
        send_telegram_message(bot_token, chat_id, f"✅ Match '{match_name}' ended and Cloudflare updated.")
    else:
        send_telegram_message(bot_token, chat_id, f"⚠️ Match '{match_name}' marked ended, but Cloudflare sync failed.")


def _format_match_line(idx: int, ev: dict) -> str:
    """Formats a single match from the API into a telegram status message."""
    t1 = ev.get('team1', {}).get('nameEn') or ev.get('team1', {}).get('nameAr', '')
    t2 = ev.get('team2', {}).get('nameEn') or ev.get('team2', {}).get('nameAr', '')
    ended_suffix = " (FINISHED)" if ev.get("ended") else ""
    time_str = format_to_human_time(ev.get('time', ''))
    ev_id = ev.get('id') or ev.get('event_id', '')
    link = get_match_player_url(ev_id)
    ch_raw = ev.get('channels', 0)
    ch_cnt = ch_raw if isinstance(ch_raw, int) else len(ch_raw) if isinstance(ch_raw, list) else 0

    line = f"[{idx}] {t1} vs {t2}{ended_suffix} - {time_str}"
    if ch_cnt > 0:
        line += f"\n   Channels: {ch_cnt}"
    if link:
        line += f"\n   Link: {link}"
    return line


def _check_is_synced(clients: dict, spreadsheet_name: str, cloudflare_api_url: str, api_matches: list) -> bool:
    """Checks whether the Cloudflare Worker API and Google Sheets cache are in sync."""
    try:
        if clients.get("sheets") is None:
            clients["sheets"] = sheets_client.get_gspread_client()

        matches_cache = sheets_client.fetch_matches_cache(clients["sheets"], spreadsheet_name)
        active_matches_list = assemble_matches_feed(matches_cache)
        clean_sheet_matches = [
            {k: v for k, v in m.items() if not k.startswith("_")}
            for m in active_matches_list
        ]

        if api_matches != clean_sheet_matches:
            return False

        # Verify channels map consistency
        try:
            resp = requests.get(f"{cloudflare_api_url.rstrip('/')}/channels", timeout=7)
            if resp.status_code == 200:
                data = resp.json()
                api_channels = data.get("channels", {}) if isinstance(data, dict) else {}
                sheet_channels = assemble_channels_map(matches_cache)
                if api_channels != sheet_channels:
                    return False
        except Exception:
            pass

        return True
    except Exception as e:
        logger.error(f"Telegram Bot: Error checking sync status against Sheets: {e}")
        return False


def _handle_match_command(chat_id: int, bot_token: str, spreadsheet_name: str, clients: dict):
    """Handles /match command to display API matches and check Sheets sync status."""
    cloudflare_api_url = get_cloudflare_api_url()
    if not cloudflare_api_url:
        send_telegram_message(bot_token, chat_id, "❌ CLOUDFLARE_API_URL is not configured.")
        return

    # Fetch live matches from Cloudflare API
    try:
        resp = requests.get(f"{cloudflare_api_url.rstrip('/')}/matches", timeout=10)
        if resp.status_code != 200:
            send_telegram_message(bot_token, chat_id, f"❌ Failed to fetch matches from API (HTTP {resp.status_code}).")
            return
        data = resp.json()
        api_matches = data.get("matches", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    except Exception as e:
        logger.error(f"Telegram Bot: Error fetching API matches: {e}")
        send_telegram_message(bot_token, chat_id, f"❌ Failed to connect to Cloudflare API: {e}")
        return

    # Format matches list
    if api_matches:
        match_lines = [_format_match_line(idx, ev) for idx, ev in enumerate(api_matches, 1)]
        matches_text = "\n\n".join(match_lines)
    else:
        matches_text = "No matches currently scheduled in API."

    # Check sync status against Google Sheets
    is_synced = _check_is_synced(clients, spreadsheet_name, cloudflare_api_url, api_matches)
    sync_status = "Synced" if is_synced else "Not synced, /sync"

    response_text = f"{matches_text}\n\n{sync_status}"
    send_telegram_message(bot_token, chat_id, response_text)


def _handle_run_command(chat_id: int, bot_token: str):
    """Handles /run command to execute full pipeline run (scrape + sync)."""
    send_telegram_message(bot_token, chat_id, "🚀 Triggering full pipeline execution (scrape & sync)...")
    logger.info("Telegram Bot: Triggering full pipeline run subprocess...")
    run_script = os.path.join(BASE_DIR, "run_pipeline.py")
    subprocess.run([sys.executable, run_script, "--telegram-report-chat-id", str(chat_id)])


def _handle_sync_command(chat_id: int, bot_token: str, spreadsheet_name: str, clients: dict):
    """Handles /sync command to trigger fast direct sync from Google Sheets in ~1s."""
    send_telegram_message(bot_token, chat_id, "⚡ Triggering fast Cloudflare sync from Google Sheets...")
    logger.info("Telegram Bot: Running fast direct sync...")
    ok = run_sync(sheets_client_instance=clients.get("sheets"), spreadsheet_name=spreadsheet_name)
    if ok:
        send_telegram_message(bot_token, chat_id, "✅ Fast sync completed! Cloudflare feeds are updated.")
    else:
        send_telegram_message(bot_token, chat_id, "❌ Fast sync failed. Check pipeline logs.")


def _process_update(update: dict, bot_token: str, spreadsheet_name: str, allowed_chat_ids: list, clients: dict):
    """Dispatches a single message update to the appropriate command handler."""
    message = update.get("message")
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()
    if not text or not chat_id:
        return

    if allowed_chat_ids and str(chat_id) not in allowed_chat_ids:
        logger.warning(f"Telegram: Unauthorized access attempt from chat ID {chat_id}")
        return

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    logger.info(f"Telegram: Command '{cmd}' received from chat {chat_id}")
    if cmd == "/end":
        _handle_end_command(arg, chat_id, bot_token, spreadsheet_name, clients)
    elif cmd == "/match":
        _handle_match_command(chat_id, bot_token, spreadsheet_name, clients)
    elif cmd in ("/run", "/check"):
        _handle_run_command(chat_id, bot_token)
    elif cmd == "/sync":
        _handle_sync_command(chat_id, bot_token, spreadsheet_name, clients)
    elif cmd in ("/start", "/help"):
        help_msg = (
            "🤖 *TiviGoal Pipeline Bot*\n\n"
            "• `/run` - Run full pipeline (scrape competitor sites & sync)\n"
            "• `/sync` - Fast 1s sync (push Google Sheets directly to Cloudflare)\n"
            "• `/match` - View API matches and sync status\n"
            "• `/end <team>` - Mark a match as ended"
        )
        send_telegram_message(bot_token, chat_id, help_msg)


def main():
    bot_token = get_telegram_bot_token()
    spreadsheet_name = get_spreadsheet_name()

    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is not set.")
        sys.exit(1)

    allowed_chat_ids = get_allowed_chat_ids()
    updates = _poll_telegram_updates(bot_token)
    if not updates:
        sys.exit(0)

    logger.info(f"Telegram Bot: Processing {len(updates)} updates.")
    clients = {"sheets": None}
    max_update_id = -1

    for update in updates:
        uid = update.get("update_id", -1)
        if uid > max_update_id:
            max_update_id = uid
        _process_update(update, bot_token, spreadsheet_name, allowed_chat_ids, clients)

    _acknowledge_updates(bot_token, max_update_id)


if __name__ == "__main__":
    main()
