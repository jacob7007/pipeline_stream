from utils import get_slot_label, get_event_display_name


def _get_slot_identifier(slot: dict) -> str:
    """Returns a unique hashable identifier for a slot dict."""
    return str(slot.get("slot") or slot.get("row_num") or id(slot))


def _categorize_sheet_slots(sheet_slots: list, scraped_map: dict) -> tuple:
    """Categorizes sheet slots into active matched, to-be-freed, and already-free lists."""
    active_matched = []
    to_be_freed = []
    already_free = []

    for slot in sheet_slots:
        ev_id = slot.get("event_id", "").strip()
        ev_name = slot.get("event_name", "").strip().lower()
        status = slot.get("status", "").strip().lower()

        if status not in ["invalid", "broken", "deleted"]:
            # Slot is actively assigned to an event
            if ev_id and ev_name not in ["", "free"]:
                if ev_id in scraped_map:
                    active_matched.append(slot)
                else:
                    to_be_freed.append(slot)
            else:
                already_free.append(slot)

    return active_matched, to_be_freed, already_free



def _assign_unassigned_events(
    unassigned_events: list,
    free_slots_queue: list,
    to_be_freed: list,
    actions: list,
    reassigned_slots: set
):
    """Assigns unassigned scraped events to free slots."""
    for event in unassigned_events:
        if not free_slots_queue:
            break
        event_name = get_event_display_name(event)
        slot = free_slots_queue.pop(0)
        slot_label = get_slot_label(slot)
        slot_id = _get_slot_identifier(slot)
        if slot in to_be_freed:
            reassigned_slots.add(slot_id)

        actions.append({
            "action_type": "assign_new",
            "slot": slot,
            "blog": slot,
            "event": event,
            "message": f"Assign new event '{event_name}' to {slot_label}"
        })


def _free_unmatched_slots(to_be_freed: list, reassigned_slots: set, actions: list):
    """Generates actions to free slots whose assigned events disappeared or were displaced."""
    for slot in to_be_freed:
        slot_id = _get_slot_identifier(slot)
        if slot_id not in reassigned_slots:
            slot_label = get_slot_label(slot)
            event_name = slot.get("event_name", "").strip() or "match"
            actions.append({
                "action_type": "free_slot",
                "slot": slot,
                "blog": slot,
                "event": None,
                "message": f"Freeing {slot_label} (event '{event_name}' disappeared or ended)"
            })


def _evaluate_matched_slots(active_matched: list, scraped_map: dict, actions: list):
    """Checks if matched active slots require stream channel updates or metadata sync."""
    for slot in active_matched:
        event = scraped_map[slot["event_id"]]
        event_name = get_event_display_name(event)
        slot_label = get_slot_label(slot)

        actions.append({
            "action_type": "sync_channels",
            "slot": slot,
            "event": event,
            "message": f"Checking/syncing stream channels for {slot_label} ('{event_name}')"
        })


def reconcile_state(sheet_slots: list, scraped_events: list) -> list:
    """
    Compares the Google Sheet slot states with the scraped live events.
    Limits candidate events to the available slot capacity, excluding finished matches.
    Returns a list of action dicts describing what updates to make to the sheet and Blogger.
    """
    if not sheet_slots:
        return []

    # Only active/upcoming matches should occupy stream slots
    active_candidates = [
        e for e in scraped_events
        if e.get("status_class") != "finished"
    ]

    # Limit candidate events strictly to available physical slot capacity
    target_events = active_candidates[:len(sheet_slots)]
    scraped_map = {e["event_id"]: e for e in target_events}
    active_matched, to_be_freed, already_free = _categorize_sheet_slots(sheet_slots, scraped_map)

    free_slots_queue = already_free + to_be_freed
    unassigned_events = [e for e in target_events if e["event_id"] not in [b["event_id"] for b in active_matched]]
    reassigned_slots = set()
    actions = []

    _assign_unassigned_events(unassigned_events, free_slots_queue, to_be_freed, actions, reassigned_slots)
    _free_unmatched_slots(to_be_freed, reassigned_slots, actions)
    _evaluate_matched_slots(active_matched, scraped_map, actions)

    return actions


