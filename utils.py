import os
import sys
import re
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import logger

PLACEHOLDER_IMAGE_URL = "https://placehold.co/300x300?text=*"


def sanitize_sheet_image_url(url: str) -> str:
    """Returns empty string if URL is empty, whitespace, or equal to the fallback placeholder."""
    if not url:
        return ""
    u = str(url).strip()
    if u == PLACEHOLDER_IMAGE_URL:
        return ""
    return u


def get_default_timezone() -> ZoneInfo:
    """
    Returns the target application timezone as a ZoneInfo object.
    Configured via DEFAULT_TIMEZONE env var (defaults to 'Africa/Casablanca').
    """
    tz_name = os.environ.get("DEFAULT_TIMEZONE") or os.environ.get("default_timezone") or "Africa/Casablanca"
    try:
        return ZoneInfo(tz_name.strip())
    except Exception as e:
        logger.warning(f"Invalid DEFAULT_TIMEZONE '{tz_name}', falling back to 'Africa/Casablanca': {e}")
        return ZoneInfo("Africa/Casablanca")

def resolve_timezone(tz_val) -> timezone | ZoneInfo:
    """
    Converts an IANA timezone name (e.g. 'Asia/Riyadh', 'Africa/Cairo'),
    a numeric offset (e.g. 3, -4), offset string (e.g. '+01:00', '-05:00', '+3', 'GMT+3', 'UTC+3'),
    or None into a valid timezone object.
    """
    if tz_val is None or tz_val == "":
        return get_default_timezone()
    if isinstance(tz_val, (timezone, ZoneInfo)):
        return tz_val
    if isinstance(tz_val, (int, float)):
        return timezone(timedelta(hours=int(tz_val)))
    if isinstance(tz_val, str):
        tz_val = tz_val.strip()
        if tz_val.upper() in ("GMT", "UTC", "ETC/GMT", "ETC/UTC", "Z"):
            return timezone.utc

        # Handle GMT+3, UTC+3, GMT-5:30, UTC+03:00, etc.
        gmt_m = re.match(r'^(?:GMT|UTC)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$', tz_val, re.IGNORECASE)
        if gmt_m:
            sign = -1 if gmt_m.group(1) == '-' else 1
            hours = int(gmt_m.group(2))
            minutes = int(gmt_m.group(3) or 0)
            return timezone(sign * timedelta(hours=hours, minutes=minutes))

        # Handle offset string like "+01:00", "-05:30", "+0300", "+3", "-5"
        offset_m = re.match(r'^([+-])(\d{1,2})(?::?(\d{2}))?$', tz_val)
        if offset_m:
            sign = -1 if offset_m.group(1) == '-' else 1
            hours = int(offset_m.group(2))
            minutes = int(offset_m.group(3) or 0)
            return timezone(sign * timedelta(hours=hours, minutes=minutes))

        # Handle unsigned number string like "3", "-3"
        num_m = re.match(r'^-?\d+$', tz_val)
        if num_m:
            return timezone(timedelta(hours=int(tz_val)))

        try:
            return ZoneInfo(tz_val)
        except Exception:
            logger.warning(f"Unrecognized timezone '{tz_val}', falling back to default timezone.")
            return get_default_timezone()
    return get_default_timezone()

def load_env():
    """Load local .env file if it exists, without overwriting existing env vars."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val

def send_telegram_message(bot_token, chat_id, text):
    """Send a message via the Telegram Bot API. Returns True on success, False on failure."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            logger.error(f"Telegram: sendMessage failed for chat_id={chat_id} (HTTP {r.status_code}): {r.text}")
            return False
        return True
    except Exception as e:
        logger.error(f"Telegram: Failed to send message to chat_id={chat_id}: {e}")
        return False

def configure_utf8():
    """Reconfigure stdout/stderr to use UTF-8 for proper Arabic text display."""
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

def strip_timezone(time_str: str) -> str:
    """Strips timezone offset (e.g. +01:00, -05:00, Z) from an ISO time string."""
    if not time_str:
        return ""
    return re.sub(r'([+-]\d{2}:?\d{2}|Z)$', '', time_str.strip())

def format_to_human_time(time_input: str | datetime) -> str:
    """
    Converts ISO 8601 time string or datetime object to user format: "22 Aug - 19:45".
    """
    if not time_input:
        return ""
    if isinstance(time_input, datetime):
        return f"{time_input.day} {time_input.strftime('%b')} - {time_input.strftime('%H:%M')}"
    time_str = str(time_input).strip()
    try:
        dt = datetime.fromisoformat(time_str)
        return f"{dt.day} {dt.strftime('%b')} - {dt.strftime('%H:%M')}"
    except Exception:
        return time_str

def get_now_local() -> datetime:
    """Returns the current time in DEFAULT_TIMEZONE as a naive datetime."""
    return datetime.now(get_default_timezone()).replace(tzinfo=None)

def parse_iso_time(time_str: str) -> datetime:
    """
    Parses ISO time string, falling back to datetime.min if parsing fails or string is empty.
    Strips timezone offset to avoid mixing aware/naive datetimes.
    """
    if not time_str:
        return datetime.min
    try:
        clean_str = strip_timezone(time_str)
        return datetime.fromisoformat(clean_str)
    except Exception:
        try:
            return datetime.strptime(time_str.split('+')[0].split('.')[0], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return datetime.min

def normalize_time_str(time_str: str) -> str:
    """
    Normalizes time strings containing Arabic digits, diacritics, and Arabic/English AM/PM indicators.
    """
    if not time_str:
        return ""
    text = str(time_str).strip()
    # Normalize Eastern Arabic / Persian numerals to Western digits (0-9)
    arabic_digits = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')
    text = text.translate(arabic_digits)
    # Strip Arabic diacritics (tashkeel/harakat)
    text = re.sub(r'[\u064B-\u065F\u0670]', '', text)
    # Replace Arabic & English PM indicators with standard spaced ' PM '
    text = re.sub(r'(?i)(?:\bمساء\b|\bم\b|pm|(?<=\d)\s*م(?!\w))', ' PM ', text)
    # Replace Arabic & English AM indicators with standard spaced ' AM '
    text = re.sub(r'(?i)(?:\bصباح\b|\bص\b|am|(?<=\d)\s*ص(?!\w))', ' AM ', text)
    return re.sub(r'\s+', ' ', text).strip()

def parse_match_time(date_str: str, time_str: str, source_tz: str | int = None, tz_offset: int = None) -> str:
    """
    Parses date and time strings from a source website and converts it to DEFAULT_TIMEZONE ISO 8601 string.

    Args:
        date_str:   Date in YYYY-MM-DD format.
        time_str:   Time string as shown on the source website (e.g. "09:00 PM", "14:00", "14:00:00", "7:00 م").
        source_tz:  The source website's timezone identifier (e.g. "Asia/Riyadh", "+01:00", "Etc/GMT-3").
        tz_offset:  Optional backwards-compatible numeric offset parameter.

    Returns:
        ISO 8601 string in DEFAULT_TIMEZONE (e.g. "2026-08-21T19:00:00+01:00").
    """
    actual_source_tz = tz_offset if tz_offset is not None else source_tz
    src_zone = resolve_timezone(actual_source_tz)
    target_zone = get_default_timezone()

    normalized = normalize_time_str(time_str)

    # Extract time component if surrounded by extraneous text (e.g. "7:00 PM0-0جارية الآن")
    m = re.search(r'(\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:AM|PM))?)', normalized, re.IGNORECASE)
    time_clean = m.group(1).strip() if m else normalized

    dt_naive = None
    formats = (
        "%Y-%m-%d %I:%M %p",      # 2026-08-23 02:00 PM
        "%Y-%m-%d %I:%M:%S %p",   # 2026-08-23 02:00:00 PM
        "%Y-%m-%d %H:%M",         # 2026-08-23 14:00
        "%Y-%m-%d %H:%M:%S",      # 2026-08-23 14:00:00
        "%Y-%m-%d %I:%M",         # 2026-08-23 02:00
    )

    for fmt in formats:
        try:
            dt_naive = datetime.strptime(f"{date_str} {time_clean}", fmt)
            break
        except ValueError:
            continue

    if dt_naive is None:
        try:
            dt_iso = datetime.fromisoformat(time_str)
            dt_naive = dt_iso.replace(tzinfo=None)
            if dt_iso.tzinfo:
                src_zone = dt_iso.tzinfo
        except Exception:
            pass

    if dt_naive:
        dt_source = dt_naive.replace(tzinfo=src_zone)
        dt_target = dt_source.astimezone(target_zone)
        return dt_target.isoformat()

    logger.warning(f"Could not parse match time '{date_str} {time_str}', falling back to midnight.")
    fallback_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=target_zone)
    return fallback_dt.isoformat()


def parse_user_styled_time(time_str: str) -> datetime:
    """
    Parses user-styled date string (e.g. "25 Aug - 00:01", "22 Aug | 19:45", "29 June 21:00 (UTC+1)")
    or falls back to ISO format. Returns a naive datetime in local/default timezone.
    """
    if not time_str:
        return datetime.min

    # Strip any parenthetical timezone comments like (UTC+1), (GMT+3), (DEFAULT_TIMEZONE, +01:00)
    clean = re.sub(r'\(.*?\)', '', str(time_str)).strip()
    clean = normalize_time_str(clean)

    current_year = datetime.now().year

    # Normalize separators (replace -, |, / with single space)
    normalized = re.sub(r'[-|/]', ' ', clean)
    normalized = re.sub(r'\s+', ' ', normalized).strip()

    formats = (
        "%Y %d %b %H:%M",       # 2026 25 Aug 00:01
        "%Y %d %B %H:%M",       # 2026 25 August 00:01
        "%Y %d %b %I:%M %p",    # 2026 25 Aug 09:00 PM
        "%Y %d %B %I:%M %p",    # 2026 25 August 09:00 PM
        "%Y %b %d %H:%M",       # 2026 Aug 25 00:01
        "%Y %B %d %H:%M",       # 2026 August 25 00:01
        "%Y-%m-%d %H:%M:%S",    # 2026-08-25 00:01:00
        "%Y-%m-%d %H:%M",       # 2026-08-25 00:01
    )

    for fmt in formats:
        try:
            target_str = f"{current_year} {normalized}" if not re.match(r'^\d{4}', normalized) else normalized
            return datetime.strptime(target_str, fmt)
        except ValueError:
            continue

    # Fallback to ISO format parsing
    try:
        clean_str = strip_timezone(clean)
        return datetime.fromisoformat(clean_str)
    except Exception:
        pass

    return datetime.min


def get_match_lookahead_hours() -> float:
    """Returns MATCH_LOOKAHEAD_HOURS env var as float (default 24.0)."""
    val = os.environ.get("MATCH_LOOKAHEAD_HOURS", "").strip()
    try:
        return float(val) if val else 24.0
    except ValueError:
        return 24.0


def get_stream_resolve_lookahead_minutes() -> int:
    """Returns STREAM_RESOLVE_LOOKAHEAD_MINUTES env var as int (default 60)."""
    val = os.environ.get("STREAM_RESOLVE_LOOKAHEAD_MINUTES", "").strip()
    try:
        return int(val) if val else 60
    except ValueError:
        return 60


def get_match_expiry_grace_minutes() -> int:
    """Returns MATCH_EXPIRY_GRACE_MINUTES env var as int (default 180)."""
    val = os.environ.get("MATCH_EXPIRY_GRACE_MINUTES", "").strip()
    try:
        return int(val) if val else 180
    except ValueError:
        return 180


def get_match_default_duration_minutes() -> int:
    """Returns MATCH_DEFAULT_DURATION_MINUTES env var as int (default 180)."""
    val = os.environ.get("MATCH_DEFAULT_DURATION_MINUTES", "").strip()
    try:
        return int(val) if val else 180
    except ValueError:
        return 180


def is_match_expired(kickoff_time: str | datetime, duration: int | str, now_dt: datetime, grace_minutes: int = None) -> bool:
    """
    Returns True if the current time is at or past the match expiration time (Kickoff + Duration + Grace Period).
    """
    if grace_minutes is None:
        grace_minutes = get_match_expiry_grace_minutes()
    if not kickoff_time:
        return False
    dt = kickoff_time if isinstance(kickoff_time, datetime) else parse_user_styled_time(kickoff_time)
    if dt == datetime.min:
        return False
    try:
        duration_mins = int(duration) if duration else get_match_default_duration_minutes()
    except (ValueError, TypeError):
        duration_mins = get_match_default_duration_minutes()
    expiry_dt = dt + timedelta(minutes=duration_mins + grace_minutes)
    return now_dt >= expiry_dt


def is_match_ended(kickoff_time: str | datetime, duration: int | str, now_dt: datetime) -> bool:
    """
    Returns True if the match has passed its scheduled duration (Kickoff + Duration).
    """
    if not kickoff_time:
        return False
    dt = kickoff_time if isinstance(kickoff_time, datetime) else parse_user_styled_time(kickoff_time)
    if dt == datetime.min:
        return False
    try:
        duration_mins = int(duration) if duration else get_match_default_duration_minutes()
    except (ValueError, TypeError):
        duration_mins = get_match_default_duration_minutes()
    end_dt = dt + timedelta(minutes=duration_mins)
    return now_dt >= end_dt


def is_match_in_24h_window(kickoff_time: str | datetime, now_dt: datetime, max_hours: float = None) -> bool:
    """
    Returns True if a match's scheduled kickoff is within max_hours (or in the past/live).
    Returns False if scheduled more than max_hours into the future.
    """
    if max_hours is None:
        max_hours = get_match_lookahead_hours()
    if not kickoff_time:
        return True
    dt = kickoff_time if isinstance(kickoff_time, datetime) else parse_user_styled_time(kickoff_time)
    if dt == datetime.min:
        return True
    time_diff_seconds = (dt - now_dt).total_seconds()
    return time_diff_seconds <= max_hours * 3600


def is_match_starting_soon(
    kickoff_time: str | datetime,
    now_dt: datetime,
    status_class: str = "",
    threshold_minutes: int = None
) -> bool:
    """
    Returns True if a match is either currently LIVE or scheduled to start soon (within threshold_minutes).
    Returns False if the match is finished or scheduled further in the future (> threshold_minutes away).
    """
    if threshold_minutes is None:
        threshold_minutes = get_stream_resolve_lookahead_minutes()

    s_class = (status_class or "").strip().lower()
    if s_class == "finished":
        return False
    if s_class == "live":
        return True

    if not kickoff_time:
        return False
    dt = kickoff_time if isinstance(kickoff_time, datetime) else parse_user_styled_time(kickoff_time)
    if dt == datetime.min:
        return False

    time_until_kickoff = (dt - now_dt).total_seconds()
    # Match starting within threshold_minutes (e.g. 60m) or match already kicked off (<= 0)
    return time_until_kickoff <= threshold_minutes * 60

def get_status_priority(status: str) -> int:
    """Returns numeric priority for match status. Higher value = higher priority."""
    if status == "live":
        return 2
    if status == "upcoming":
        return 1
    return 0

def get_allowed_chat_ids(extra_chat_id: str = None) -> list:
    """Parses TELEGRAM_ALLOWED_CHAT_IDS env var into a list, optionally adding an extra chat ID."""
    allowed_chat_ids_raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    chat_ids = [x.strip() for x in allowed_chat_ids_raw.split(",") if x.strip()] if allowed_chat_ids_raw else []
    if extra_chat_id and extra_chat_id not in chat_ids:
        chat_ids.append(extra_chat_id)
    return chat_ids

# Shared HTTP headers for scraping — single source of truth for User-Agent
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

def broadcast_telegram(bot_token, chat_ids, text):
    """Sends a Telegram message to all chat IDs."""
    if not bot_token or not chat_ids:
        return
    for cid in chat_ids:
        send_telegram_message(bot_token, cid, text)

def get_telegram_bot_token() -> str:
    """Returns the Telegram Bot API token from TELEGRAM_BOT_TOKEN env var."""
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

def get_blog_id() -> str:
    """Returns the Blogger ID for the public website (Tivivi Edu) from BLOG_ID env var."""
    return os.environ.get("BLOG_ID", "").strip()

def get_blog_player_id() -> str:
    """Returns the Blogger ID for the Multi-Channel Player blog from BLOG_PLAYER_ID env var."""
    return os.environ.get("BLOG_PLAYER_ID", "").strip()

def get_blog_data_id() -> str:
    """Returns the Blogger ID for the data website from BLOG_DATA_ID env var."""
    return os.environ.get("BLOG_DATA_ID", "").strip()

def get_data_page_id() -> str:
    """Returns the Blogger page ID for matches data from DATA_PAGE_ID env var."""
    return os.environ.get("DATA_PAGE_ID", "").strip()

DEFAULT_PLAYER_THEME = "?background=ffffff&theme=light&primary=1a73e8"

def get_player_theme() -> str:
    """Returns the player theme query string from PLAYER_THEME env var, or DEFAULT_PLAYER_THEME if unset/empty."""
    theme = os.environ.get("PLAYER_THEME", "").strip()
    return theme if theme else DEFAULT_PLAYER_THEME

def build_player_iframe_url(player_url: str) -> str:
    """Appends the player theme query parameters to the player post URL before embedding it in the public blog.
    Ensures theme queries are always included (defaulting to DEFAULT_PLAYER_THEME).
    """
    if not player_url or not player_url.strip():
        return ""
    player_url = player_url.strip()
    theme = get_player_theme().strip()
    if not theme:
        theme = DEFAULT_PLAYER_THEME

    query = theme.lstrip("?").lstrip("&")
    if not query:
        return player_url

    # Avoid duplicating if already present
    if query in player_url:
        return player_url

    if player_url.endswith("?") or player_url.endswith("&"):
        return f"{player_url}{query}"
    sep = "&" if "?" in player_url else "?"
    return f"{player_url}{sep}{query}"

def get_spreadsheet_name() -> str:
    """Returns the Google Sheets spreadsheet name/ID from SPREADSHEET_NAME env var."""
    return os.environ.get("SPREADSHEET_NAME", "Streaming Dashboard").strip()

def get_slot_label(slot: dict) -> str:
    """Returns a standardized human-readable label for a slot row (e.g. 'Slot #01')."""
    if not slot:
        return "unknown slot"
    raw_slot = slot.get('slot') or slot.get('blog') or ""
    if raw_slot:
        raw_str = str(raw_slot).strip()
        if raw_str.startswith("#"):
            return f"Slot {raw_str}"
        if raw_str.lower().startswith("slot"):
            clean = raw_str.replace("Slot", "").replace("slot", "").strip()
            return f"Slot {clean}" if clean.startswith("#") else f"Slot #{clean}"
        return f"Slot #{raw_str}"
    if slot.get('row_num') is not None:
        return f"Slot (Row {slot['row_num']})"
    return "unknown slot"

def get_blog_label(blog: dict) -> str:
    """Returns a standardized label for a slot (alias for get_slot_label)."""
    return get_slot_label(blog)

def get_event_display_name(event: dict) -> str:
    """Returns 'Team1En vs Team2En' display name for an event."""
    t1 = event['team1'].get('nameEn') or event['team1']['nameAr']
    t2 = event['team2'].get('nameEn') or event['team2']['nameAr']
    return f"{t1} vs {t2}"


class PipelineAbortError(Exception):
    """Raised when the pipeline must halt execution due to an unrecoverable condition."""
    def __init__(self, reason: str, details: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.details = details




