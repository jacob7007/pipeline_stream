import os
import json
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import logger
from utils import format_to_human_time, parse_user_styled_time, get_now_local, resolve_timezone, is_match_expired

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SLOT_COLUMNS = [
    "slot",
    "channel_post_id",
    "blog_post_id",
    "event_id",
    "event_name",
    "status",
    "kickoff_time",
    "last_updated"
]

COLUMNS = SLOT_COLUMNS

MATCHES_CACHE_COLUMNS = [
    "event_id",
    "team1_en",
    "team2_en",
    "team1_ar",
    "team2_ar",
    "link",
    "channels",
    "kickoff_time",
    "duration",
    "status_class",
    "last_updated",
    "team1_img",
    "team2_img"
]


def open_spreadsheet(client, spreadsheet_name: str):
    """Opens a spreadsheet by name, falling back to opening by key/ID."""
    try:
        return client.open(spreadsheet_name)
    except gspread.exceptions.SpreadsheetNotFound:
        try:
            return client.open_by_key(spreadsheet_name)
        except gspread.exceptions.SpreadsheetNotFound:
            raise ValueError(f"Spreadsheet '{spreadsheet_name}' not found by name or ID.")


def check_sheets_status(client: gspread.Client, spreadsheet_name: str) -> tuple[bool, str]:
    """
    Verifies that the Google Sheets spreadsheet exists and is accessible.
    Returns a tuple (is_accessible, error_message).
    """
    try:
        sh = open_spreadsheet(client, spreadsheet_name)
        _ = sh.title
        return True, ""
    except ValueError as ve:
        return False, str(ve)
    except Exception as e:
        return False, str(e)



def get_gspread_client() -> gspread.Client:
    """Authenticates and returns a gspread client using environment variables."""
    sa_json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json_str:
        try:
            info = json.loads(sa_json_str)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
            return gspread.authorize(creds)
        except Exception as e:
            raise RuntimeError(f"Failed to load credentials from GOOGLE_SERVICE_ACCOUNT_JSON env var: {e}")

    sa_file_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if sa_file_path and os.path.exists(sa_file_path):
        creds = Credentials.from_service_account_file(sa_file_path, scopes=SCOPES)
        return gspread.authorize(creds)

    raise ValueError(
        "Google Service Account credentials not found in environment variables. "
        "Please set GOOGLE_SERVICE_ACCOUNT_JSON in .env."
    )


def _get_slots_worksheet(sh, preferred_name: str = None):
    """Finds the slots worksheet (_cache_slots or first sheet)."""
    target = preferred_name if preferred_name else "_cache_slots"
    try:
        return sh.worksheet(target), target
    except gspread.exceptions.WorksheetNotFound:
        return sh.sheet1, sh.sheet1.title


def fetch_all_slots(client: gspread.Client, spreadsheet_name: str = "Streaming Dashboard", worksheet_name: str = None) -> list:
    """Fetches all slot rows from the slots worksheet."""
    sh = open_spreadsheet(client, spreadsheet_name)
    worksheet, _ = _get_slots_worksheet(sh, worksheet_name)

    all_values = worksheet.get_all_values()
    if not all_values:
        return []

    headers = [h.strip() for h in all_values[0]]
    rows = all_values[1:]

    slots = []
    for idx, row in enumerate(rows, start=2):
        padded_row = row + [""] * (len(headers) - len(row))
        row_dict = {"row_num": idx}
        for h_idx, header in enumerate(headers):
            key = header.strip().lower()
            val = padded_row[h_idx].strip()
            row_dict[key] = val
            if key in ["blog", "slot"]:
                row_dict["slot"] = val
            elif key in ["post_id", "blog_post_id", "blog_id"]:
                row_dict["blog_post_id"] = val
            elif key in ["channel_post_id", "channel_id", "player_post_id"]:
                row_dict["channel_post_id"] = val
        slots.append(row_dict)

    return slots


def _build_slot_update_range(slot: dict, headers: list, header_indices: dict, now_local_str: str) -> dict | None:
    """Constructs a single row update payload for a changed slot."""
    row_num = slot.get("row_num")
    if not row_num:
        return None

    slot["last_updated"] = now_local_str
    row_data = [""] * len(headers)
    for key, value in slot.items():
        if key == "row_num":
            continue
        key_norm = key.lower()
        if key_norm in header_indices:
            row_data[header_indices[key_norm] - 1] = str(value)

    range_name = f"A{row_num}:{gspread.utils.rowcol_to_a1(row_num, len(headers))}"
    return {"range": range_name, "values": [row_data]}


def update_changed_slots(client: gspread.Client, changed_slots: list, spreadsheet_name: str = "Streaming Dashboard", worksheet_name: str = None):
    """Updates the Google Sheet for rows that have changed in the slots worksheet."""
    if not changed_slots:
        return

    sh = open_spreadsheet(client, spreadsheet_name)
    worksheet, target_sheet_name = _get_slots_worksheet(sh, worksheet_name)

    headers = [h.strip() for h in worksheet.row_values(1)]
    header_indices = {header.lower(): idx for idx, header in enumerate(headers, start=1)}
    now_local = get_now_local()
    now_local_str = format_to_human_time(now_local.replace(tzinfo=resolve_timezone(None)).isoformat())

    body = []
    for slot in changed_slots:
        entry = _build_slot_update_range(slot, headers, header_indices, now_local_str)
        if entry:
            body.append(entry)

    if body:
        worksheet.batch_update(body)
        count = len(body)
        logger.success(f"Updated {count} slot{'s' if count != 1 else ''} on Google Sheets.")


def fetch_matches_cache(client, spreadsheet_name: str = "Streaming Dashboard") -> dict:
    """Fetches matches cache from '_cache_matches' worksheet, supporting 13-column and legacy schemas."""
    sh = open_spreadsheet(client, spreadsheet_name)
    sheet_name = "_cache_matches"
    try:
        worksheet = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="13")
        worksheet.append_row(MATCHES_CACHE_COLUMNS)
        return {}

    all_values = worksheet.get_all_values()
    if not all_values or len(all_values) <= 1:
        return {}

    headers = [h.strip().lower() for h in all_values[0]]
    header_map = {h: idx for idx, h in enumerate(headers)}

    matches_cache = {}
    for row in all_values[1:]:
        padded_row = row + [""] * (len(headers) - len(row))
        event_id = padded_row[header_map.get("event_id", 0)].strip() if "event_id" in header_map else ""
        if not event_id and "event_name" in header_map:
            # Legacy fallback: event_id might be column 1
            event_id = padded_row[header_map.get("event_id", 1)].strip()
        if not event_id:
            continue

        team1_en = padded_row[header_map["team1_en"]].strip() if "team1_en" in header_map else ""
        team1_ar = padded_row[header_map["team1_ar"]].strip() if "team1_ar" in header_map else ""
        team2_en = padded_row[header_map["team2_en"]].strip() if "team2_en" in header_map else ""
        team2_ar = padded_row[header_map["team2_ar"]].strip() if "team2_ar" in header_map else ""
        team1_img = padded_row[header_map["team1_img"]].strip() if "team1_img" in header_map else ""
        team2_img = padded_row[header_map["team2_img"]].strip() if "team2_img" in header_map else ""
        link = padded_row[header_map["link"]].strip() if "link" in header_map else ""
        channels = padded_row[header_map["channels"]].strip() if "channels" in header_map else ""
        k_time = padded_row[header_map["kickoff_time"]].strip() if "kickoff_time" in header_map else ""

        duration_raw = padded_row[header_map["duration"]].strip() if "duration" in header_map else "180"
        try:
            duration = int(duration_raw) if duration_raw else 180
        except ValueError:
            duration = 180

        s_class = padded_row[header_map["status_class"]].strip().lower() if "status_class" in header_map else "upcoming"
        if s_class not in ["live", "upcoming", "finished"]:
            s_class = "upcoming"

        l_updated = padded_row[header_map["last_updated"]].strip() if "last_updated" in header_map else ""

        # Legacy fallback if team names not in dedicated columns
        if not team1_en and not team1_ar and "event_name" in header_map:
            ev_name = padded_row[header_map["event_name"]].strip()
            if " vs " in ev_name:
                parts = ev_name.split(" vs ", 1)
                team1_en = parts[0].strip()
                team2_en = parts[1].strip()
        else:
            t1_display = team1_en or team1_ar
            t2_display = team2_en or team2_ar
            ev_name = f"{t1_display} vs {t2_display}" if (t1_display and t2_display) else event_id

        matches_cache[event_id] = {
            "event_id": event_id,
            "event_name": ev_name,
            "team1_en": team1_en,
            "team1_ar": team1_ar,
            "team2_en": team2_en,
            "team2_ar": team2_ar,
            "team1_img": team1_img,
            "team2_img": team2_img,
            "link": link,
            "channels": channels,
            "kickoff_time": k_time,
            "duration": duration,
            "status_class": s_class,
            "last_updated": l_updated,
        }
    return matches_cache


def _filter_valid_cache_rows(matches_cache: dict, now: datetime, now_local_str: str) -> list:
    """Filters out matches expired 3 hours after scheduled end and formats 13-column rows for cache sheet."""
    valid_cache_rows = []
    for event_id, data in matches_cache.items():
        k_time = data.get("kickoff_time", "")
        duration = int(data.get("duration", 180))

        # Check if match has passed its 3-hour post-game retention window
        if is_match_expired(k_time, duration, now, grace_minutes=180):
            continue

        last_updated_str = data.get("last_updated", "")
        out_time_str = now_local_str
        if last_updated_str:
            try:
                dt = parse_user_styled_time(last_updated_str)
                out_time_str = format_to_human_time(dt.isoformat()) if dt != datetime.min else now_local_str
            except Exception as e:
                logger.warning(f"Sheets: Failed to parse cache time '{last_updated_str}': {e}")
                out_time_str = now_local_str

        status_class = data.get("status_class", "upcoming").strip().lower()
        if status_class not in ["live", "upcoming", "finished"]:
            status_class = "upcoming"

        row = [
            data.get("event_id", event_id),
            data.get("team1_en", ""),
            data.get("team2_en", ""),
            data.get("team1_ar", ""),
            data.get("team2_ar", ""),
            data.get("link", ""),
            data.get("channels", ""),
            format_to_human_time(str(data.get("kickoff_time", ""))),
            duration,
            status_class,
            out_time_str,
            data.get("team1_img", ""),
            data.get("team2_img", "")
        ]
        valid_cache_rows.append(row)

    return valid_cache_rows


def save_matches_cache(client, matches_cache: dict, spreadsheet_name: str = "Streaming Dashboard") -> bool:
    """Clears and updates the '_cache_matches' worksheet with current cache entries if changed."""
    sh = open_spreadsheet(client, spreadsheet_name)
    sheet_name = "_cache_matches"
    try:
        worksheet = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="13")

    now = get_now_local()
    now_local_str = format_to_human_time(now.replace(tzinfo=resolve_timezone(None)).isoformat())

    # Purge expired matches in-place from the matches_cache dictionary
    expired_ids = [
        ev_id for ev_id, data in matches_cache.items()
        if is_match_expired(data.get("kickoff_time", ""), int(data.get("duration", 180)), now, grace_minutes=180)
    ]
    for ev_id in expired_ids:
        del matches_cache[ev_id]

    valid_cache_rows = _filter_valid_cache_rows(matches_cache, now, now_local_str)

    # Smart change detection: compare existing data cells with new rows (ignoring timestamp column at index 10)
    try:
        all_values = worksheet.get_all_values()
        if all_values and len(all_values) > 1:
            headers = [h.strip().lower() for h in all_values[0]]
            header_map = {h: idx for idx, h in enumerate(headers)}
            ev_idx = header_map.get("event_id", 0)
            existing_event_ids = {row[ev_idx].strip() for row in all_values[1:] if len(row) > ev_idx and row[ev_idx].strip()}
            current_event_ids = {r[0] for r in valid_cache_rows}
            removed_ids = existing_event_ids - current_event_ids
            if removed_ids:
                count = len(removed_ids)
                logger.info(f"Cache: Remove {count} expired match{'es' if count != 1 else ''} from cache.")

            existing_rows_data = [
                [str(cell).strip() for idx, cell in enumerate(row) if idx != 10]
                for row in all_values[1:]
                if any(str(cell).strip() for cell in row)
            ]
            new_rows_data = [
                [str(cell).strip() for idx, cell in enumerate(row) if idx != 10]
                for row in valid_cache_rows
            ]
            if existing_rows_data == new_rows_data:
                skip_msg = f"{logger.COLOR_DARK_GRAY}Skipping update.{logger.COLOR_RESET}"
                logger.success(f"Matches list is already up to date in the dashboard. {skip_msg}")
                return True
    except Exception as e:
        logger.warning(f"Sheets: Could not compare cache differences: {e}")

    worksheet.clear()
    worksheet.update([MATCHES_CACHE_COLUMNS] + valid_cache_rows)
    logger.success(f"Sheets: Saved {len(valid_cache_rows)} matches cache to Google Sheets.")
    return True


DOMAIN_CACHE_COLUMNS = ["domain", "status", "failure_reason", "last_tested"]

_DOMAIN_CACHE_SHEET = "_cache_domains"

_PRE_SEEDED_DOMAINS = {
    "youtube.com": {"status": "OK", "failure_reason": "--", "last_tested": "pre-seeded"},
    "youtu.be":    {"status": "OK", "failure_reason": "--", "last_tested": "pre-seeded"},
    "ok.ru":       {"status": "OK", "failure_reason": "--", "last_tested": "pre-seeded"},
    "yasirtv.com": {"status": "OK", "failure_reason": "--", "last_tested": "pre-seeded"},
}


def domain_to_sheet_format(domain: str) -> str:
    """Replaces '.' with '_' so Google Sheets does not detect domains as clickable links or flag them."""
    return domain.strip().lower().replace(".", "_")


def domain_from_sheet_format(raw_domain: str) -> str:
    """Converts domain from sheet format back to standard dot notation."""
    return raw_domain.strip().lower().replace("_", ".")


def load_domain_cache(client: gspread.Client, spreadsheet_name: str = "Streaming Dashboard") -> dict:
    """Loads the domain validation cache from the '_cache_domains' worksheet.

    Returns a dict keyed by registered domain:
        {
            "embed1.top":      {"status": "OK",  "failure_reason": "--",               "last_tested": "30 Aug - 16:00"},
            "merithotdog.net": {"status": "NO",  "failure_reason": "SANDBOX_REJECTED", "last_tested": "..."},
            "somesite.com":    {"status": "--",  "failure_reason": "TIMEOUT",          "last_tested": "..."},
        }
    If duplicate rows exist for the same domain, the last row wins.
    If the sheet is empty or newly created, it auto-seeds known-good domains.
    """
    sh = open_spreadsheet(client, spreadsheet_name)
    try:
        worksheet = sh.worksheet(_DOMAIN_CACHE_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=_DOMAIN_CACHE_SHEET, rows="500", cols="4")
        worksheet.append_row(DOMAIN_CACHE_COLUMNS)
        logger.info(f"Sheets: Created '{_DOMAIN_CACHE_SHEET}' worksheet with headers.")

    all_values = worksheet.get_all_values()
    if not all_values or len(all_values) <= 1:
        rows = [
            [domain_to_sheet_format(d), data["status"], data["failure_reason"], data["last_tested"]]
            for d, data in sorted(_PRE_SEEDED_DOMAINS.items())
        ]
        try:
            worksheet.clear()
            worksheet.update([DOMAIN_CACHE_COLUMNS] + rows)
            logger.info(f"Sheets: Auto-seeded '{_DOMAIN_CACHE_SHEET}' with {len(rows)} trusted domains.")
        except Exception as ex:
            logger.warning(f"Sheets: Failed to auto-seed '{_DOMAIN_CACHE_SHEET}': {ex}")
        return dict(_PRE_SEEDED_DOMAINS)

    headers = [h.strip().lower() for h in all_values[0]]
    header_map = {h: idx for idx, h in enumerate(headers)}

    cache = {}
    for row in all_values[1:]:
        padded = row + [""] * (len(headers) - len(row))
        raw_domain = padded[header_map.get("domain", 0)].strip().lower()
        if not raw_domain:
            continue
        domain = domain_from_sheet_format(raw_domain)
        cache[domain] = {
            "status":         padded[header_map.get("status", 1)].strip(),
            "failure_reason": padded[header_map.get("failure_reason", 2)].strip() or "--",
            "last_tested":    padded[header_map.get("last_tested", 3)].strip(),
        }

    logger.success(f"Sheets: Loaded {len(cache)} domain cache entries from '{_DOMAIN_CACHE_SHEET}'.")
    return cache


def save_domain_cache(client: gspread.Client, cache: dict, spreadsheet_name: str = "Streaming Dashboard") -> None:
    """Rewrites the '_cache_domains' worksheet with one row per domain, sorted alphabetically.

    Domains are saved replacing '.' with '_' (e.g. 'youtube_com', 'evemeverbee_info_pl') so that
    Google Sheets does not detect them as clickable links or flag them as suspicious.
    Eliminates any duplicate rows that may have accumulated from manual editing.
    Only called when the in-memory cache has been modified during the current pipeline run.
    """
    sh = open_spreadsheet(client, spreadsheet_name)
    try:
        worksheet = sh.worksheet(_DOMAIN_CACHE_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=_DOMAIN_CACHE_SHEET, rows="500", cols="4")

    rows = []
    for domain in sorted(cache.keys()):
        entry = cache[domain]
        sheet_domain = domain_to_sheet_format(domain)
        rows.append([
            sheet_domain,
            entry.get("status", "--"),
            entry.get("failure_reason", "--"),
            entry.get("last_tested", "--"),
        ])

    worksheet.clear()
    worksheet.update([DOMAIN_CACHE_COLUMNS] + rows)
    logger.success(f"Sheets: Saved {len(rows)} domain entries to '{_DOMAIN_CACHE_SHEET}'.")
