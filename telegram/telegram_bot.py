import os
import sys
import subprocess
from datetime import datetime
import requests

# Ensure parent directory (Pipeline) is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils import (
    load_env,
    send_telegram_message,
    get_allowed_chat_ids,
    get_telegram_bot_token,
    get_spreadsheet_name,
    get_player_base_url,
    format_to_human_time,
    get_status_priority,
    parse_iso_time
)

load_env()

import sheets_module
import scraper_module
import translation_manager
import logger
import patcher
from steps import sync_data


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
        clients["sheets"] = sheets_module.get_gspread_client()

    team_translations = translation_manager.load_team_translations(clients["sheets"], spreadsheet_name)
    matches_cache = sheets_module.fetch_matches_cache(clients["sheets"], spreadsheet_name)

    scraped_events, _, _, _ = scraper_module.scrape_live_matches(
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

    matches_cache = sheets_module.fetch_matches_cache(clients["sheets"], spreadsheet_name)
    if event_id in matches_cache:
        matches_cache[event_id].update({
            "status_class": "finished",
            "last_updated": datetime.now().isoformat()
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
            "link": found_match.get("link", ""),
            "channels": ch_payload,
            "kickoff_time": kickoff_time,
            "duration": int(found_match.get("duration", 140)),
            "status_class": "finished",
            "last_updated": datetime.now().isoformat()
        }
    sheets_module.save_matches_cache(clients["sheets"], matches_cache, spreadsheet_name)

    msg = f"Match '{match_name}' marked as ended. Triggering fast sync now..."
    send_telegram_message(bot_token, chat_id, msg)
    logger.success(f"Telegram Bot: Match '{match_name}' marked as finished in cache.")
    
    # Run fast direct sync
    ok = sync_data.run_sync(sheets_client=clients["sheets"], spreadsheet_name=spreadsheet_name)
    if ok:
        send_telegram_message(bot_token, chat_id, f"✅ Match '{match_name}' ended and Cloudflare updated.")
    else:
        send_telegram_message(bot_token, chat_id, f"⚠️ Match '{match_name}' marked ended, but Cloudflare sync failed.")


def _format_match_line(idx: int, ev: dict, player_base_url: str) -> str:
    """Formats a single scraped match into a telegram status message."""
    t1 = ev['team1'].get('nameEn') or ev['team1'].get('nameAr')
    t2 = ev['team2'].get('nameEn') or ev['team2'].get('nameAr')
    status = ev.get('status_class', 'unknown').upper()
    time_str = format_to_human_time(ev.get('time', ''))
    ev_id = ev.get('event_id', '')
    base_url = (player_base_url or "").rstrip("?")
    link = f"{base_url}?match={ev_id}" if (ev_id and base_url) else ""
    ch_cnt = len(ev.get('channels', []))

    line = f"[{idx}] {t1} vs {t2} ({status}) - {time_str}"
    if ch_cnt > 0:
        line += f"\n   Channels: {ch_cnt}"
    if link:
        line += f"\n   Link: {link}"
    return line


def _handle_match_command(chat_id: int, bot_token: str, spreadsheet_name: str, player_base_url: str, clients: dict):
    """Handles /match command to display all currently scraped matches."""
    if clients["sheets"] is None:
        clients["sheets"] = sheets_module.get_gspread_client()

    team_translations = translation_manager.load_team_translations(clients["sheets"], spreadsheet_name)
    matches_cache = sheets_module.fetch_matches_cache(clients["sheets"], spreadsheet_name)

    scraped_events, _, _, _ = scraper_module.scrape_live_matches(
        team_translations=team_translations, matches_cache=matches_cache
    )

    def _telegram_sort_key(ev):
        prio = get_status_priority(
            ev.get("status_class", "upcoming"),
            has_stream=bool(ev.get("channels") or ev.get("link"))
        )
        dt = parse_iso_time(ev.get("time", ""))
        t_val = dt.timestamp() if dt != datetime.min else 0.0
        time_key = -t_val if (prio == 0 or ev.get("status_class") == "finished") else t_val
        return (-prio, time_key)

    scraped_events.sort(key=_telegram_sort_key)

    match_lines = [_format_match_line(idx, ev, player_base_url) for idx, ev in enumerate(scraped_events, 1)]
    response_text = "Scraped Matches:\n\n" + "\n\n".join(match_lines) if match_lines else "No matches currently scraped."
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
    ok = sync_data.run_sync(sheets_client=clients.get("sheets"), spreadsheet_name=spreadsheet_name)
    if ok:
        send_telegram_message(bot_token, chat_id, "✅ Fast sync completed! Cloudflare feeds are updated.")
    else:
        send_telegram_message(bot_token, chat_id, "❌ Fast sync failed. Check pipeline logs.")


def _process_update(update: dict, bot_token: str, spreadsheet_name: str, player_base_url: str, allowed_chat_ids: list, clients: dict):
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
        _handle_match_command(chat_id, bot_token, spreadsheet_name, player_base_url, clients)
    elif cmd in ("/run", "/check"):
        _handle_run_command(chat_id, bot_token)
    elif cmd == "/sync":
        _handle_sync_command(chat_id, bot_token, spreadsheet_name, clients)
    elif cmd in ("/start", "/help"):
        help_msg = (
            "🤖 *TiviGoal Pipeline Bot*\n\n"
            "• `/run` - Run full pipeline (scrape competitor sites & sync)\n"
            "• `/sync` - Fast 1s sync (push Google Sheets directly to Cloudflare)\n"
            "• `/match` - View all scraped matches and channel links\n"
            "• `/end <team>` - Mark a match as ended"
        )
        send_telegram_message(bot_token, chat_id, help_msg)


def main():
    bot_token = get_telegram_bot_token()
    spreadsheet_name = get_spreadsheet_name()
    player_base_url = get_player_base_url()

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
        _process_update(update, bot_token, spreadsheet_name, player_base_url, allowed_chat_ids, clients)

    _acknowledge_updates(bot_token, max_update_id)


if __name__ == "__main__":
    main()
