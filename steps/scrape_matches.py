import translation_manager
import sheets_module
import scraper_module
import logger
from utils import format_to_human_time, get_status_priority, PipelineAbortError


def _display_scraped_events(scraped_events: list):
    """Renders aligned preview of scraped match events."""
    if not scraped_events:
        return
    print()
    max_t1_len = max((len(ev['team1'].get('nameEn') or ev['team1']['nameAr']) for ev in scraped_events), default=15)
    max_t2_len = max((len(ev['team2'].get('nameEn') or ev['team2']['nameAr']) for ev in scraped_events), default=15)

    for idx, ev in enumerate(scraped_events, 1):
        t1 = ev['team1'].get('nameEn') or ev['team1']['nameAr']
        t2 = ev['team2'].get('nameEn') or ev['team2']['nameAr']
        status = ev.get('status_class', 'unknown').upper()
        ch_count = len(ev.get('channels', []))
        if ch_count:
            stream_part = f"{ch_count} live channel{'s' if ch_count != 1 else ''}"
        else:
            stream_part = f"{logger.COLOR_DARK_GRAY}(No stream){logger.COLOR_RESET}"

        aligned_teams = f"{t1:<{max_t1_len}} - {t2:<{max_t2_len}}"
        if status == "LIVE":
            status_styled = f"{logger.COLOR_GREEN}{logger.COLOR_BOLD}{status:<11}{logger.COLOR_RESET}"
        elif status == "NOT-STARTED":
            status_styled = f"{logger.COLOR_YELLOW}{status:<11}{logger.COLOR_RESET}"
        elif status == "FINISHED":
            status_styled = f"{logger.COLOR_DARK_GRAY}{status:<11}{logger.COLOR_RESET}"
        else:
            status_styled = f"{status:<11}"

        kickoff_str = format_to_human_time(ev['time'])
        print(f"  [{idx:2d}] {aligned_teams}  |  {kickoff_str}  |  {status_styled}  |  {stream_part}")


def _persist_scraper_translations(sheets_client, spreadsheet_name: str, new_translations: list, alias_updates: list):
    """Persists translation additions and alias updates to Google Sheets."""
    if new_translations:
        print()
        logger.info(f"Saving {len(new_translations)} new translations back to Google Sheets...")
        try:
            translation_manager.save_new_team_translations_separated(sheets_client, new_translations, spreadsheet_name)
        except Exception as e:
            logger.error(f"Error saving translations: {e}")
    if alias_updates:
        print()
        logger.info(f"Saving {len(alias_updates)} alias updates back to Google Sheets...")
        try:
            translation_manager.update_team_aliases(sheets_client, alias_updates, spreadsheet_name)
        except Exception as e:
            logger.error(f"Error saving alias updates: {e}")


def run(
    sheets_client,
    spreadsheet_name: str
) -> tuple[list, dict, dict]:
    """
    Step 4: Scrapes live matches from competitors, renders display preview, and persists translations.
    Returns (scraped_events, team_translations, updated_matches_cache).
    """
    team_translations = translation_manager.load_team_translations(sheets_client, spreadsheet_name)
    matches_cache = sheets_module.fetch_matches_cache(sheets_client, spreadsheet_name)

    try:
        scraped_events, new_translations, updated_matches_cache, alias_updates = scraper_module.scrape_live_matches(
            team_translations=team_translations, matches_cache=matches_cache
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
        logger.error("Scraper: 0 matches found across all sources.")
        raise PipelineAbortError(
            "SCRAPER : 0 MATCHES FOUND",
            "Scraper returned 0 matches across all sources. Halting pipeline to protect active stream slots."
        )

    scraped_events.sort(key=lambda ev: (-get_status_priority(ev.get("status_class", "not-started")), ev["time"]))
    logger.item(f"Scraped {len(scraped_events)} total matches:")
    _display_scraped_events(scraped_events)

    _persist_scraper_translations(sheets_client, spreadsheet_name, new_translations, alias_updates)
    return scraped_events, team_translations, updated_matches_cache
