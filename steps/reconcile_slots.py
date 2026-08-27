import reconciler
import blogger_module
import patcher
import logger
from utils import get_slot_label, get_event_display_name, format_to_human_time, PipelineAbortError


def _patch_single_player_channel(slot: dict, event: dict, post_id: str, blogger_session, posts_map: dict, blog_player_id: str) -> str:
    """Fetches, patches the obfuscated payload, and updates a Player Blog post (hd1/hd2). Returns post permalink."""
    slot_name = get_slot_label(slot)
    post_data = posts_map.get(post_id)
    if not post_data:
        try:
            post_data = blogger_module.fetch_post(blogger_session, blog_player_id, post_id)
            posts_map[post_id] = post_data
        except Exception as ex:
            logger.error(f"Error fetching player post for {slot_name}: {ex}")
            return ""

    current_content = post_data.get("content", "")
    channel_streams = event.get("channels", [])
    ch_count = len(channel_streams)
    ch_text = f"{ch_count} live channel{'s' if ch_count != 1 else ''}"
    try:
        patched_content = patcher.patch_player_payload(current_content, channel_streams)
        logger.info(f"Updating stream player for {slot_name} ({ch_text})...")
        blogger_module.update_post(blogger_session, blog_player_id, post_id, patched_content)
        logger.success(f"Successfully updated player for {slot_name}.")
        print()
        post_data["content"] = patched_content
        return post_data.get("url", "")
    except Exception as ex:
        logger.error(f"Error updating player for {slot_name}: {ex}")
        return post_data.get("url", "")


def _patch_single_blog_post(slot: dict, iframe_src: str, post_id: str, blogger_session, posts_map: dict, blog_id: str) -> bool:
    """Fetches, patches the iframe, and updates a Public Blog post on Blogger."""
    slot_name = get_slot_label(slot)
    post_data = posts_map.get(post_id)
    if not post_data:
        try:
            post_data = blogger_module.fetch_post(blogger_session, blog_id, post_id)
            posts_map[post_id] = post_data
        except Exception as ex:
            logger.error(f"Error fetching public post for {slot_name}: {ex}")
            return False

    current_content = post_data.get("content", "")
    try:
        patched_content = patcher.patch_blog_html(current_content, iframe_src)
        if patched_content == current_content:
            return True

        logger.info(f"Updating public iframe for {slot_name}... ")
        blogger_module.update_post(blogger_session, blog_id, post_id, patched_content)
        logger.success(f"Successfully updated public iframe for {slot_name}.")
        post_data["content"] = patched_content
        return True
    except Exception as ex:
        logger.error(f"Error updating public iframe for {slot_name}: {ex}")
        return False


def _append_invalid_actions(actions: list, invalid_slots: list, restored_slots: list):
    """Appends invalid and restored status actions to reconciliation action list for logging."""
    for slot in invalid_slots:
        slot_label = get_slot_label(slot)
        actions.append({
            "action_type": "mark_invalid",
            "slot": slot,
            "event": None,
            "message": f"Mark {slot_label} as invalid (missing or draft post IDs)"
        })
    for slot in restored_slots:
        slot_label = get_slot_label(slot)
        actions.append({
            "action_type": "restore_slot",
            "slot": slot,
            "event": None,
            "message": f"Restore {slot_label} to valid status (all posts now LIVE)"
        })


def execute_slot_updates(actions: list, blogger_session, player_posts_map: dict, public_posts_map: dict, blog_id: str, blog_player_id: str) -> tuple[list, int, int]:
    """Executes reconciliation actions across Player Channel and Public Blog posts."""
    changed_slots = []
    player_updates_count = 0
    public_updates_count = 0

    for act in actions:
        action_type = act["action_type"]
        slot = act.get("slot") or act.get("blog")
        event = act.get("event")
        if not slot or action_type in ["no_action", "mark_invalid"]:
            continue

        channel_post_id = slot.get("channel_post_id", "").strip()
        blog_post_id = slot.get("blog_post_id", "").strip()

        if action_type == "free_slot":
            slot.update({"event_id": "", "event_name": "free", "kickoff_time": "", "status": "valid"})
            changed_slots.append(slot)
        elif action_type == "update_sheet_only":
            slot.update({
                "event_name": get_event_display_name(event),
                "kickoff_time": format_to_human_time(event["time"]),
                "status": "valid"
            })
            changed_slots.append(slot)
        elif action_type == "sync_channels":
            if channel_post_id:
                old_content = player_posts_map.get(channel_post_id, {}).get("content", "")
                player_url = _patch_single_player_channel(slot, event, channel_post_id, blogger_session, player_posts_map, blog_player_id)
                new_content = player_posts_map.get(channel_post_id, {}).get("content", "")
                if old_content and new_content != old_content:
                    player_updates_count += 1

            sheet_name = slot.get("event_name", "").strip()
            sheet_kickoff = slot.get("kickoff_time", "").strip()
            expected_name = get_event_display_name(event)
            expected_kickoff = format_to_human_time(event["time"])
            if sheet_name != expected_name or sheet_kickoff != expected_kickoff:
                slot.update({
                    "event_name": expected_name,
                    "kickoff_time": expected_kickoff,
                    "status": "valid"
                })
                changed_slots.append(slot)
        elif action_type == "assign_new":
            ev_id = event["event_id"]
            player_url = ""
            if channel_post_id:
                player_url = _patch_single_player_channel(slot, event, channel_post_id, blogger_session, player_posts_map, blog_player_id)
                if player_url:
                    player_updates_count += 1

            slot.update({
                "event_id": ev_id,
                "event_name": get_event_display_name(event),
                "kickoff_time": format_to_human_time(event["time"]),
                "status": "valid"
            })
            changed_slots.append(slot)

            target_iframe = player_url or player_posts_map.get(channel_post_id, {}).get("url", "")
            if blog_post_id and target_iframe:
                if _patch_single_blog_post(slot, target_iframe, blog_post_id, blogger_session, public_posts_map, blog_id):
                    public_updates_count += 1

    return changed_slots, player_updates_count, public_updates_count


def _log_reconciliation_summary(slot_actions: list, restored_slots: list, all_changed_slots: list, player_updates_count: int):
    """Prints a human-readable summary of applied reconciliation updates."""
    assigned_count = sum(1 for a in slot_actions if a["action_type"] == "assign_new")
    freed_count = sum(1 for a in slot_actions if a["action_type"] == "free_slot")
    updated_count = sum(1 for a in slot_actions if a["action_type"] == "update_sheet_only") + len(all_changed_slots)
    restored_count = len(restored_slots)

    summary_parts = []
    if assigned_count:
        summary_parts.append(f"{assigned_count} slot{'s' if assigned_count != 1 else ''} assigned")
    if freed_count:
        summary_parts.append(f"{freed_count} slot{'s' if freed_count != 1 else ''} freed")
    if updated_count:
        summary_parts.append(f"{updated_count} slot{'s' if updated_count != 1 else ''} updated")
    if restored_count:
        summary_parts.append(f"{restored_count} slot{'s' if restored_count != 1 else ''} restored")

    if summary_parts:
        summary_str = ", ".join(summary_parts)
        logger.item(f"Stream slot reconciliation applied ({summary_str}).")
    elif all_changed_slots or player_updates_count > 0:
        count = len(all_changed_slots)
        logger.item(f"Stream slot reconciliation applied ({count} slot{'s' if count != 1 else ''} updated).")
    else:
        logger.item("All stream slots are up to date. (0 updates needed)")


def run(
    blogger_session,
    valid_slots: list,
    newly_invalid_slots: list,
    restored_slots: list,
    scraped_events: list,
    blog_id: str,
    blog_player_id: str
) -> tuple[list, list, dict]:
    """
    Step 5: Reconciles slots with active stream events and applies Blogger post/channel updates.
    Returns (all_changed_slots, slot_actions, public_posts_map).
    """
    try:
        active_stream_events = [e for e in scraped_events if e.get("channels") and e.get("status_class") in ["live", "not-started"]]
        slot_actions = reconciler.reconcile_state(valid_slots, active_stream_events)
        _append_invalid_actions(slot_actions, newly_invalid_slots, restored_slots)

        for act in slot_actions:
            logger.action(act["action_type"], act["message"])

        if slot_actions:
            print()

        player_posts_map = blogger_module.fetch_posts_map(blogger_session, blog_player_id, status="live,draft")
        public_posts_map = blogger_module.fetch_posts_map(blogger_session, blog_id, status="live,draft")

        changed_slots, player_updates_count, public_updates_count = execute_slot_updates(
            slot_actions, blogger_session, player_posts_map, public_posts_map, blog_id, blog_player_id
        )

        all_changed_slots = changed_slots + [s for s in (newly_invalid_slots + restored_slots) if s not in changed_slots]
        _log_reconciliation_summary(slot_actions, restored_slots, all_changed_slots, player_updates_count)

        return all_changed_slots, slot_actions, public_posts_map
    except Exception as e:
        raise PipelineAbortError("SLOT RECONCILIATION FAILED", f"Error during reconciliation: {e}")
