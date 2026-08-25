import json
from datetime import datetime
import sheets_module
import blogger_module
import patcher
import logger
from utils import (
    get_status_priority,
    parse_user_styled_time,
    resolve_timezone,
    broadcast_telegram,
    PipelineAbortError
)


def assemble_matches_feed(matches_cache: dict) -> list[dict]:
    """Builds and sorts the standardized matches array for the data website from matches_cache."""
    feed_list = []
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
        time_iso = raw_time
        if raw_time and "T" not in raw_time:
            try:
                dt = parse_user_styled_time(raw_time)
                if dt != datetime.min:
                    time_iso = dt.replace(tzinfo=resolve_timezone(None)).isoformat()
            except Exception:
                time_iso = raw_time

        duration = int(match.get("duration", 180))
        status_class = match.get("status_class", "not-started")
        is_ended = bool(match.get("is_ended", match.get("ended", status_class in ["finished", "manually-finished"])))
        link = "" if is_ended else (match.get("links", "") or match.get("link", ""))

        feed_list.append({
            "id": 0,
            "team1": {
                "nameAr": t1_ar or t1_en,
                "nameEn": t1_en or t1_ar,
                "img": match.get("team1_img", "")
            },
            "team2": {
                "nameAr": t2_ar or t2_en,
                "nameEn": t2_en or t2_ar,
                "img": match.get("team2_img", "")
            },
            "time": time_iso,
            "duration": duration,
            "link": link,
            "ended": is_ended,
            "_status_class": status_class
        })

    feed_list.sort(key=lambda m: (-get_status_priority(m["_status_class"]), m["time"]))
    for idx, item in enumerate(feed_list, start=1):
        item["id"] = idx
        item.pop("_status_class", None)

    return feed_list


def display_data_matches(active_matches_list: list):
    """Renders an aligned CLI preview table of matches formatted for the data website."""
    if not active_matches_list:
        return
    print()
    max_t1_len = max((len(m['team1'].get('nameEn') or m['team1']['nameAr']) for m in active_matches_list), default=15)
    max_t2_len = max((len(m['team2'].get('nameEn') or m['team2']['nameAr']) for m in active_matches_list), default=15)
    arrow = f"{logger.COLOR_DARK_GRAY}->{logger.COLOR_RESET}"

    for m in active_matches_list:
        t1 = m['team1'].get('nameEn') or m['team1']['nameAr']
        t2 = m['team2'].get('nameEn') or m['team2']['nameAr']
        aligned_teams = f"{t1:<{max_t1_len}} - {t2:<{max_t2_len}}"

        if m.get("ended"):
            status_tag = f"{logger.COLOR_DARK_GRAY}[ENDED]{logger.COLOR_RESET}"
        elif m.get("link"):
            status_tag = f"{logger.COLOR_GREEN}[LIVE]{logger.COLOR_RESET}"
        else:
            status_tag = f"{logger.COLOR_YELLOW}[UPCOMING]{logger.COLOR_RESET}"

        link_str = m.get('link', '')
        if len(link_str) > 42:
            link_str = link_str[:39] + "..."
        if not link_str:
            link_str = f"{logger.COLOR_DARK_GRAY}(no link){logger.COLOR_RESET}"

        print(f"  [{m['id']:2d}] {aligned_teams}  {arrow}  {status_tag}  Link: {link_str}")
    print()


def sync_data_page(
    blogger_session,
    blog_data_id: str,
    data_page_id: str,
    matches_cache: dict,
    skip_display: bool = False,
    active_matches_list: list = None
) -> bool:
    """Generates data website feed and patches the target Blogger page."""
    if active_matches_list is None:
        active_matches_list = assemble_matches_feed(matches_cache)
    if not skip_display:
        logger.info(f"Active matches formatted for the data website ({len(active_matches_list)} matches):")
        display_data_matches(active_matches_list)

    try:
        page_data = blogger_module.fetch_page(blogger_session, blog_data_id, data_page_id)
        page_content = page_data.get("content", "")
        patched_page_content = patcher.patch_matches_page(page_content, active_matches_list)

        if patched_page_content == page_content:
            skip_msg = f"{logger.COLOR_DARK_GRAY}Skipping update.{logger.COLOR_RESET}"
            logger.success(f"Matches list is already up to date in the data website. {skip_msg}")
            return True

        logger.info("Updating events JSON on the data website...")
        blogger_module.update_page(blogger_session, blog_data_id, data_page_id, patched_page_content)
        logger.success("Matches list page successfully updated.")
        return True
    except Exception as ex:
        logger.error(f"Failed to update data website page: {ex}")
        return False


def send_reconciliation_report(actions: list, telegram_token: str, send_report_chat_ids: list):
    """Sends a summary report of reconciliation actions via Telegram."""
    if not send_report_chat_ids or not telegram_token:
        return
    reconciliation_report = [f"• {act['message']}" for act in actions if act["action_type"] != "no_action"]
    report_text = ("Pipeline Updates Applied:\n" + "\n".join(reconciliation_report)) if reconciliation_report else "Pipeline check completed. No slot changes needed (everything up to date)."
    broadcast_telegram(telegram_token, send_report_chat_ids, report_text)


def _update_matches_cache_links(scraped_events: list, matches_cache: dict, updated_slots: list, public_posts_map: dict, blogger_session, blog_id: str):
    """Links active slots to matches in cache with permalinks and completion flags."""
    slot_by_event_id = {
        s["event_id"]: s for s in updated_slots
        if s.get("status", "").strip().lower() in ["valid", "active"] and s.get("event_id") and s.get("event_name", "").strip().lower() not in ["", "free"]
    }

    for ev in scraped_events:
        ev_id = ev["event_id"]
        if ev_id in matches_cache:
            permalink_url = ""
            if ev_id in slot_by_event_id:
                s = slot_by_event_id[ev_id]
                blog_post_id = s.get("blog_post_id", "")
                if blog_post_id and blog_post_id in public_posts_map:
                    permalink_url = public_posts_map[blog_post_id].get("url", "")
                elif blog_post_id:
                    try:
                        p_data = blogger_module.fetch_post(blogger_session, blog_id, blog_post_id)
                        public_posts_map[blog_post_id] = p_data
                        permalink_url = p_data.get("url", "")
                    except Exception:
                        pass

            matches_cache[ev_id]["links"] = permalink_url
            matches_cache[ev_id]["is_ended"] = ev.get("status_class") in ["finished", "manually-finished"]


def run(
    sheets_client,
    blogger_session,
    all_changed_slots: list,
    slots: list,
    scraped_events: list,
    matches_cache: dict,
    public_posts_map: dict,
    slot_actions: list,
    spreadsheet_name: str,
    blog_id: str,
    blog_data_id: str,
    data_page_id: str,
    telegram_token: str = "",
    send_report_chat_ids: list = None
):
    """
    Step 6: Syncs changed slots to Google Sheets, updates match cache, patches Data Website, and sends report.
    """
    if send_report_chat_ids is None:
        send_report_chat_ids = []

    try:
        if all_changed_slots:
            sheets_module.update_changed_slots(sheets_client, all_changed_slots, spreadsheet_name)
        else:
            logger.info("Google Sheets is already up to date. (0 slots changed)")

        updated_slots = sheets_module.fetch_all_slots(sheets_client, spreadsheet_name)
        _update_matches_cache_links(scraped_events, matches_cache, updated_slots, public_posts_map, blogger_session, blog_id)

        active_matches_list = assemble_matches_feed(matches_cache)
        logger.info(f"Active matches formatted for the data website ({len(active_matches_list)} matches):")
        display_data_matches(active_matches_list)

        sheets_module.save_matches_cache(sheets_client, matches_cache, spreadsheet_name)

        sync_data_page(blogger_session, blog_data_id, data_page_id, matches_cache, skip_display=True, active_matches_list=active_matches_list)
        send_reconciliation_report(slot_actions, telegram_token, send_report_chat_ids)
    except Exception as e:
        raise PipelineAbortError("DATABASE SYNC OR DATA PAGE UPDATE FAILED", f"Error syncing database or data page: {e}")
