import os
import re
import json
import requests
import gspread
import logger
from sheets_module import open_spreadsheet

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _parse_translation_sheet(sh, sheet_name: str, type_label: str, translations: dict):
    """Loads a single cache worksheet and parses rows into translations dict."""
    try:
        worksheet = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="5")
        worksheet.append_row(["arabic_name", "english_name", "code", "logo_url"])
        return

    all_values = worksheet.get_all_values()
    if not all_values or len(all_values) <= 1:
        return

    headers = [h.strip().lower() for h in all_values[0]]
    rows = all_values[1:]
    header_map = {h: idx for idx, h in enumerate(headers)}

    for idx, row in enumerate(rows, start=2):
        padded_row = row + [""] * (len(headers) - len(row))
        arabic_name_raw = padded_row[header_map.get("arabic_name", 0)].strip()
        if not arabic_name_raw:
            continue

        aliases = [a.strip() for a in re.split(r'[|;\n,]', arabic_name_raw) if a.strip()]
        if not aliases:
            continue

        primary_arabic = aliases[0]
        name_en = padded_row[header_map.get("english_name", 1)].strip()
        code = padded_row[header_map.get("code", 2)].strip()

        # "--" means "needs human review" — treat as Unknown/### so the script never uses bad data
        if not name_en or name_en == "--":
            name_en = "Unknown"
            code = "###"

        translations[primary_arabic] = {
            "nameEn": name_en,
            "code": code,
            "logo_url": padded_row[header_map.get("logo_url", 3)].strip(),
            "type": type_label,
            "row_num": idx,
            "sheet_name": sheet_name,
            "primary_arabic": primary_arabic,
            "original_arabic_cell": arabic_name_raw
        }


def load_team_translations(client, spreadsheet_name: str = "Streaming Dashboard") -> dict:
    """
    Fetches the team translations from '_cache_national_teams' and '_cache_clubs' worksheets.
    """
    sh = open_spreadsheet(client, spreadsheet_name)
    translations = {}
    _parse_translation_sheet(sh, "_cache_national_teams", "national", translations)
    _parse_translation_sheet(sh, "_cache_clubs", "club", translations)
    return translations


def find_existing_translation(name: str, team_translations: dict) -> dict:
    """Looks up a team name in the team_translations cache using exact matching."""
    if not team_translations:
        return None

    if name in team_translations:
        return team_translations[name]

    for v in team_translations.values():
        orig_cell = v.get("original_arabic_cell", "")
        if not orig_cell:
            continue
        aliases = [a.strip() for a in re.split(r'[|;\n,]', orig_cell) if a.strip()]
        if name in aliases:
            return v
    return None


def update_team_aliases(client, alias_updates: list, spreadsheet_name: str = "Streaming Dashboard"):
    """Appends new aliases to existing rows in Google Sheets."""
    if not alias_updates:
        return

    sh = open_spreadsheet(client, spreadsheet_name)
    by_sheet = {}
    for row_num, sheet_name, new_val in alias_updates:
        by_sheet.setdefault(sheet_name, []).append((row_num, new_val))

    for sheet_name, updates in by_sheet.items():
        try:
            worksheet = sh.worksheet(sheet_name)
        except Exception as e:
            logger.error(f"TranslationManager: Worksheet '{sheet_name}' not found: {e}")
            continue

        for row_num, new_val in updates:
            worksheet.update_cell(row_num, 1, new_val)
            logger.success(f"TranslationManager: Appended alias in '{sheet_name}' row {row_num} to: '{new_val}'")


def save_new_team_translations_separated(client, new_translations: list, spreadsheet_name: str = "Streaming Dashboard"):
    """Appends new translation rows to either '_cache_national_teams' or '_cache_clubs' worksheet."""
    if not new_translations:
        return

    sh = open_spreadsheet(client, spreadsheet_name)
    national_rows = [[t[0], t[1], t[2], t[3]] for t in new_translations if t[4] == "national"]
    club_rows = [[t[0], t[1], t[2], t[3]] for t in new_translations if t[4] != "national"]

    for sheet_name, rows in [("_cache_national_teams", national_rows), ("_cache_clubs", club_rows)]:
        if not rows:
            continue
        try:
            worksheet = sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="5")
            worksheet.append_row(["arabic_name", "english_name", "code", "logo_url"])

        worksheet.append_rows(rows)
        logger.success(f"TranslationManager: Appended {len(rows)} new rows to '{sheet_name}'.")


def _clean_llm_json(content: str) -> str:
    """Strips markdown fences from LLM JSON response content."""
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    return content


def _call_openrouter_model(model: str, payload: dict, headers: dict) -> dict | None:
    """Executes a single OpenRouter model request and parses response JSON."""
    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=20)
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            logger.success(f"OpenRouter: API call successful using model: {model}")
            return json.loads(_clean_llm_json(content))
        logger.error(f"OpenRouter: Error with model {model}: HTTP {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"OpenRouter: Exception with model {model}: {e}")
    return None


def fetch_openrouter_mappings(unique_names: list) -> dict:
    """Calls OpenRouter to batch translate Arabic team names to English and resolve ISO/club codes."""
    if not unique_names:
        return {}

    system_prompt = (
        "You are an expert football database assistant. Translate Arabic football team and country names into standard international English with their official broadcast codes.\n\n"
        "Instructions for each team:\n"
        '1. "nameEn": Full, official, and standard English name (e.g. "Inter Miami", "Inter Milan", "Toronto FC", "Torino", "Manchester City", "Manchester United", "Bayern Munich", "ENPPI").\n'
        '   - If the team is unknown, cannot be identified with high confidence, or has no valid English translation: set "nameEn": "Unknown".\n'
        '   - NEVER echo the Arabic name back as the English name.\n'
        '2. "code":\n'
        '   - If National Team: Standard 2-letter lowercase ISO country code (e.g. "ma", "eg", "es", "fr", "br", "ar", "sa"; UK: "gb-eng", "gb-sct", "gb-wls", "gb-nir").\n'
        '   - If Club: Standard 3-letter uppercase league/TV broadcast abbreviation that uniquely identifies the club (e.g. "MIA" for Inter Miami, "INT" for Inter Milan, "TOR" for Toronto FC, "TRN" for Torino, "MCI" for Manchester City, "MUN" for Manchester United, "ATM" for Atlético Madrid, "ATH" for Athletic Bilbao, "RMA" for Real Madrid, "FCB" for Barcelona, "PSG" for Paris Saint-Germain, "ENP" for ENPPI).\n'
        '   - If the team is unknown or the code cannot be determined: set "code": "###".\n\n'
        "Disambiguation Rules:\n"
        '- Never confuse clubs sharing a common word like "Inter" (Inter Miami is MIA, Inter Milan is INT) or "Real" (Real Madrid is RMA, Real Sociedad is RSO).\n'
        '- Output a strict JSON object mapping each input Arabic name to {"nameEn": "...", "code": "..."}.\n'
        "- Respond with raw JSON only. No markdown fences, no explanatory text."
    )

    models = ["openrouter/free", "google/gemini-2.5-flash", "meta-llama/llama-3.1-8b-instruct"]
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/google/antigravity",
        "X-Title": "Antigravity Match Scraper"
    }

    for model in models:
        logger.info(f"OpenRouter: Requesting team details using model: {model}")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(unique_names, ensure_ascii=False)}
            ],
            "response_format": {"type": "json_object"}
        }
        res = _call_openrouter_model(model, payload, headers)
        if res is not None:
            return res

    logger.warning("OpenRouter: All models failed. Falling back to local offline mapping rules.")
    return {}


def _resolve_team_logo_and_type(name: str, code: str, matches_to_process: list) -> tuple:
    """Determines team type ('national' vs 'club') and resolves logo URL."""
    is_national = (code.islower() and len(code) == 2) or (
        code.lower() in ["gb-eng", "gb-sct", "gb-wls", "gb-nir"]
    )
    if is_national:
        return "national", f"https://flagcdn.com/{code.lower()}.svg"

    logo_url = ""
    for m in matches_to_process:
        if m["team1_name"] == name:
            logo_url = m["team1_orig_img"]
            break
        elif m["team2_name"] == name:
            logo_url = m["team2_orig_img"]
            break
    return "club", logo_url


def _is_unknown_team(name_en: str, code: str) -> bool:
    """Returns True if the AI response indicates it does not know this team.

    Catches: explicit Unknown/### sentinels, empty values, old CLUB fallback,
    and cases where the AI echoed the Arabic name back as the English name.
    """
    if not name_en or name_en.strip().lower() == "unknown":
        return True
    if not code or code.strip().upper() in ("###", "CLUB", ""):
        return True
    # Detect Arabic characters echoed back as nameEn
    if any('\u0600' <= c <= '\u06FF' for c in name_en):
        return True
    return False


def _add_placeholder_row(name: str, logo_url: str, team_translations: dict, new_translations_list: list):
    """Writes a '--' placeholder row to Sheets and sets the team as Unknown in memory.

    Used when the AI does not know the team or returns a potentially wrong match.
    The human can open Google Sheets, see the Arabic name + logo, and fill in the correct
    English name and code. Next run, the entry is loaded normally.
    """
    team_translations[name] = {
        "nameEn": "Unknown",
        "code": "###",
        "logo_url": "",
        "type": "club",
        "primary_arabic": name,
        "original_arabic_cell": name
    }
    new_translations_list.append((name, "--", "###", logo_url, "club"))


def resolve_missing_teams(missing_team_names: list, team_translations: dict, matches_to_process: list) -> tuple:
    """Resolves Arabic team names not found in the local translation cache."""
    new_translations_list = []
    alias_updates = []

    if not missing_team_names:
        skip_msg = f"{logger.COLOR_DARK_GRAY}Skipping OpenRouter.{logger.COLOR_RESET}"
        logger.success(f"Translation: All teams found in translation cache. {skip_msg}")
        return new_translations_list, alias_updates

    logger.info(f"Translation: Sending {len(missing_team_names)} new/untranslated teams to OpenRouter...")
    llm_mappings = fetch_openrouter_mappings(missing_team_names)

    for name in missing_team_names:
        raw_info = llm_mappings.get(name)
        if isinstance(raw_info, dict):
            name_en = raw_info.get("nameEn", "").strip()
            code = raw_info.get("code", "###").strip().upper()
        elif isinstance(raw_info, str) and raw_info.strip():
            name_en = raw_info.strip()
            code = "###"
        else:
            name_en = "Unknown"
            code = "###"

        # Always resolve the scraped logo first — it is contextually correct for this match
        # Use "###" as code for type-resolution when unknown so we always get "club" + scraped logo
        type_code = "###" if _is_unknown_team(name_en, code) else code
        team_type, logo_url = _resolve_team_logo_and_type(name, type_code, matches_to_process)

        if _is_unknown_team(name_en, code):
            # AI does not know this team — write "--" placeholder row for human review
            logger.warning(f"Translation: Unknown team '{name}' — adding '--' row to Google Sheets for human review.")
            _add_placeholder_row(name, logo_url, team_translations, new_translations_list)
            continue

        # Check if AI's translated nameEn already exists in the cache
        normalized_name_en = name_en.lower()
        found_team_entry = next(
            (v for v in team_translations.values() if v.get("nameEn", "").strip().lower() == normalized_name_en),
            None
        )

        if found_team_entry:
            # AI matched an existing entry — this could be a wrong match (e.g. نيوم → Newcastle United).
            # Write "--" row for human review instead of auto-aliasing and risking wrong data.
            logger.warning(
                f"Translation: AI matched '{name}' to existing '{found_team_entry['nameEn']}' — "
                f"may be wrong. Adding '--' row for human review."
            )
            _add_placeholder_row(name, logo_url, team_translations, new_translations_list)
            continue

        team_translations[name] = {
            "nameEn": name_en,
            "code": code,
            "logo_url": logo_url,
            "type": team_type,
            "primary_arabic": name,
            "original_arabic_cell": name
        }
        new_translations_list.append((name, name_en, code, logo_url, team_type))

    return new_translations_list, alias_updates
