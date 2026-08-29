import sys
import subprocess
from datetime import datetime
import requests
from utils import (
    load_env,
    send_telegram_message,
    get_allowed_chat_ids,
    get_slot_label,
    get_telegram_bot_token,
    get_blog_id,
    get_blog_player_id,
    get_spreadsheet_name,
    format_to_human_time
)

load_env()

import sheets_module
import scraper_module
import blogger_module
import translation_manager
import logger


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
    """Handles /end command to manually mark a match finished."""
    if not arg:
        send_telegram_message(bot_token, chat_id, "Usage: /end <team_name>")
        return

    if clients["sheets"] is None:
        clients["sheets"] = sheets_module.get_gspread_client()

    team_translations = translation_manager.load_team_translations(clients["sheets"], spreadsheet_name)
    matches_cache = sheets_module.fetch_matches_cache(clients["sheets"], spreadsheet_name)
    slots = sheets_module.fetch_all_slots(clients["sheets"], spreadsheet_name)

    scraped_events, _, _, _ = scraper_module.scrape_live_matches(
        team_translations=team_translations, matches_cache=matches_cache, slots=slots
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
            "links": "",
            "status_class": "finished",
            "is_ended": True,
            "last_updated": datetime.now().isoformat()
        })
    else:
        matches_cache[event_id] = {
            "event_id": event_id,
            "event_name": match_name,
            "team1_en": t1_en,
            "team2_en": t2_en,
            "team1_ar": t1_ar,
            "team2_ar": t2_ar,
            "team1_img": found_match['team1'].get('img', ''),
            "team2_img": found_match['team2'].get('img', ''),
            "links": "",
            "kickoff_time": kickoff_time,
            "duration": int(found_match.get("duration", 180)),
            "status_class": "finished",
            "is_ended": True,
            "last_updated": datetime.now().isoformat()
        }
    sheets_module.save_matches_cache(clients["sheets"], matches_cache, spreadsheet_name)

    msg = f"Match '{match_name}' marked as ended. Triggering blog updates now..."
    send_telegram_message(bot_token, chat_id, msg)
    logger.success(f"Telegram Bot: Match '{match_name}' marked as finished in cache.")
    subprocess.run([sys.executable, "run_pipeline.py", "--telegram-report-chat-id", str(chat_id)])


def _format_match_line(idx: int, ev: dict, slots: list, posts_map: dict) -> str:
    """Formats a single scraped match into a telegram status message."""
    t1 = ev['team1'].get('nameEn') or ev['team1'].get('nameAr')
    t2 = ev['team2'].get('nameEn') or ev['team2'].get('nameAr')
    status = ev.get('status_class', 'unknown').upper()

    assigned_slot_label = ""
    permalink = ""
    for s in slots:
        slot_status = s.get("status", "").strip().lower()
        ev_name = s.get("event_name", "").strip().lower()
        if slot_status in ["valid", "active"] and ev_name not in ["", "free"] and s.get("event_id") == ev["event_id"]:
            assigned_slot_label = get_slot_label(s)
            blog_post_id = s.get("blog_post_id") or s.get("post_id")
            post_info = posts_map.get(blog_post_id)
            if post_info and post_info.get("status") in ["LIVE", None]:
                permalink = post_info.get("url", "")
            break

    line = f"[{idx}] {t1} vs {t2} ({status})"
    if assigned_slot_label:
        line += f"\n   Slot: {assigned_slot_label}"
        if permalink:
            line += f"\n   Link: {permalink}"
    else:
        line += "\n   (Not streaming)"
    return line


def _handle_match_command(chat_id: int, bot_token: str, spreadsheet_name: str, blog_id: str, clients: dict):
    """Handles /match command to display all currently scraped matches."""
    if clients["sheets"] is None:
        clients["sheets"] = sheets_module.get_gspread_client()
    if clients["blogger"] is None:
        clients["blogger"] = blogger_module.get_blogger_session()

    slots = sheets_module.fetch_all_slots(clients["sheets"], spreadsheet_name)
    team_translations = translation_manager.load_team_translations(clients["sheets"], spreadsheet_name)
    matches_cache = sheets_module.fetch_matches_cache(clients["sheets"], spreadsheet_name)

    scraped_events, _, _, _ = scraper_module.scrape_live_matches(
        team_translations=team_translations, matches_cache=matches_cache, slots=slots
    )

    try:
        posts_map = blogger_module.fetch_posts_map(clients["blogger"], blog_id, status="live,draft")
    except Exception as e:
        logger.error(f"Telegram Bot: Error fetching Blogger posts: {e}")
        posts_map = {}

    match_lines = [_format_match_line(idx, ev, slots, posts_map) for idx, ev in enumerate(scraped_events, 1)]
    response_text = "Scraped Matches:\n\n" + "\n\n".join(match_lines) if match_lines else "No matches currently scraped."
    send_telegram_message(bot_token, chat_id, response_text)



def _handle_check_command(chat_id: int, bot_token: str):
    """Handles /check command to execute pipeline run."""
    send_telegram_message(bot_token, chat_id, "Triggering pipeline execution immediately...")
    logger.info("Telegram Bot: Triggering pipeline check subprocess...")
    subprocess.run([sys.executable, "run_pipeline.py", "--telegram-report-chat-id", str(chat_id)])


def _handle_sync_command(chat_id: int, bot_token: str):
    """Handles /sync command to trigger sync_data_page directly from cache."""
    send_telegram_message(bot_token, chat_id, "Syncing Data Website directly from Sheets cache...")
    logger.info("Telegram Bot: Triggering sync_data_page subprocess...")
    proc = subprocess.run([sys.executable, "sync_data_page.py"], capture_output=True, text=True)
    if proc.returncode == 0:
        send_telegram_message(bot_token, chat_id, "✅ Data Website successfully synced with latest cache.")
    else:
        err_snippet = (proc.stderr or proc.stdout)[:300]
        send_telegram_message(bot_token, chat_id, f"❌ Data Website sync failed: {err_snippet}")


def _process_update(update: dict, bot_token: str, blog_id: str, spreadsheet_name: str, allowed_chat_ids: list, clients: dict):
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
        _handle_match_command(chat_id, bot_token, spreadsheet_name, blog_id, clients)
    elif cmd == "/check":
        _handle_check_command(chat_id, bot_token)
    elif cmd == "/sync":
        _handle_sync_command(chat_id, bot_token)


def main():
    bot_token = get_telegram_bot_token()
    blog_id = get_blog_id() or get_blog_player_id()
    spreadsheet_name = get_spreadsheet_name()

    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is not set.")
        sys.exit(1)
    if not blog_id:
        logger.error("BLOG_ID or BLOG_PLAYER_ID environment variable is not set.")
        sys.exit(1)

    allowed_chat_ids = get_allowed_chat_ids()
    updates = _poll_telegram_updates(bot_token)
    if not updates:
        sys.exit(0)

    logger.info(f"Telegram Bot: Processing {len(updates)} updates.")
    clients = {"sheets": None, "blogger": None}
    max_update_id = -1

    for update in updates:
        uid = update.get("update_id", -1)
        if uid > max_update_id:
            max_update_id = uid
        _process_update(update, bot_token, blog_id, spreadsheet_name, allowed_chat_ids, clients)

    _acknowledge_updates(bot_token, max_update_id)


if __name__ == "__main__":
    main()

