import sys
import argparse
from datetime import datetime
from utils import (
    load_env,
    configure_utf8,
    format_to_human_time,
    get_allowed_chat_ids,
    get_spreadsheet_name,
    get_telegram_bot_token,
    get_cloudflare_api_url,
    get_cloudflare_sync_token,
    get_player_base_url,
    broadcast_telegram,
    PipelineAbortError
)

load_env()

import sheets_module
import logger
import channel_resolver
from steps import (
    verify_services,
    scrape_matches,
    sync_data
)

configure_utf8()

SPREADSHEET_NAME = get_spreadsheet_name()
CLOUDFLARE_API_URL = get_cloudflare_api_url()
CLOUDFLARE_SYNC_TOKEN = get_cloudflare_sync_token()
PLAYER_BASE_URL = get_player_base_url()


def _parse_cli_args():
    """Parses command line arguments for the pipeline runner."""
    parser = argparse.ArgumentParser(description="Football Stream Automation Pipeline")
    parser.add_argument("--sheet", type=str, default=SPREADSHEET_NAME, help="Google Sheets spreadsheet name or ID")
    parser.add_argument("--sync", action="store_true", help="Direct fast sync from Google Sheets without scraping")
    parser.add_argument("--telegram-report-chat-id", type=str, default="", help="Telegram chat ID for alert notifications")
    return parser.parse_args()


def _init_clients():
    """Initializes and authenticates the Google Sheets API client."""
    try:
        sheets_client = sheets_module.get_gspread_client()
        logger.success("Google Sheets client authorized.")
        return sheets_client
    except Exception as e:
        logger.error(f"Error initializing Google Sheets client: {e}")
        raise PipelineAbortError("GOOGLE SHEETS CLIENT INITIALIZATION FAILED", str(e))


def _send_domain_alerts(alerts: list, telegram_token: str, chat_ids: list, player_base_url: str) -> None:
    """Sends a single batch Telegram notification for all '--' iframe domains found this run.
    Each alert is enriched with the match player URL.
    """
    if not alerts or not telegram_token or not chat_ids:
        return

    base_url = (player_base_url or "").rstrip("?")
    lines = []
    for i, alert in enumerate(alerts, start=1):
        domain       = alert.get("domain", "unknown")
        match_name   = alert.get("match_name", "Unknown Match")
        channel_name = alert.get("channel_name", "Unknown Channel")
        event_id     = alert.get("event_id", "")

        post_url = f"{base_url}?match={event_id}" if (event_id and base_url) else ""

        line = f"{i}. {domain}\n   Match: {match_name}\n   Channel: {channel_name}"
        if post_url:
            line += f"\n   🔗 {post_url}"
        lines.append(line)

    body = "\n\n".join(lines)
    message = (
        "⚠️ Unverified iframe domains — Human Review Needed\n\n"
        f"{body}\n\n"
        "Open _cache_domains in Sheets and set each to OK or NO."
    )
    broadcast_telegram(telegram_token, chat_ids, message)
    logger.info(f"Telegram: Sent domain alert for {len(alerts)} unverified domain(s).")


def main():
    args = _parse_cli_args()
    telegram_token = get_telegram_bot_token()
    allowed_chat_ids = get_allowed_chat_ids(args.telegram_report_chat_id)

    try:
        if not CLOUDFLARE_API_URL or not CLOUDFLARE_SYNC_TOKEN:
            logger.error("Missing required environment variables: CLOUDFLARE_API_URL or CLOUDFLARE_SYNC_TOKEN")
            raise PipelineAbortError(
                "MISSING REQUIRED ENVIRONMENT VARIABLES",
                "Check CLOUDFLARE_API_URL and CLOUDFLARE_SYNC_TOKEN in .env"
            )

        logger.step_header("START", "Starting Stream Pipeline")
        human_time = format_to_human_time(datetime.now().isoformat())
        print(f"  {logger.COLOR_BLUE}ℹ{logger.COLOR_RESET}  Time : {human_time}")

        if args.sync:
            from steps.sync_data import run_sync
            sheets_client = _init_clients()
            ok = run_sync(
                sheets_client=sheets_client,
                spreadsheet_name=args.sheet,
                cloudflare_api_url=CLOUDFLARE_API_URL,
                sync_token=CLOUDFLARE_SYNC_TOKEN,
                player_base_url=PLAYER_BASE_URL,
            )
            sys.exit(0 if ok else 1)

        # Step 1: Initializing API & verifying status
        logger.step_header("1/3", "Initializing API & verifying status")
        sheets_client = _init_clients()
        print()
        verify_services.run(sheets_client, args.sheet, CLOUDFLARE_API_URL)

        # Step 2: Scraping competitors live matches
        logger.step_header("2/3", "Scraping competitors live matches")
        scraped_events, team_translations, matches_cache = scrape_matches.run(
            sheets_client, args.sheet
        )

        # Step 3: Syncing DB & broadcasting to Cloudflare
        logger.step_header("3/3", "Syncing DB & broadcasting to Cloudflare")
        sync_data.run(
            sheets_client, args.sheet, matches_cache,
            CLOUDFLARE_API_URL, CLOUDFLARE_SYNC_TOKEN, PLAYER_BASE_URL
        )

        # Flush any new domain validator results to the _cache_domains sheet.
        channel_resolver.flush_domain_cache(sheets_client, args.sheet)

        # Send one batch Telegram alert for all '--' (inconclusive) domains discovered this run.
        _send_domain_alerts(
            channel_resolver.get_pending_alerts(),
            telegram_token,
            allowed_chat_ids,
            player_base_url=PLAYER_BASE_URL,
        )

        logger.pipeline_end("Stream pipeline completed successfully", is_error=False)

    except PipelineAbortError as err:
        if err.details:
            logger.error(f"Details: {err.details}")
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
