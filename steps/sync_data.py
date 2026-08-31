import json
import base64
from urllib.parse import unquote
from datetime import datetime
import sheets_module
import blogger_module
import patcher
import logger
from normalization import are_english_teams_equivalent, are_arabic_names_equivalent
from utils import (
    get_status_priority,
    parse_user_styled_time,
    parse_iso_time,
    format_to_human_time,
    resolve_timezone,
    broadcast_telegram,
    get_now_local,
    is_match_expired,
    PLACEHOLDER_IMAGE_URL,
    PipelineAbortError
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
    """Builds and sorts the standardized matches array for the data website from matches_cache."""
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
        duration = int(match.get("duration", 180))

        # Do not include expired matches (> 3 hours post-match TTL) on the data website
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
        link = "" if is_ended else match.get("link", "")

        feed_list.append({
            "id": 0,
            "team1": {
                "nameAr": t1_ar or t1_en,
                "nameEn": t1_en or t1_ar,
                "img": match.get("team1_img", "").strip() or PLACEHOLDER_IMAGE_URL
            },
            "team2": {
                "nameAr": t2_ar or t2_en,
                "nameEn": t2_en or t2_ar,
                "img": match.get("team2_img", "").strip() or PLACEHOLDER_IMAGE_URL
            },
            "time": time_iso,
            "duration": duration,
            "link": link,
            "ended": is_ended,
            "_status_class": status_class,
            "_channels_count": _get_channels_count(match.get("channels", "")),
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
                    if item.get("_channels_count", 0) > exist.get("_channels_count", 0):
                        exist["_channels_count"] = item["_channels_count"]
                    break
        if not is_dup:
            deduped_feed.append(item)

    deduped_feed.sort(key=lambda m: (-get_status_priority(m["_status_class"]), m["time"]))
    for idx, item in enumerate(deduped_feed, start=1):
        item["id"] = idx

    return deduped_feed


def display_data_matches(active_matches_list: list):
    """Renders an aligned CLI preview table of matches formatted for the data website."""
    if not active_matches_list:
        return
    print()
    max_t1_len = max((len(m['team1'].get('nameEn') or m['team1']['nameAr']) for m in active_matches_list), default=15)
    max_t2_len = max((len(m['team2'].get('nameEn') or m['team2']['nameAr']) for m in active_matches_list), default=15)
    max_date_len = max((len(format_to_human_time(m.get('time', ''))) for m in active_matches_list), default=14)
    max_status_len = 8

    ch_prefixes = []
    for m in active_matches_list:
        if m.get("link"):
            cnt = m.get("_channels_count", 0)
            ch_prefixes.append(f"{cnt} Channels: " if cnt != 1 else f"{cnt} Channel: ")
    max_ch_prefix_len = max((len(p) for p in ch_prefixes), default=13)

    for m in active_matches_list:
        t1 = m['team1'].get('nameEn') or m['team1']['nameAr']
        t2 = m['team2'].get('nameEn') or m['team2']['nameAr']
        aligned_teams = f"{t1:<{max_t1_len}} - {t2:<{max_t2_len}}"
        date_str = format_to_human_time(m.get('time', ''))
        aligned_date = f"{date_str:<{max_date_len}}"

        status_val = m.get("_status_class", "upcoming")
        if status_val == "finished" or m.get("ended"):
            status = "FINISHED"
            status_styled = f"{logger.COLOR_DARK_GRAY}{status:<{max_status_len}}{logger.COLOR_RESET}"
        elif m.get("link"):
            status = "LIVE"
            status_styled = f"{logger.COLOR_GREEN}{logger.COLOR_BOLD}{status:<{max_status_len}}{logger.COLOR_RESET}"
        else:
            status = "UPCOMING"
            status_styled = f"{logger.COLOR_YELLOW}{status:<{max_status_len}}{logger.COLOR_RESET}"

        link = m.get('link', '').strip()
        if link:
            cnt = m.get('_channels_count', 0)
            prefix = f"{cnt} Channels: " if cnt != 1 else f"{cnt} Channel: "
            disp_url = link[:20] + "..." if len(link) > 20 else link
            clickable = f"\033]8;;{link}\033\\\033[4m\033[94m{disp_url}\033[0m\033]8;;\033\\"
            stream_part = f"{prefix:<{max_ch_prefix_len}}{clickable}"
        else:
            stream_part = f"{logger.COLOR_DARK_GRAY}(No link){logger.COLOR_RESET}"

        print(f"  [{m['id']:2d}] {aligned_teams}  |  {aligned_date}  |  {status_styled}  |  {stream_part}")
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
        clean_matches = [
            {k: v for k, v in m.items() if not k.startswith("_")}
            for m in active_matches_list
        ]
        patched_page_content = patcher.patch_matches_page(page_content, clean_matches)

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

    scraped_by_event_id = {ev["event_id"]: ev for ev in scraped_events if ev.get("event_id")}

    for ev_id, cached_match in matches_cache.items():
        if ev_id in scraped_by_event_id:
            ev = scraped_by_event_id[ev_id]
            cached_match["status_class"] = ev.get("status_class", cached_match.get("status_class", "upcoming"))
            if ev.get("channels"):
                cached_match["channels"] = patcher.encode_channels_payload(ev["channels"])

        permalink_url = ""
        if ev_id in slot_by_event_id and cached_match.get("status_class") != "finished":
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

        cached_match["link"] = permalink_url


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

        sheets_module.save_matches_cache(sheets_client, matches_cache, spreadsheet_name)

        active_matches_list = assemble_matches_feed(matches_cache)
        logger.info(f"Active matches formatted for the data website ({len(active_matches_list)} matches):")
        display_data_matches(active_matches_list)

        sync_data_page(blogger_session, blog_data_id, data_page_id, matches_cache, skip_display=True, active_matches_list=active_matches_list)
        send_reconciliation_report(slot_actions, telegram_token, send_report_chat_ids)
    except Exception as e:
        raise PipelineAbortError("DATABASE SYNC OR DATA PAGE UPDATE FAILED", f"Error syncing database or data page: {e}")
