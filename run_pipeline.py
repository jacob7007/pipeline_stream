import sys
import argparse
from datetime import datetime
from utils import (
    load_env,
    configure_utf8,
    format_to_human_time,
    get_allowed_chat_ids,
    get_blog_id,
    get_blog_player_id,
    get_blog_data_id,
    get_data_page_id,
    get_spreadsheet_name,
    get_telegram_bot_token,
    broadcast_telegram,
    PipelineAbortError
)

load_env()

import sheets_module
import blogger_module
import logger
from steps import (
    verify_services,
    fetch_slots,
    verify_slots,
    scrape_matches,
    reconcile_slots,
    sync_data
)

configure_utf8()

BLOG_ID = get_blog_id() or get_blog_player_id()
BLOG_PLAYER_ID = get_blog_player_id()
BLOG_DATA_ID = get_blog_data_id()
DATA_PAGE_ID = get_data_page_id()
SPREADSHEET_NAME = get_spreadsheet_name()


def _parse_cli_args():
    """Parses command line arguments for the pipeline runner."""
    parser = argparse.ArgumentParser(description="Football Stream Automation Pipeline")
    parser.add_argument("--sheet", type=str, default=SPREADSHEET_NAME, help="Google Sheets spreadsheet name or ID")
    parser.add_argument("--telegram-report-chat-id", type=str, default="", help="Telegram chat ID for reconciliation report")
    return parser.parse_args()


def _init_clients():
    """Initializes and authenticates Google Sheets and Blogger API clients."""
    try:
        sheets_client = sheets_module.get_gspread_client()
        logger.success("Google Sheets client authorized.")
    except Exception as e:
        logger.error(f"Error initializing Google Sheets client: {e}")
        raise PipelineAbortError("GOOGLE SHEETS CLIENT INITIALIZATION FAILED", str(e))

    try:
        blogger_session = blogger_module.get_blogger_session()
        logger.success("Blogger API session authorized.")
    except Exception as e:
        logger.error(f"Error initializing Blogger API session: {e}")
        raise PipelineAbortError("BLOGGER API CLIENT INITIALIZATION FAILED", str(e))

    return sheets_client, blogger_session


def main():
    args = _parse_cli_args()
    telegram_token = get_telegram_bot_token()
    allowed_chat_ids = get_allowed_chat_ids(args.telegram_report_chat_id)
    send_report_chat_ids = [args.telegram_report_chat_id] if args.telegram_report_chat_id else []

    try:
        if not BLOG_PLAYER_ID or not BLOG_DATA_ID or not DATA_PAGE_ID:
            logger.error("Missing required environment variables: BLOG_PLAYER_ID, BLOG_DATA_ID, or DATA_PAGE_ID")
            raise PipelineAbortError(
                "MISSING REQUIRED ENVIRONMENT VARIABLES",
                "Check BLOG_PLAYER_ID, BLOG_DATA_ID, DATA_PAGE_ID"
            )

        logger.step_header("START", "Starting Stream Pipeline")
        human_time = format_to_human_time(datetime.now().isoformat())
        print(f"  {logger.COLOR_BLUE}ℹ{logger.COLOR_RESET}  Time : {human_time}")

        # Step 1: Initializing API & verifying status
        logger.step_header("1/6", "Initializing API & verifying status")
        sheets_client, blogger_session = _init_clients()
        print()
        verify_services.run(
            sheets_client, blogger_session, args.sheet,
            BLOG_ID, BLOG_PLAYER_ID, BLOG_DATA_ID, DATA_PAGE_ID
        )

        # Step 2: Fetching stream slots
        logger.step_header("2/6", "Fetching stream slots")
        slots = fetch_slots.run(sheets_client, args.sheet)

        # Step 3: Validating slots
        logger.step_header("3/6", "Validating slots")
        valid_slots, newly_invalid_slots, restored_slots = verify_slots.run(
            blogger_session, slots, BLOG_ID, BLOG_PLAYER_ID,
            telegram_token, allowed_chat_ids
        )

        # Step 4: Scraping competitors live matches
        logger.step_header("4/6", "Scraping competitors live matches")
        scraped_events, team_translations, matches_cache = scrape_matches.run(
            sheets_client, args.sheet, slots=valid_slots
        )

        if not scraped_events:
            print()
            logger.info("Scraper found 0 matches. Skipping slot reconciliation to protect active stream slots.")
            logger.step_header("SYNC", "Syncing Data Website directly from cache")
            if newly_invalid_slots or restored_slots:
                sheets_module.update_changed_slots(sheets_client, newly_invalid_slots + restored_slots, args.sheet)
            sheets_module.save_matches_cache(sheets_client, matches_cache, args.sheet)
            active_matches_list = sync_data.assemble_matches_feed(matches_cache)
            logger.info(f"Active matches formatted for the data website ({len(active_matches_list)} matches):")
            sync_data.display_data_matches(active_matches_list)
            sync_data.sync_data_page(blogger_session, BLOG_DATA_ID, DATA_PAGE_ID, matches_cache, skip_display=True, active_matches_list=active_matches_list)
            logger.pipeline_end("Stream pipeline completed safely (0 matches scheduled, cache synced)", is_error=False)
            return

        # Step 5: Reconciling & updating slots
        logger.step_header("5/6", "Reconciling & updating slots")
        all_changed_slots, slot_actions, public_posts_map = reconcile_slots.run(
            blogger_session, valid_slots, newly_invalid_slots, restored_slots,
            scraped_events, BLOG_ID, BLOG_PLAYER_ID
        )

        # Step 6: Syncing DB & updating data website
        logger.step_header("6/6", "Syncing DB & updating data website")
        sync_data.run(
            sheets_client, blogger_session, all_changed_slots, slots,
            scraped_events, matches_cache, public_posts_map, slot_actions,
            args.sheet, BLOG_ID, BLOG_DATA_ID, DATA_PAGE_ID,
            telegram_token, send_report_chat_ids
        )

        logger.pipeline_end("Stream pipeline completed successfully", is_error=False)

    except PipelineAbortError as err:
        if telegram_token and allowed_chat_ids:
            details_part = f"\n\nDetails: {err.details}" if err.details else ""
            alert_text = f"🚨 Pipeline Alert!\n\nReason: {err.reason}{details_part}"
            broadcast_telegram(telegram_token, allowed_chat_ids, alert_text)
        logger.pipeline_end(err.reason, is_error=True)
        sys.exit(1)
    except KeyboardInterrupt:
        print()
        logger.error("Pipeline interrupted by user (SIGINT).")
        logger.pipeline_end("MANUAL STOP (CTRL + C)", is_error=True)
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected pipeline failure: {e}")
        logger.pipeline_end(f"PIPELINE CRASHED: {e}", is_error=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
