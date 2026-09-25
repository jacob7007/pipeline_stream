import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure Pipeline directory is on sys.path for standalone imports
_pipeline_dir = str(Path(__file__).resolve().parent.parent)
if _pipeline_dir not in sys.path:
    sys.path.insert(0, _pipeline_dir)

try:
    from .ss_utils import SofaClient, is_match_date_matching, teams_match, extract_team_ar_name
except ImportError:
    from ss_utils import SofaClient, is_match_date_matching, teams_match, extract_team_ar_name


def _safe_int(val: Any) -> int | None:
    """Safely parses string or int to int, ignoring empty or None values."""
    if val in (None, ""):
        return None
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


def _extract_team_identifiers(team_dict: dict) -> tuple[int | None, list[str]]:
    """Extracts team ID and all known name representations (en, short, code, ar)."""
    if not isinstance(team_dict, dict):
        return None, []
    tid = team_dict.get("id")
    names = []
    for key in ("name", "shortName", "nameCode"):
        val = team_dict.get(key)
        if val and isinstance(val, str):
            names.append(val)
    ar_name = extract_team_ar_name(team_dict)
    if ar_name:
        names.append(ar_name)
    return tid, names


def _find_event_in_list(
    events: list[dict],
    opponent_id: int | None = None,
    opponent_names: list[str] | None = None,
    kickoff_iso: str | None = None,
) -> dict | None:
    """Scans events for a match against the opponent (by code or name) within date window."""
    opponent_names = opponent_names or []
    for ev in events:
        if not is_match_date_matching(ev.get("startTimestamp"), kickoff_iso):
            continue

        h_id, h_names = _extract_team_identifiers(ev.get("homeTeam", {}))
        a_id, a_names = _extract_team_identifiers(ev.get("awayTeam", {}))

        if opponent_id is not None:
            if opponent_id in (h_id, a_id):
                return ev
            continue

        all_opp_names = h_names + a_names
        for target_name in opponent_names:
            if any(teams_match(target_name, cand_name) for cand_name in all_opp_names):
                return ev
    return None


def _scan_for_opponent(
    team_id: int,
    kickoff_iso: str | None,
    client: SofaClient,
    *,
    opponent_id: int | None = None,
    opponent_names: list[str] | None = None,
) -> dict | None:
    """Unified helper: fetches next/last events for team_id and searches for the opponent."""
    for direction in ("next", "last"):
        events = client.get_team_events(team_id, direction=direction)
        ev = _find_event_in_list(
            events,
            opponent_id=opponent_id,
            opponent_names=opponent_names,
            kickoff_iso=kickoff_iso,
        )
        if ev:
            return ev
    return None


def _resolve_by_names(
    team1_names: list[str],
    team2_names: list[str],
    kickoff_iso: str | None,
    client: SofaClient,
) -> dict | None:
    """Scenario C: Searches team candidates and scans schedule with deduplication and reverse fallback."""
    seen_ids: set[int] = set()

    # Forward search: Team 1 candidates searching for Team 2
    for query in team1_names:
        for candidate in client.search_teams(query)[:3]:
            cid = candidate.get("id")
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)
            ev = _scan_for_opponent(cid, kickoff_iso, client, opponent_names=team2_names)
            if ev:
                return ev

    # Bidirectional fallback: Team 2 candidates searching for Team 1
    for query in team2_names:
        for candidate in client.search_teams(query)[:3]:
            cid = candidate.get("id")
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)
            ev = _scan_for_opponent(cid, kickoff_iso, client, opponent_names=team1_names)
            if ev:
                return ev

    return None


def _map_discovered_team_codes(
    home_id: int | None,
    away_id: int | None,
    home_names: list[str],
    team1_code: int | None,
    team2_code: int | None,
    team1_names: list[str],
) -> tuple[int | None, int | None]:
    """Determines which discovered team ID corresponds to team1 vs team2."""
    if team1_code is not None:
        return (home_id, away_id) if home_id == team1_code else (away_id, home_id)
    if team2_code is not None:
        return (away_id, home_id) if home_id == team2_code else (home_id, away_id)

    is_t1_home = any(teams_match(t1, h) for t1 in team1_names for h in home_names)
    return (home_id, away_id) if is_t1_home else (away_id, home_id)


def _format_match_result(
    event: dict,
    team1_code: int | None,
    team2_code: int | None,
    team1_names: list[str],
) -> dict:
    """Formats raw SofaScore event dictionary into standardized return contract."""
    event_id = event.get("id")
    slug = event.get("slug", "")
    custom_id = event.get("customId", "")
    url = f"https://www.sofascore.com/football/match/{slug}/{custom_id}#id:{event_id}"

    start_ts = event.get("startTimestamp")
    kickoff_utc = (
        datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if start_ts
        else None
    )

    home_ent = event.get("homeTeam", {})
    away_ent = event.get("awayTeam", {})
    home_id, home_names = _extract_team_identifiers(home_ent)
    away_id, _ = _extract_team_identifiers(away_ent)

    home_dict = {"id": home_id, "name": home_ent.get("name")}
    home_ar = extract_team_ar_name(home_ent)
    if home_ar:
        home_dict["name_ar"] = home_ar

    away_dict = {"id": away_id, "name": away_ent.get("name")}
    away_ar = extract_team_ar_name(away_ent)
    if away_ar:
        away_dict["name_ar"] = away_ar

    disc_t1, disc_t2 = _map_discovered_team_codes(
        home_id, away_id, home_names, team1_code, team2_code, team1_names
    )

    return {
        "found": True,
        "event_id": event_id,
        "slug": slug,
        "custom_id": custom_id,
        "sofascore_url": url,
        "status": event.get("status", {}).get("type", "unknown"),
        "status_description": event.get("status", {}).get("description", ""),
        "start_timestamp": start_ts,
        "kickoff_utc": kickoff_utc,
        "tournament": event.get("tournament", {}).get("name", ""),
        "season": event.get("season", {}).get("name", ""),
        "home_team": home_dict,
        "away_team": away_dict,
        "scores": {
            "home": event.get("homeScore", {}).get("current"),
            "away": event.get("awayScore", {}).get("current"),
        },
        "discovered_team1_code": disc_t1,
        "discovered_team2_code": disc_t2,
    }


def _to_names_list(val: Any) -> list[str]:
    """Converts a string, list/tuple of strings, or None into a clean non-empty list of string names."""
    if not val:
        return []
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, (list, tuple, set)):
        res = []
        for item in val:
            if item and isinstance(item, str) and item.strip():
                s = item.strip()
                if s not in res:
                    res.append(s)
        return res
    return []


def resolve_match_event(
    team1: str | list[str],
    team2: str | list[str],
    kickoff_iso: str | None = None,
    team1_code: str | int | None = None,
    team2_code: str | int | None = None,
    client: SofaClient | None = None,
) -> dict | None:
    """
    Resolves match event details and IDs from SofaScore.
    Handles Scenario A (both codes), Scenario B (1 code), and Scenario C (0 codes).

    Parameters:
        team1: Name(s) of Team 1 (e.g. ['Real Madrid', 'ريال مدريد'], ['ريال مدريد'], or 'Real Madrid')
        team2: Name(s) of Team 2 (e.g. ['Kuwait', 'الكويت'], ['الكويت'], or 'Kuwait')
        kickoff_iso: ISO 8601 kickoff datetime string (e.g. '2026-09-24T18:45:00Z') or None
        team1_code: SofaScore ID for Team 1 (int/numeric str/None)
        team2_code: SofaScore ID for Team 2 (int/numeric str/None)
        client: Optional shared SofaClient instance
    """
    client = client or SofaClient()

    c1 = _safe_int(team1_code)
    c2 = _safe_int(team2_code)

    t1_names = _to_names_list(team1)
    t2_names = _to_names_list(team2)

    ev = None
    if c1 is not None and c2 is not None:
        ev = _scan_for_opponent(c1, kickoff_iso, client, opponent_id=c2)
    elif c1 is not None:
        ev = _scan_for_opponent(c1, kickoff_iso, client, opponent_names=t2_names)
    elif c2 is not None:
        ev = _scan_for_opponent(c2, kickoff_iso, client, opponent_names=t1_names)
    else:
        ev = _resolve_by_names(t1_names, t2_names, kickoff_iso, client)

    if not ev:
        return None

    return _format_match_result(ev, c1, c2, t1_names)

