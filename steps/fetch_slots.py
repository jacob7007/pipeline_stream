import sheets_module
import logger
from utils import get_slot_label, PipelineAbortError


def _display_slot_rows(slots: list):
    """Renders formatted table of stream slot rows."""
    if not slots:
        return
    print()
    max_slot_len = max((len(get_slot_label(s)) for s in slots), default=8)

    for s in slots:
        slot_name = get_slot_label(s)
        raw_status = (s.get("status") or "valid").strip().lower()
        status = "valid" if raw_status in ["valid", "active", "free"] else "invalid"
        ev_name = s.get("event_name", "").strip() or ("free" if status == "valid" else "(none)")

        status_color = logger.COLOR_GREEN if status == "valid" else logger.COLOR_RED

        if ev_name == "free":
            ev_styled = f"{logger.COLOR_DARK_GRAY}free{logger.COLOR_RESET}"
        elif ev_name == "(none)":
            ev_styled = f"{logger.COLOR_DARK_GRAY}(none){logger.COLOR_RESET}"
        else:
            ev_styled = ev_name

        status_styled = f"{status_color}{status:<7}{logger.COLOR_RESET}"
        print(f"  {slot_name:<{max_slot_len}}  |  Status: {status_styled}  |  Event: {ev_styled}")


def run(sheets_client, spreadsheet_name: str) -> list[dict]:
    """
    Step 2: Fetches all stream slots from Google Sheets and displays their current state.
    Returns the list of slot dictionaries.
    """
    try:
        slots = sheets_module.fetch_all_slots(sheets_client, spreadsheet_name)
        logger.info(f"Found {len(slots)} stream slots in '{spreadsheet_name}':")
        _display_slot_rows(slots)
        return slots
    except Exception as e:
        raise PipelineAbortError(
            "FAILED TO FETCH STREAM SLOTS",
            f"Error reading slots from '{spreadsheet_name}': {e}"
        )
