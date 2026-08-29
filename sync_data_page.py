import sys
import argparse
import json
from utils import (
    load_env,
    configure_utf8,
    get_blog_data_id,
    get_data_page_id,
    get_spreadsheet_name
)

load_env()

import sheets_module
import blogger_module
import logger
from steps.sync_data import assemble_matches_feed, sync_data_page

configure_utf8()


def _parse_args():
    """Parses CLI arguments for the standalone data page sync tool."""
    parser = argparse.ArgumentParser(description="Sync match feed to Data Website directly from Sheets Cache")
    parser.add_argument("--sheet", type=str, default=get_spreadsheet_name(), help="Google Sheets spreadsheet name")
    parser.add_argument("--export-file", type=str, default="", help="Path to export raw matches.json feed")
    return parser.parse_args()


def main():
    args = _parse_args()
    blog_data_id = get_blog_data_id()
    data_page_id = get_data_page_id()

    if not blog_data_id or not data_page_id:
        logger.error("Missing BLOG_DATA_ID or DATA_PAGE_ID environment variables.")
        sys.exit(1)

    logger.step_header("DATA SYNC", "Syncing Data Website Feed")
    try:
        sheets_client = sheets_module.get_gspread_client()
        blogger_session = blogger_module.get_blogger_session()
        logger.success("Authenticated with Google Sheets and Blogger.")
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        sys.exit(1)

    matches_cache = sheets_module.fetch_matches_cache(sheets_client, args.sheet)
    if not matches_cache:
        logger.warning(f"No matches found in cache on sheet '{args.sheet}'.")

    if args.export_file:
        feed = assemble_matches_feed(matches_cache)
        with open(args.export_file, "w", encoding="utf-8") as f:
            json.dump(feed, f, indent=2, ensure_ascii=False)
        logger.success(f"Exported {len(feed)} matches to '{args.export_file}'.")

    success = sync_data_page(blogger_session, blog_data_id, data_page_id, matches_cache)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
