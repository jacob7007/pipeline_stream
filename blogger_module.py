import os
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
import logger

API_BASE = "https://www.googleapis.com/blogger/v3"
BLOGGER_API_TIMEOUT = int(os.environ.get("BLOGGER_API_TIMEOUT", 10))

def get_blogger_session() -> AuthorizedSession:
    """
    Returns an authorized requests session for the Blogger API.
    Uses credentials from environment variables (BLOGGER_REFRESH_TOKEN,
    BLOGGER_CLIENT_ID, BLOGGER_CLIENT_SECRET).
    """
    refresh_token = os.environ.get("BLOGGER_REFRESH_TOKEN")
    client_id = os.environ.get("BLOGGER_CLIENT_ID")
    client_secret = os.environ.get("BLOGGER_CLIENT_SECRET")

    if not (refresh_token and client_id and client_secret):
        raise ValueError(
            "Blogger credentials not found in environment variables. "
            "Please set BLOGGER_REFRESH_TOKEN, BLOGGER_CLIENT_ID, and BLOGGER_CLIENT_SECRET in .env."
        )

    try:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret
        )
        return AuthorizedSession(creds)
    except Exception as e:
        raise RuntimeError(f"Failed to initialize credentials from Blogger env vars: {e}")

def fetch_post(session: AuthorizedSession, blog_id: str, post_id: str) -> dict:
    """Fetches a post's metadata and content."""
    url = f"{API_BASE}/blogs/{blog_id}/posts/{post_id}"
    resp = session.get(url, timeout=BLOGGER_API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def fetch_all_posts(session: AuthorizedSession, blog_id: str, status: str = None) -> dict:
    """
    Fetches all posts in the blog (returns list of posts with id, url, content, etc.).
    Supports comma-separated statuses (e.g. 'live,draft') by making multiple requests and merging.
    """
    statuses = [s.strip() for s in status.split(",")] if status else ["live"]
    merged_items = []
    first_resp = None

    url = f"{API_BASE}/blogs/{blog_id}/posts"
    for s in statuses:
        params = {"status": s, "maxResults": 500}
        try:
            resp = session.get(url, params=params, timeout=BLOGGER_API_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if not first_resp:
                first_resp = data
            items = data.get("items", [])
            merged_items.extend(items)
        except Exception as e:
            logger.error(f"Error fetching posts with status '{s}': {e}")
            if not first_resp:
                raise e

    if first_resp:
        first_resp["items"] = merged_items
        return first_resp
    return {"items": []}

def fetch_posts_map(session: AuthorizedSession, blog_id: str, status: str = "live,draft") -> dict:
    """Fetches all posts and returns a dict mapping post_id -> post data."""
    posts_resp = fetch_all_posts(session, blog_id, status=status)
    posts_list = posts_resp.get("items", [])
    return {p["id"]: p for p in posts_list}

def check_blog_status(session: AuthorizedSession, blog_id: str) -> tuple[bool, str]:
    """
    Checks if a blog exists and is not suspended/removed.
    Returns a tuple (is_active, error_message).
    """
    url = f"{API_BASE}/blogs/{blog_id}"
    try:
        resp = session.get(url, timeout=BLOGGER_API_TIMEOUT)
        if resp.status_code == 200:
            return True, ""

        if resp.status_code == 404:
            return False, "Blog not found (deleted or invalid ID)"
        if resp.status_code == 410:
            return False, "Blog has been permanently removed"
        if resp.status_code == 403:
            try:
                err_data = resp.json()
                reason = err_data.get("error", {}).get("errors", [{}])[0].get("reason", "")
                message = err_data.get("error", {}).get("message", "Access forbidden")
                if reason == "blogDeleted":
                    return False, "Blog was deleted or suspended by Blogger"
                return False, f"Access forbidden (suspended or private): {message}"
            except Exception as ex:
                logger.warning(f"Blogger: Failed to parse 403 error body: {ex}")
                return False, "Access forbidden (suspended or private)"

        return False, f"Blogger API error: HTTP {resp.status_code}"
    except Exception as e:
        if hasattr(e, 'response') and e.response is not None:
            code = e.response.status_code
            if code in [403, 404, 410]:
                return False, f"Blogger API error: HTTP {code}"
        return False, str(e)


def update_post(session: AuthorizedSession, blog_id: str, post_id: str, new_content: str) -> dict:
    """Updates/patches a post's content."""
    url = f"{API_BASE}/blogs/{blog_id}/posts/{post_id}"
    payload = {"content": new_content}
    resp = session.patch(url, json=payload, timeout=BLOGGER_API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def fetch_page(session: AuthorizedSession, blog_id: str, page_id: str) -> dict:
    """Fetches a page's metadata and content."""
    url = f"{API_BASE}/blogs/{blog_id}/pages/{page_id}"
    resp = session.get(url, timeout=BLOGGER_API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def check_page_status(session: AuthorizedSession, blog_id: str, page_id: str) -> tuple[bool, str]:
    """
    Checks if a page exists, is published and LIVE on Blogger (not DRAFT, deleted, or inaccessible).
    Returns a tuple (is_live, error_message).
    """
    if not page_id:
        return False, "Page ID is missing or empty"
    url = f"{API_BASE}/blogs/{blog_id}/pages/{page_id}"
    try:
        resp = session.get(url, timeout=BLOGGER_API_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status")
            if status in ["LIVE", None]:
                return True, ""
            return False, f"Page is in status '{status}' (not LIVE)"

        if resp.status_code == 404:
            return False, "Page not found (deleted or invalid ID)"
        if resp.status_code == 410:
            return False, "Page has been permanently removed"
        if resp.status_code == 403:
            return False, "Access forbidden (private or permissions issue)"

        return False, f"Blogger API error: HTTP {resp.status_code}"
    except Exception as ex:
        status_code = getattr(getattr(ex, 'response', None), 'status_code', 0)
        err_str = str(ex).lower()
        if status_code in [400, 403, 404, 410] or any(k in err_str for k in ["404", "410", "not found", "deleted"]):
            return False, f"Page is not available on Blogger (HTTP {status_code})"
        return False, str(ex)

def update_page(session: AuthorizedSession, blog_id: str, page_id: str, new_content: str) -> dict:
    """Updates/patches a page's content."""
    url = f"{API_BASE}/blogs/{blog_id}/pages/{page_id}"
    payload = {"content": new_content}
    resp = session.patch(url, json=payload, timeout=BLOGGER_API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


