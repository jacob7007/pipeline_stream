import sheets_module
import blogger_module
import logger
from utils import PipelineAbortError


def _verify_sheets_status(sheets_client, spreadsheet_name: str):
    """Verifies that the Google Sheets dashboard spreadsheet is accessible."""
    logger.info("Checking the dashboard sheets...")
    sheets_ok, sheets_err = sheets_module.check_sheets_status(sheets_client, spreadsheet_name)
    if not sheets_ok:
        logger.error(f"Sheets: Unexpected error opening spreadsheet '{spreadsheet_name}': {sheets_err}")
        raise PipelineAbortError(
            "INACCESSIBLE : DASHBOARD SHEETS",
            f"Google Sheets ({spreadsheet_name}) is not accessible: {sheets_err}"
        )
    logger.success("Dashboard sheets is active and accessible.")


def _verify_single_blog(blogger_session, label: str, target_name: str, blog_id: str):
    """Verifies single Blogger blog status."""
    print()
    logger.info(f"Checking {label}...")
    is_ok, err_msg = blogger_module.check_blog_status(blogger_session, blog_id)

    if not is_ok:
        logger.error(f"{label.capitalize()} is suspended or not available: {err_msg}")
        raise PipelineAbortError(
            f"INACCESSIBLE : {target_name}",
            f"{label.capitalize()} ({blog_id}) is suspended or not available: {err_msg}"
        )

    logger.success(f"{label.capitalize()} is active and accessible.")


def _verify_data_page(blogger_session, blog_data_id: str, data_page_id: str):
    """Verifies that the Blogger data page is accessible."""
    print()
    logger.info("Checking the data page...")
    page_ok, page_err = blogger_module.check_page_status(blogger_session, blog_data_id, data_page_id)
    if not page_ok:
        logger.error(f"The data page is suspended or not available: {page_err}")
        raise PipelineAbortError(
            "INACCESSIBLE : DATA PAGE",
            f"The data page ({data_page_id}) is not accessible: {page_err}"
        )
    logger.success("The data page is active and accessible.")


def run(
    sheets_client,
    blogger_session,
    spreadsheet_name: str,
    blog_id: str,
    blog_player_id: str,
    blog_data_id: str,
    data_page_id: str
):
    """
    Step 1: Verifies that Google Sheets dashboard and all Blogger websites are active and accessible.
    Raises PipelineAbortError if any check fails.
    """
    _verify_sheets_status(sheets_client, spreadsheet_name)

    blogs_to_check = [
        ("the blog website", "BLOG WEBSITE", blog_id),
        ("the data website", "DATA WEBSITE", blog_data_id),
    ]
    if blog_player_id and blog_player_id != blog_id:
        blogs_to_check.insert(1, ("the player website", "PLAYER WEBSITE", blog_player_id))

    for label, target_name, b_id in blogs_to_check:
        _verify_single_blog(blogger_session, label, target_name, b_id)

    _verify_data_page(blogger_session, blog_data_id, data_page_id)
