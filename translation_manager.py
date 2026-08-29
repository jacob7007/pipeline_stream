import os
import re
import json
import requests
import gspread
import logger
from sheets_module import open_spreadsheet
from normalization import (
    are_arabic_names_equivalent,
    are_english_teams_equivalent,
)

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

        # "--" means "needs human review" — treat as Unknown so the script never uses bad data
        if not name_en or name_en == "--":
            name_en = "Unknown"
            code = ""

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
    """
    Looks up a team name in the team_translations cache using exact matching,
    alias matching, and Arabic phonetic/orthographic normalization.
    Prioritizes fully translated entries over placeholder ('--') rows.
    """
    if not team_translations or not name:
        return None

    def _is_valid(entry: dict) -> bool:
        return entry.get("nameEn") not in ("Unknown", "", None)

    # Step 1: Direct exact primary name match (if fully translated)
    if name in team_translations:
        entry = team_translations[name]
        if _is_valid(entry):
            return entry

    # Step 2: Exact alias lookup across all loaded translations (if fully translated)
    for v in team_translations.values():
        if not _is_valid(v):
            continue
        orig_cell = v.get("original_arabic_cell", "")
        if not orig_cell:
            continue
        aliases = [a.strip() for a in re.split(r'[|;\n,]', orig_cell) if a.strip()]
        if name in aliases:
            return v

    # Step 3: Arabic phonetic/orthographic normalization match
    best_candidate = None
    for v in team_translations.values():
        orig_cell = v.get("original_arabic_cell", "")
        aliases = [a.strip() for a in re.split(r'[|;\n,]', orig_cell) if a.strip()] if orig_cell else []
        if not aliases and v.get("primary_arabic"):
            aliases = [v["primary_arabic"]]

        for alias in aliases:
            if are_arabic_names_equivalent(name, alias):
                if _is_valid(v):
                    return v
                if best_candidate is None:
                    best_candidate = v

    # Step 4: Fallback to direct placeholder if no valid translation was found
    if name in team_translations:
        return team_translations[name]

    return best_candidate


def update_team_aliases(client, alias_updates: list, spreadsheet_name: str = "Streaming Dashboard"):
    """Appends new aliases or backfills missing fields on existing rows in Google Sheets."""
    if not alias_updates:
        return

    sh = open_spreadsheet(client, spreadsheet_name)
    by_sheet = {}
    for update in alias_updates:
        row_num, sheet_name = update[0], update[1]
        col_idx, new_val = (update[2], update[3]) if len(update) == 4 else (1, update[2])
        by_sheet.setdefault(sheet_name, []).append((row_num, col_idx, new_val))

    for sheet_name, updates in by_sheet.items():
        try:
            worksheet = sh.worksheet(sheet_name)
        except Exception as e:
            logger.error(f"TranslationManager: Worksheet '{sheet_name}' not found: {e}")
            continue

        for row_num, col_idx, new_val in updates:
            try:
                worksheet.update_cell(row_num, col_idx, new_val)
                action = "alias" if col_idx == 1 else "logo"
                logger.success(f"TranslationManager: Updated {action} in '{sheet_name}' row {row_num} to: '{new_val}'")
            except Exception as e:
                logger.error(f"TranslationManager: Failed updating '{sheet_name}' row {row_num}: {e}")


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
    """Calls OpenRouter to batch translate Arabic team names to English."""
    if not unique_names:
        return {}

    system_prompt = (
        "You are an expert football database assistant. "
        "For each Arabic football team name in the input list, return a JSON object with:\n\n"
        "1. \"nameEn\": The full, official English name of the team.\n"
        "   Rules:\n"
        "   - Always use the COMPLETE, disambiguated official name. NEVER abbreviate or truncate:\n"
        "     ✓ \"Inter Milan\"       ✗ \"Inter\"\n"
        "     ✓ \"Inter Miami\"       ✗ \"Inter\"\n"
        "     ✓ \"Real Madrid\"       ✓ \"Real Sociedad\"    ✗ \"Real\"\n"
        "     ✓ \"Sporting CP\"       ✓ \"Sporting Gijón\"   ✗ \"Sporting\"\n"
        "     ✓ \"Manchester City\"   ✓ \"Manchester United\" ✗ \"Manchester\"\n"
        "     ✓ \"Bayer Leverkusen\" (NOT \"Leverkusen\" or \"Bayer\")\n"
        "   - For a NATIONAL TEAM use the country's standard English name (e.g. \"Morocco\", \"France\", \"Saudi Arabia\").\n"
        "   - If the team cannot be identified with high confidence: set \"nameEn\": \"Unknown\".\n"
        "   - NEVER echo the Arabic input back as the English name.\n"
        "   - NEVER invent or guess a name you are not confident about.\n\n"
        "2. \"code\":\n"
        "   - For NATIONAL TEAMS only: the standard 2-letter lowercase ISO 3166-1 alpha-2 country code.\n"
        "     Examples: \"ma\" (Morocco), \"es\" (Spain), \"fr\" (France), \"eg\" (Egypt), \"sa\" (Saudi Arabia).\n"
        "     UK nations: \"gb-eng\", \"gb-sct\", \"gb-wls\", \"gb-nir\".\n"
        "   - For CLUB teams: set \"code\": \"\" (empty string).\n"
        "   - If unknown: set \"code\": \"\".\n\n"
        "Output a strict JSON object mapping each input Arabic name to {\"nameEn\": \"...\", \"code\": \"\"}.\n"
        "Respond with raw JSON only. No markdown fences, no explanatory text."
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
    code_clean = (code or "").strip().lower()
    is_national = (len(code_clean) == 2 and code_clean.isalpha()) or \
                  code_clean in ("gb-eng", "gb-sct", "gb-wls", "gb-nir")
    if is_national:
        return "national", f"https://flagcdn.com/{code_clean}.svg"

    logo_url = ""
    for m in matches_to_process:
        if m["team1_name"] == name:
            logo_url = m["team1_orig_img"]
            break
        elif m["team2_name"] == name:
            logo_url = m["team2_orig_img"]
            break
    return "club", logo_url


def _is_unknown_team(name_en: str) -> bool:
    """Returns True if the AI response indicates it does not know this team."""
    if not name_en or not str(name_en).strip() or str(name_en).strip().lower() in ("unknown", "--", "none"):
        return True
    # Detect Arabic characters echoed back as nameEn
    if any('\u0600' <= c <= '\u06FF' for c in str(name_en)):
        return True
    return False



def _add_placeholder_row(name: str, logo_url: str, team_translations: dict, new_translations_list: list):
    """Writes a '--' placeholder row to Sheets and sets the team as Unknown in memory."""
    team_translations[name] = {
        "nameEn": "Unknown",
        "code": "",
        "logo_url": logo_url,
        "type": "club",
        "primary_arabic": name,
        "original_arabic_cell": name
    }
    new_translations_list.append((name, "--", "", logo_url, "club"))


def _is_valid_iso_code(code: str) -> bool:
    """Returns True if the code looks like a valid ISO national team code."""
    c = (code or "").strip().lower()
    return (len(c) == 2 and c.isalpha()) or c in ("gb-eng", "gb-sct", "gb-wls", "gb-nir")


def _find_matching_cached_team(name_en: str, code: str, team_translations: dict) -> dict | None:
    """
    Searches for an existing team in cache that matches the newly translated team.

    Matching strategy (name-first, code never used for clubs):
      Pass 1 — National teams: if code is a valid ISO 2-letter code, match by code.
               ISO-3166 codes are globally unique so this is safe.
      Pass 2 — Exact English name match (accent-stripped, lowercase).
      Pass 3 — Canonical English equivalence via are_english_teams_equivalent()
               which now includes conflict detection and set-equality token matching.

    Club codes are NEVER used as a match condition to prevent collisions between
    clubs that share the same 3-letter broadcast abbreviation (e.g. LEV for both
    Levante UD and Bayer Leverkusen).
    """
    if not team_translations:
        return None

    clean_en = name_en.strip().lower()

    def _is_valid_entry(v: dict) -> bool:
        return v.get("nameEn") not in ("Unknown", "", None)

    # Pass 1: National team ISO code match — codes are globally unique for nations
    if _is_valid_iso_code(code):
        for v in team_translations.values():
            if not _is_valid_entry(v):
                continue
            if v.get("code", "").strip().lower() == code.strip().lower():
                return v

    # Pass 2: Exact English name match
    for v in team_translations.values():
        if not _is_valid_entry(v):
            continue
        if v.get("nameEn", "").strip().lower() == clean_en:
            return v

    # Pass 3: Canonical English equivalence (conflict-aware, no subset trap)
    for v in team_translations.values():
        if not _is_valid_entry(v):
            continue
        if are_english_teams_equivalent(name_en, v.get("nameEn", "")):
            return v

    return None


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
            code = raw_info.get("code", "").strip()
        elif isinstance(raw_info, str) and raw_info.strip():
            name_en = raw_info.strip()
            code = ""
        else:
            name_en = "Unknown"
            code = ""

        team_type, logo_url = _resolve_team_logo_and_type(name, code, matches_to_process)

        if _is_unknown_team(name_en):
            # AI does not know this team — write "--" placeholder row for human review
            logger.warning(f"Translation: Unknown team '{name}' — adding '--' row to Google Sheets for human review.")
            _add_placeholder_row(name, logo_url, team_translations, new_translations_list)
            continue

        # Check if this team is an alias or variant of an already known team
        matched_cached_team = _find_matching_cached_team(name_en, code, team_translations)

        # Safety net: verify the English names actually agree before writing any alias.
        # This guards against edge cases where Pass 3 might find a wrong candidate.
        if matched_cached_team:
            canonical_en = matched_cached_team.get("nameEn", "")
            if not are_english_teams_equivalent(name_en, canonical_en):
                logger.warning(
                    f"Translation: Safety net caught collision — AI returned '{name_en}' but "
                    f"matched cached entry is '{canonical_en}'. Creating separate new entry instead."
                )
                matched_cached_team = None

        if matched_cached_team:
            # Auto-link as alias to existing team entry
            canonical_en = matched_cached_team.get("nameEn", name_en)
            orig_cell = matched_cached_team.get("original_arabic_cell", "")
            aliases = [a.strip() for a in re.split(r'[|;\n,]', orig_cell) if a.strip()] if orig_cell else []
            if name not in aliases:
                new_val = f"{orig_cell} | {name}" if orig_cell else name
                matched_cached_team["original_arabic_cell"] = new_val
                row_num = matched_cached_team.get("row_num")
                sheet_name = matched_cached_team.get("sheet_name")
                if row_num and sheet_name:
                    alias_updates.append((row_num, sheet_name, new_val))

            # Backfill logo if missing in matched cached team
            if logo_url and not matched_cached_team.get("logo_url"):
                matched_cached_team["logo_url"] = logo_url
                row_num = matched_cached_team.get("row_num")
                sheet_name = matched_cached_team.get("sheet_name")
                if row_num and sheet_name:
                    alias_updates.append((row_num, sheet_name, 4, logo_url))

            team_translations[name] = matched_cached_team
            logger.success(
                f"Translation: Auto-linked Arabic name '{name}' as alias for existing team '{canonical_en}'."
            )
            continue

        # Brand new team — add to cache and record new row for Google Sheets
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
