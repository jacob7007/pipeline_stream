import os
import re
import json
import requests
import gspread
import logger
from sheets_module import open_spreadsheet
from utils import sanitize_sheet_image_url, PLACEHOLDER_IMAGE_URL
from normalization import (
    are_arabic_names_equivalent,
    are_english_teams_equivalent,
    set_canonical_synonyms,
)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_TIMEOUT = int(os.environ.get("OPENROUTER_TIMEOUT", 10))


def _parse_translation_sheet(sh, sheet_name: str, type_label: str, translations: dict, dynamic_synonyms: dict):
    """Loads a single cache worksheet and parses rows into translations dict."""
    try:
        worksheet = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="5")
        if type_label == "national":
            worksheet.append_row(["arabic_aliases", "primary_arabic_name", "primary_english_name", "code", "logo_url"])
        else:
            worksheet.append_row(["arabic_aliases", "primary_arabic_name", "primary_english_name", "canonical_synonyms", "logo_url"])
        return

    all_values = worksheet.get_all_values()
    if not all_values or len(all_values) <= 1:
        return

    headers = [h.strip().lower() for h in all_values[0]]
    rows = all_values[1:]
    header_map = {h: idx for idx, h in enumerate(headers)}

    for idx, row in enumerate(rows, start=2):
        padded_row = row + [""] * (max(5, len(headers)) - len(row))

        # Column resolution supporting 5-column headers (with positional fallback)
        aliases_raw = padded_row[header_map.get("arabic_aliases", 0)].strip()
        primary_arabic = padded_row[header_map.get("primary_arabic_name", 1 if "primary_arabic_name" in header_map else 0)].strip()

        # Fallback if primary_arabic is in aliases cell
        if not primary_arabic and aliases_raw:
            primary_arabic = [a.strip() for a in re.split(r'[|;\n,]', aliases_raw) if a.strip()][0]

        if not primary_arabic:
            continue

        arabic_aliases = [a.strip() for a in re.split(r'[|;\n,]', aliases_raw) if a.strip()]

        name_en = padded_row[header_map.get("primary_english_name", header_map.get("english_name", 2))].strip()

        if type_label == "national":
            code = padded_row[header_map.get("code", 3)].strip()
            synonyms_raw = ""
            logo_url = padded_row[header_map.get("logo_url", 4)].strip()
            if code and not logo_url:
                logo_url = f"https://flagcdn.com/{code.lower()}.svg"
        else:
            code = ""
            synonyms_raw = padded_row[header_map.get("canonical_synonyms", 3)].strip()
            logo_url = padded_row[header_map.get("logo_url", 4)].strip()

        # "--" means "needs human review" — treat as Unknown so the script never uses bad data
        if not name_en or name_en == "--":
            name_en = "Unknown"
            code = ""

        canonical_synonyms = [s.strip() for s in re.split(r'[|;\n,]', synonyms_raw) if s.strip()]
        if type_label == "club" and name_en and name_en != "Unknown":
            for syn in canonical_synonyms:
                dynamic_synonyms[syn.lower()] = name_en

        translations[primary_arabic] = {
            "nameEn": name_en,
            "code": code,
            "logo_url": logo_url,
            "type": type_label,
            "row_num": idx,
            "sheet_name": sheet_name,
            "primary_arabic": primary_arabic,
            "arabic_aliases": arabic_aliases,
            "canonical_synonyms": canonical_synonyms,
            "original_aliases_cell": aliases_raw
        }


def load_team_translations(client, spreadsheet_name: str = "Streaming Dashboard") -> dict:
    """
    Fetches the team translations from '_cache_national_teams' and '_cache_clubs' worksheets.
    Registers dynamic canonical synonyms from the sheet.
    """
    sh = open_spreadsheet(client, spreadsheet_name)
    translations = {}
    dynamic_synonyms = {}
    _parse_translation_sheet(sh, "_cache_national_teams", "national", translations, dynamic_synonyms)
    _parse_translation_sheet(sh, "_cache_clubs", "club", translations, dynamic_synonyms)
    if dynamic_synonyms:
        set_canonical_synonyms(dynamic_synonyms)
    return translations


def find_existing_translation(name: str, team_translations: dict) -> dict:
    """
    Looks up a team name in the team_translations cache using exact matching,
    alias matching, Arabic normalization, and English canonical synonyms.
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
        aliases = v.get("arabic_aliases", [])
        if name in aliases:
            return v

    # Step 3: Arabic phonetic/orthographic normalization match
    best_candidate = None
    for v in team_translations.values():
        all_ar_names = [v["primary_arabic"]] + v.get("arabic_aliases", [])
        for alias in all_ar_names:
            if are_arabic_names_equivalent(name, alias):
                if _is_valid(v):
                    return v
                if best_candidate is None:
                    best_candidate = v

    # Step 4: English canonical name / synonyms matching
    for v in team_translations.values():
        if not _is_valid(v):
            continue
        name_en = v.get("nameEn", "")
        if name_en and are_english_teams_equivalent(name, name_en):
            return v
        for syn in v.get("canonical_synonyms", []):
            if are_english_teams_equivalent(name, syn):
                return v

    # Step 5: Fallback to direct placeholder if no valid translation was found
    if name in team_translations:
        return team_translations[name]

    return best_candidate


def update_team_aliases(client, alias_updates: list, spreadsheet_name: str = "Streaming Dashboard"):
    """Appends new aliases to Column A or backfills missing fields on existing rows in Google Sheets using a single batch update."""
    if not alias_updates:
        return

    sh = open_spreadsheet(client, spreadsheet_name)
    by_sheet = {}
    for update in alias_updates:
        row_num, sheet_name = update[0], update[1]
        col_idx, new_val = (update[2], update[3]) if len(update) == 4 else (1, update[2])
        if col_idx == 5:
            new_val = sanitize_sheet_image_url(new_val)
        by_sheet.setdefault(sheet_name, {})[(row_num, col_idx)] = new_val

    for sheet_name, updates in by_sheet.items():
        if not updates:
            continue
        try:
            worksheet = sh.worksheet(sheet_name)
        except Exception as e:
            logger.error(f"Sheets: Worksheet '{sheet_name}' not found: {e}")
            continue

        batch_data = []
        for (row_num, col_idx), new_val in updates.items():
            cell_a1 = gspread.utils.rowcol_to_a1(row_num, col_idx)
            batch_data.append({"range": cell_a1, "values": [[new_val]]})

        if batch_data:
            try:
                worksheet.batch_update(batch_data)
            except Exception as e:
                logger.error(f"Sheets: Failed batch updating '{sheet_name}': {e}")


def save_new_team_translations_separated(client, new_translations: list, spreadsheet_name: str = "Streaming Dashboard"):
    """Appends new 5-column translation rows to either '_cache_national_teams' or '_cache_clubs' worksheet."""
    if not new_translations:
        return

    sh = open_spreadsheet(client, spreadsheet_name)
    # national: [arabic_aliases, primary_arabic_name, primary_english_name, code, logo_url]
    national_rows = [["", t[0], t[1], t[2], sanitize_sheet_image_url(t[3])] for t in new_translations if t[4] == "national"]
    # clubs: [arabic_aliases, primary_arabic_name, primary_english_name, canonical_synonyms, logo_url]
    club_rows = [["", t[0], t[1], "", sanitize_sheet_image_url(t[3])] for t in new_translations if t[4] != "national"]

    for sheet_name, rows, headers in [
        ("_cache_national_teams", national_rows, ["arabic_aliases", "primary_arabic_name", "primary_english_name", "code", "logo_url"]),
        ("_cache_clubs", club_rows, ["arabic_aliases", "primary_arabic_name", "primary_english_name", "canonical_synonyms", "logo_url"])
    ]:
        if not rows:
            continue
        try:
            worksheet = sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="5")
            worksheet.append_row(headers)

        worksheet.append_rows(rows)
        logger.success(f"Sheets: Appended {len(rows)} new rows to '{sheet_name}'.")


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
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=OPENROUTER_TIMEOUT)
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            logger.success(f"Translation: API call successful using model: {model}.")
            return json.loads(_clean_llm_json(content))
        logger.error(f"Translation: Error with model {model}: HTTP {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"Translation: Exception with model {model}: {e}")
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
        logger.info(f"Translation: Requesting team details using model: {model}")
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

    logger.warning("Translation: All models failed. Adding placeholder rows for human review.")
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
        if m.get("team1_name") == name:
            logo_url = sanitize_sheet_image_url(m.get("team1_orig_img", ""))
            break
        elif m.get("team2_name") == name:
            logo_url = sanitize_sheet_image_url(m.get("team2_orig_img", ""))
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
    clean_logo = sanitize_sheet_image_url(logo_url)
    team_translations[name] = {
        "nameEn": "Unknown",
        "code": "",
        "logo_url": clean_logo,
        "type": "club",
        "primary_arabic": name,
        "arabic_aliases": [],
        "canonical_synonyms": [],
        "original_aliases_cell": ""
    }
    new_translations_list.append((name, "--", "", clean_logo, "club"))


def _is_valid_iso_code(code: str) -> bool:
    """Returns True if the code looks like a valid ISO national team code."""
    c = (code or "").strip().lower()
    return (len(c) == 2 and c.isalpha()) or c in ("gb-eng", "gb-sct", "gb-wls", "gb-nir")


def _find_matching_cached_team(name_en: str, code: str, team_translations: dict) -> dict | None:
    """
    Searches for an existing team in cache that matches the newly translated team.
    Strictly separates national teams (matched by ISO code) from clubs (matched by English name & canonical synonyms).
    """
    if not team_translations:
        return None

    clean_en = name_en.strip().lower()

    def _is_valid_entry(v: dict) -> bool:
        return v.get("nameEn") not in ("Unknown", "", None)

    # Pass 1: National teams — matched strictly by unique ISO 2-letter code
    if _is_valid_iso_code(code):
        for v in team_translations.values():
            if not _is_valid_entry(v) or v.get("type") != "national":
                continue
            if v.get("code", "").strip().lower() == code.strip().lower():
                return v

    # Pass 2: Clubs — exact English name match or canonical synonyms match
    for v in team_translations.values():
        if not _is_valid_entry(v) or v.get("type") == "national":
            continue
        if v.get("nameEn", "").strip().lower() == clean_en:
            return v
        if any(syn.strip().lower() == clean_en for syn in v.get("canonical_synonyms", [])):
            return v

    # Pass 3: Clubs — canonical English equivalence
    for v in team_translations.values():
        if not _is_valid_entry(v) or v.get("type") == "national":
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
        if matched_cached_team and matched_cached_team.get("type") != "national":
            canonical_en = matched_cached_team.get("nameEn", "")
            if not are_english_teams_equivalent(name_en, canonical_en):
                logger.warning(
                    f"Translation: Safety net caught collision — AI returned '{name_en}' but "
                    f"matched cached entry is '{canonical_en}'. Creating separate new entry instead."
                )
                matched_cached_team = None

        if matched_cached_team:
            canonical_en = matched_cached_team.get("nameEn", name_en)
            primary_ar = matched_cached_team.get("primary_arabic", "")
            aliases = matched_cached_team.get("arabic_aliases", [])

            # If name is different from primary Arabic and not in aliases, append to Col A
            if name != primary_ar and name not in aliases:
                orig_cell = matched_cached_team.get("original_aliases_cell", "")
                new_val = f"{orig_cell} | {name}" if orig_cell else name
                matched_cached_team["original_aliases_cell"] = new_val
                matched_cached_team.setdefault("arabic_aliases", []).append(name)
                row_num = matched_cached_team.get("row_num")
                sheet_name = matched_cached_team.get("sheet_name")
                if row_num and sheet_name:
                    alias_updates.append((row_num, sheet_name, 1, new_val))

            # Update logo in matched cached club (Col E, col_idx = 5) if fresh logo available
            clean_logo = sanitize_sheet_image_url(logo_url)
            if clean_logo and matched_cached_team.get("type") != "national":
                if matched_cached_team.get("logo_url") != clean_logo:
                    matched_cached_team["logo_url"] = clean_logo
                    row_num = matched_cached_team.get("row_num")
                    sheet_name = matched_cached_team.get("sheet_name")
                    if row_num and sheet_name:
                        alias_updates.append((row_num, sheet_name, 5, clean_logo))

            team_translations[name] = matched_cached_team
            logger.success(
                f"Translation: Auto-linked Arabic name '{name}' as alias for existing team '{canonical_en}'."
            )
            continue

        # Brand new team — add to cache and record new row for Google Sheets
        clean_logo = sanitize_sheet_image_url(logo_url)
        team_translations[name] = {
            "nameEn": name_en,
            "code": code,
            "logo_url": clean_logo,
            "type": team_type,
            "primary_arabic": name,
            "arabic_aliases": [],
            "canonical_synonyms": [],
            "original_aliases_cell": ""
        }
        new_translations_list.append((name, name_en, code, clean_logo, team_type))

    return new_translations_list, alias_updates
