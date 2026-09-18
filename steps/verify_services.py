import sheets_module
import logger
import cloudflare_module
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


def _verify_cloudflare_api(cloudflare_api_url: str):
    """Verifies that the Cloudflare Worker API is active and accessible via probe GET /matches and GET /channels."""
    if not cloudflare_api_url:
        logger.error("Cloudflare: CLOUDFLARE_API_URL is missing or empty.")
        raise PipelineAbortError(
            "MISSING CONFIG : CLOUDFLARE API",
            "CLOUDFLARE_API_URL environment variable is not configured."
        )

    logger.info("Checking Cloudflare API endpoints /matches, /channels...")
    is_ok, err_msg = cloudflare_module.check_api_status(cloudflare_api_url)
    if not is_ok:
        logger.error(f"Cloudflare API health check failed: {err_msg}")
        raise PipelineAbortError(
            "INACCESSIBLE : CLOUDFLARE API",
            f"Failed to connect to Cloudflare Worker API: {err_msg}"
        )
    logger.success("Cloudflare API endpoints are active and accessible.")


def run(sheets_client, spreadsheet_name: str, cloudflare_api_url: str):
    """
    Step 1: Verifies that Google Sheets dashboard and Cloudflare API are active and accessible.
    Raises PipelineAbortError if any check fails.
    """
    _verify_sheets_status(sheets_client, spreadsheet_name)
    print()
    _verify_cloudflare_api(cloudflare_api_url)

