import translation_manager
import sheets_module
import scraper_module
import logger
from utils import (
    format_to_human_time,
    get_status_priority,
    get_match_lookahead_hours,
    PipelineAbortError,
)


def _display_scraped_events(scraped_events: list):
    """Renders aligned preview of scraped match events."""
    if not scraped_events:
        return
    print()
    max_t1_len = max((len(ev['team1'].get('nameEn') or ev['team1']['nameAr']) for ev in scraped_events), default=15)
    max_t2_len = max((len(ev['team2'].get('nameEn') or ev['team2']['nameAr']) for ev in scraped_events), default=15)

    max_date_len = max((len(format_to_human_time(ev['time'])) for ev in scraped_events), default=14)
    max_status_len = 8

    for idx, ev in enumerate(scraped_events, 1):
        t1 = ev['team1'].get('nameEn') or ev['team1']['nameAr']
        t2 = ev['team2'].get('nameEn') or ev['team2']['nameAr']
        status = ev.get('status_class', 'upcoming').upper()
        ch_count = len(ev.get('channels', []))
        if ch_count:
            stream_part = f"{ch_count} live channel{'s' if ch_count != 1 else ''}"
        else:
            stream_part = f"{logger.COLOR_DARK_GRAY}(No stream){logger.COLOR_RESET}"

        aligned_teams = f"{t1:<{max_t1_len}} - {t2:<{max_t2_len}}"
        if status == "LIVE":
            status_styled = f"{logger.COLOR_GREEN}{logger.COLOR_BOLD}{status:<{max_status_len}}{logger.COLOR_RESET}"
        elif status == "UPCOMING":
            status_styled = f"{logger.COLOR_YELLOW}{status:<{max_status_len}}{logger.COLOR_RESET}"
        elif status == "FINISHED":
            status_styled = f"{logger.COLOR_DARK_GRAY}{status:<{max_status_len}}{logger.COLOR_RESET}"
        else:
            status_styled = f"{status:<{max_status_len}}"

        kickoff_str = format_to_human_time(ev['time'])
        aligned_date = f"{kickoff_str:<{max_date_len}}"
        print(f"  [{idx:2d}] {aligned_teams}  |  {aligned_date}  |  {status_styled}  |  {stream_part}")


def _persist_scraper_translations(sheets_client, spreadsheet_name: str, new_translations: list, alias_updates: list):
    """Persists translation additions and alias updates to Google Sheets."""
    if new_translations:
        print()
        logger.info(f"Sheets: Saving {len(new_translations)} new translations back...")
        try:
            translation_manager.save_new_team_translations_separated(sheets_client, new_translations, spreadsheet_name)
        except Exception as e:
            logger.error(f"Sheets: Error saving translations: {e}")
    if alias_updates:
        alias_only_count = sum(1 for u in alias_updates if (len(u) < 3 or u[2] == 1))
        if alias_only_count > 0:
            print()
            logger.info(f"Sheets: Saving {alias_only_count} alias update{'s' if alias_only_count != 1 else ''} back...")
        try:
            translation_manager.update_team_aliases(sheets_client, alias_updates, spreadsheet_name)
        except Exception as e:
            logger.error(f"Sheets: Error saving alias updates: {e}")


def run(
    sheets_client,
    spreadsheet_name: str,
    slots: list = None
) -> tuple[list, dict, dict]:
    """
    Step 4: Scrapes live matches from competitors, renders display preview, and persists translations.
    Returns (scraped_events, team_translations, updated_matches_cache).
    """
    team_translations = translation_manager.load_team_translations(sheets_client, spreadsheet_name)
    matches_cache = sheets_module.fetch_matches_cache(sheets_client, spreadsheet_name)

    try:
        scraped_events, new_translations, updated_matches_cache, alias_updates = scraper_module.scrape_live_matches(
            team_translations=team_translations,
            matches_cache=matches_cache,
            slots=slots,
            sheets_client=sheets_client,
            spreadsheet_name=spreadsheet_name,
        )
    except ConnectionError as ce:
        logger.error(f"Scraper: Connection failure fetching match sources: {ce}")
        raise PipelineAbortError(
            "SCRAPER : CONNECTION ERROR",
            f"Failed to connect to scraper sources: {ce}"
        )
    except Exception as e:
        logger.error(f"Scraper: Unexpected error during scraping: {e}")
        raise PipelineAbortError(
            "MATCH SCRAPING FAILED",
            f"Error during scraping: {e}"
        )

    if not scraped_events:
        logger.item("Scraper: 0 matches currently scheduled on competitor websites.")
        return [], team_translations, updated_matches_cache or matches_cache

    scraped_events.sort(key=lambda ev: (-get_status_priority(ev.get("status_class", "upcoming")), ev["time"]))
    lookahead_h = get_match_lookahead_hours()
    hours_str = f"{int(lookahead_h)}h" if lookahead_h.is_integer() else f"{lookahead_h}h"
    print()
    logger.item(f"Scraper: Scraped {len(scraped_events)} matches within the next {hours_str}:")
    _display_scraped_events(scraped_events)

    _persist_scraper_translations(sheets_client, spreadsheet_name, new_translations, alias_updates)
    return scraped_events, team_translations, updated_matches_cache
