import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from curl_cffi import requests

# Ensure Pipeline directory is on sys.path for standalone imports
_pipeline_dir = str(Path(__file__).resolve().parent.parent)
if _pipeline_dir not in sys.path:
    sys.path.insert(0, _pipeline_dir)

from normalization import are_arabic_names_equivalent, are_english_teams_equivalent

PROXIES = [
    "http://uxtwitfy:1ueyvpq3eh8k@64.137.96.74:6641",   # Spain - Madrid (Primary)
    "http://uxtwitfy:1ueyvpq3eh8k@198.105.121.200:6462", # UK - London
    "http://uxtwitfy:1ueyvpq3eh8k@45.38.107.97:6014",    # UK - London
    "http://uxtwitfy:1ueyvpq3eh8k@31.59.20.176:6754",    # UK - London
    "http://uxtwitfy:1ueyvpq3eh8k@142.111.67.146:5611",  # Japan - Tokyo
]

DEFAULT_HEADERS = {
    "authority": "www.sofascore.com",
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9,ar;q=0.8",
    "referer": "https://www.sofascore.com/",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


class SofaClient:
    """HTTP client for SofaScore with TLS Chrome fingerprinting, proxy rotation, and caching."""

    def __init__(self, proxies: list[str] | None = None, timeout: int = 10):
        self.proxies = proxies if proxies is not None else PROXIES
        self.timeout = timeout
        self._proxy_idx = 0
        self._session: requests.Session | None = None
        self._team_events_cache: dict[str, list[dict]] = {}
        self._search_cache: dict[str, list[dict]] = {}
        self._build_session()

    def _build_session(self):
        """Creates a fresh curl_cffi session for the current proxy, closing any prior session."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass

        proxy = self.proxies[self._proxy_idx] if self.proxies else None
        self._session = requests.Session(impersonate="chrome120")
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}

    def _rotate_proxy(self):
        """Advances to the next proxy in pool and rebuilds session."""
        if not self.proxies:
            return
        self._proxy_idx = (self._proxy_idx + 1) % len(self.proxies)
        self._build_session()

    def get(self, endpoint: str, params: dict | None = None, max_retries: int = 5) -> dict | None:
        """Executes GET request. Rotates on 403/429/5xx/non-JSON/timeout. Fails fast on 404."""
        url = endpoint if endpoint.startswith("http") else f"https://www.sofascore.com/api/v1{endpoint}"
        retries = 0
        while retries < max_retries:
            try:
                resp = self._session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=self.timeout)
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except Exception:
                        # 200 OK with non-JSON body (e.g. Cloudflare captcha/challenge HTML)
                        self._rotate_proxy()
                        retries += 1
                        time.sleep(0.3)
                        continue
                if resp.status_code == 404:
                    return None
                if resp.status_code in (403, 429) or resp.status_code >= 500:
                    self._rotate_proxy()
                    retries += 1
                    time.sleep(0.3)
                    continue
                return None
            except Exception:
                self._rotate_proxy()
                retries += 1
                time.sleep(0.3)
        return None

    def get_team_events(self, team_id: int | str, direction: str = "next", page: int = 0) -> list[dict]:
        """Fetches and caches next/last events for a team."""
        cache_key = f"{team_id}_{direction}_{page}"
        if cache_key in self._team_events_cache:
            return self._team_events_cache[cache_key]

        data = self.get(f"/team/{team_id}/events/{direction}/{page}")
        events = (data or {}).get("events", [])
        self._team_events_cache[cache_key] = events
        return events

    def search_teams(self, query: str) -> list[dict]:
        """Searches SofaScore and returns candidates filtered to Senior Men's Football."""
        clean_q = (query or "").strip().lower()
        if not clean_q:
            return []
        if clean_q in self._search_cache:
            return self._search_cache[clean_q]

        data = self.get("/search/all", params={"q": query})
        results = (data or {}).get("results", [])
        candidates = []
        for item in results:
            if item.get("type") != "team":
                continue
            entity = item.get("entity", {})
            sport = entity.get("sport", {})
            sport_slug = sport.get("slug") if isinstance(sport, dict) else ""
            gender = entity.get("gender")
            if sport_slug == "football" and gender == "M":
                candidates.append(entity)

        self._search_cache[clean_q] = candidates
        return candidates

    def get_event(self, event_id: int | str) -> dict | None:
        """Fetches details for a single event."""
        data = self.get(f"/event/{event_id}")
        return (data or {}).get("event")


def is_match_date_matching(event_timestamp: int | None, target_iso_str: str | None) -> bool:
    """Checks if event timestamp aligns with target ISO 8601 datetime (e.g. '2026-09-24T18:45:00Z') within +/- 6h."""
    if not target_iso_str or not event_timestamp:
        return True

    clean_iso = str(target_iso_str).strip()
    ev_dt = datetime.fromtimestamp(event_timestamp, tz=timezone.utc)

    try:
        parsed_dt = datetime.fromisoformat(clean_iso.replace("Z", "+00:00"))
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        return abs((ev_dt - parsed_dt).total_seconds()) <= 21600
    except Exception:
        return False


def teams_match(name1: str | None, name2: str | None) -> bool:
    """Compares two team names using project English and Arabic equivalence normalizers."""
    if not name1 or not name2:
        return False
    n1, n2 = name1.strip(), name2.strip()
    if n1.lower() == n2.lower():
        return True
    has_ar = bool(re.search(r"[\u0600-\u06FF]", n1) or re.search(r"[\u0600-\u06FF]", n2))
    return are_arabic_names_equivalent(n1, n2) if has_ar else are_english_teams_equivalent(n1, n2)


def extract_team_ar_name(team_dict: dict | None) -> str | None:
    """Safely extracts Arabic name translation from team entity dictionary."""
    if not isinstance(team_dict, dict):
        return None
    field_trans = team_dict.get("fieldTranslations")
    if isinstance(field_trans, dict):
        name_trans = field_trans.get("nameTranslation")
        if isinstance(name_trans, dict):
            return name_trans.get("ar")
    return None
