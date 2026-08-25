import blogger_module
import logger
from utils import get_slot_label, broadcast_telegram, PipelineAbortError


def _fetch_blogger_posts_status(blogger_session, blog_id: str, blog_label: str) -> dict:
    """Fetches all post IDs and their statuses from Blogger (both live and draft)."""
    logger.info(f"Fetching published post IDs from {blog_label}...")
    try:
        posts_resp = blogger_module.fetch_all_posts(blogger_session, blog_id, status="live,draft")
        posts_list = posts_resp.get("items", [])
        status_map = {p["id"]: p.get("status", "LIVE") or "LIVE" for p in posts_list}
        live_count = sum(1 for s in status_map.values() if s == "LIVE")
        logger.success(f"Found {live_count} published (LIVE) posts, ({len(status_map)} total).")
        print()
        return status_map
    except Exception as e:
        logger.error(f"Failed to fetch posts list from {blog_label}: {e}. Will verify posts individually.")
        print()
        return {}


def _check_post_live(blogger_session, blog_id: str, post_id: str, posts_status_map: dict, slot_name: str, post_type: str) -> bool:
    """Checks if a post ID is published and LIVE on Blogger."""
    if not post_id:
        return False
    if post_id in posts_status_map:
        status = posts_status_map[post_id]
        if status == "LIVE":
            return True
        logger.error(f"{slot_name.capitalize()} {post_type} post ID {post_id} is in status '{status}' (not LIVE).")
        return False

    if posts_status_map:
        logger.error(f"{slot_name.capitalize()} {post_type} post ID {post_id} was not found on {post_type} (deleted or invalid ID).")
        return False

    try:
        post_data = blogger_module.fetch_post(blogger_session, blog_id, post_id)
        post_status = post_data.get("status")
        if post_status in ["LIVE", None]:
            return True
        logger.error(f"{slot_name.capitalize()} {post_type} post ID {post_id} is in status '{post_status}' (not LIVE).")
        return False
    except Exception as ex:
        status_code = getattr(getattr(ex, 'response', None), 'status_code', 0)
        err_str = str(ex).lower()
        if status_code in [400, 403, 404, 410] or any(k in err_str for k in ["404", "410", "not found", "deleted"]):
            logger.error(f"{slot_name.capitalize()} {post_type} post ID {post_id} is NOT available on Blogger (HTTP {status_code}).")
            return False
        logger.warning(f"Temporary error verifying {slot_name} {post_type} post ID {post_id}: {ex}. Assuming working.")
        return True


def _mark_invalid_slot(slot: dict, slot_name: str, reason: str, telegram_token: str, allowed_chat_ids: list):
    """Marks a slot as invalid in memory and broadcasts an alert."""
    logger.error(f"ALERT: {slot_name.capitalize()} is INVALID: {reason}")
    slot.update({"status": "invalid", "event_id": "", "event_name": "", "kickoff_time": ""})
    alert_text = (
        f"🚨 Blogger Alert!\n\nSlot: {slot_name}\n"
        f"Blog Post ID: {slot.get('blog_post_id', 'MISSING')}\n"
        f"Channel Post ID: {slot.get('channel_post_id', 'MISSING')}\n"
        f"Reason: {reason}\n\n"
        f"Action: Slot marked as 'invalid' in Google Sheets and excluded from active streams."
    )
    if telegram_token and allowed_chat_ids:
        broadcast_telegram(telegram_token, allowed_chat_ids, alert_text)


def _validate_single_slot(
    slot: dict,
    blogger_session,
    blog_id: str,
    blog_player_id: str,
    pub_post_ids: dict,
    player_post_ids: dict,
    telegram_token: str,
    allowed_chat_ids: list
) -> tuple[str, dict]:
    """
    Validates a single slot's post IDs against Blogger.
    Returns ('valid' | 'invalid' | 'restored', slot).
    """
    slot_name = get_slot_label(slot)
    blog_post_id = slot.get("blog_post_id", "").strip()
    channel_post_id = slot.get("channel_post_id", "").strip()
    current_status = slot.get("status", "").strip().lower()

    if not blog_post_id or not channel_post_id:
        missing_item = "channel_post_id" if not channel_post_id else "blog_post_id"
        if not blog_post_id and not channel_post_id:
            missing_item = "both blog_post_id and channel_post_id"
        _mark_invalid_slot(slot, slot_name, f"Missing {missing_item}", telegram_token, allowed_chat_ids)
        return "invalid", slot

    is_blog_live = _check_post_live(blogger_session, blog_id, blog_post_id, pub_post_ids, slot_name, "Public Blog")
    is_channel_live = _check_post_live(blogger_session, blog_player_id, channel_post_id, player_post_ids, slot_name, "Player Channel")

    if is_blog_live and is_channel_live:
        if current_status in ["invalid", "broken", "deleted"]:
            logger.success(f"{slot_name.capitalize()} restored to valid status! (All posts are LIVE).")
            slot.update({"status": "valid", "event_id": "", "event_name": "free", "kickoff_time": ""})
            recovery_text = f"🎉 Slot Restored!\n\nSlot: {slot_name}\nStatus: Back online and set to 'valid'."
            if telegram_token and allowed_chat_ids:
                broadcast_telegram(telegram_token, allowed_chat_ids, recovery_text)
            return "restored", slot
        else:
            slot["status"] = "valid"
            return "valid", slot

    failed_parts = []
    if not is_blog_live:
        failed_parts.append("Public Blog post is in draft/deleted")
    if not is_channel_live:
        failed_parts.append("Player Channel post is in draft/deleted")
    reason_msg = " and ".join(failed_parts)
    _mark_invalid_slot(slot, slot_name, reason_msg, telegram_token, allowed_chat_ids)
    return "invalid", slot


def run(
    blogger_session,
    slots: list,
    blog_id: str,
    blog_player_id: str,
    telegram_token: str = "",
    allowed_chat_ids: list = None
) -> tuple[list, list, list]:
    """
    Step 3: Validates post IDs for each slot and filters valid, invalid, and restored slots.
    Returns (valid_slots, invalid_slots, restored_slots).
    """
    if allowed_chat_ids is None:
        allowed_chat_ids = []

    try:
        pub_post_ids = _fetch_blogger_posts_status(blogger_session, blog_id, "the blog website")
        player_post_ids = _fetch_blogger_posts_status(blogger_session, blog_player_id, "the player website")

        valid_slots = []
        invalid_slots = []
        restored_slots = []

        for slot in slots:
            outcome, updated_slot = _validate_single_slot(
                slot, blogger_session, blog_id, blog_player_id,
                pub_post_ids, player_post_ids, telegram_token, allowed_chat_ids
            )
            if outcome == "valid":
                valid_slots.append(updated_slot)
            elif outcome == "restored":
                valid_slots.append(updated_slot)
                restored_slots.append(updated_slot)
            else:
                invalid_slots.append(updated_slot)

        logger.item(f"{len(valid_slots)} slots valid.")
        logger.item(f"{len(invalid_slots)} invalid.")
        return valid_slots, invalid_slots, restored_slots
    except Exception as e:
        raise PipelineAbortError("SLOT VALIDATION FAILED", f"Error validating slots: {e}")
