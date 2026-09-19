import time
import requests
import logger

# In-memory remote state captured during Step 1 check_api_status
_remote_matches_data: list[dict] | None = None
_remote_channels_data: dict | None = None


def check_api_status(cloudflare_api_url: str) -> tuple[bool, str | None]:
    """
    Probes Cloudflare Worker API health via GET /matches and GET /channels.
    Captures the live remote state in memory for zero-redundant sync diffing in Step 3.
    Returns (True, None) on success, or (False, error_message) on failure.
    """
    global _remote_matches_data, _remote_channels_data
    if not cloudflare_api_url:
        return False, "CLOUDFLARE_API_URL is not configured."

    base_url = cloudflare_api_url.rstrip("/")
    endpoints = ["/matches", "/channels"]

    for ep in endpoints:
        probe_url = f"{base_url}{ep}"
        try:
            resp = requests.get(probe_url, timeout=7)
            if resp.status_code != 200:
                return False, f"Endpoint '{ep}' returned HTTP {resp.status_code}: {resp.text}"
            
            # Cache live remote data
            if ep == "/matches":
                try:
                    data = resp.json()
                    _remote_matches_data = data.get("matches", []) if isinstance(data, dict) else (data if isinstance(data, list) else None)
                except Exception:
                    _remote_matches_data = None
            elif ep == "/channels":
                try:
                    data = resp.json()
                    _remote_channels_data = data.get("channels", {}) if isinstance(data, dict) else None
                except Exception:
                    _remote_channels_data = None

        except Exception as e:
            return False, f"Connection failed to {probe_url}: {e}"

    return True, None


def sync_matches(
    matches_list: list[dict],
    cloudflare_api_url: str,
    sync_token: str,
    retries: int = 3
) -> bool:
    """
    Pushes clean matches schedule feed to POST {cloudflare_api_url}/sync/matches with Bearer token authentication.
    Skips network request if local clean matches match the live remote state from Step 1.
    """
    global _remote_matches_data
    if not cloudflare_api_url:
        logger.error("Cloudflare: CLOUDFLARE_API_URL is not configured.")
        return False

    clean_matches = [
        {k: v for k, v in m.items() if not k.startswith("_")}
        for m in matches_list
    ]

    # Smart Diff: If remote state is identical to new feed, skip network write
    if _remote_matches_data is not None and _remote_matches_data == clean_matches:
        skip_msg = f"{logger.COLOR_DARK_GRAY}Skipping update.{logger.COLOR_RESET}"
        logger.info(f"Cloudflare: Matches feed is already up to date. {skip_msg}")
        return True

    payload = {"matches": clean_matches}
    sync_url = f"{cloudflare_api_url.rstrip('/')}/sync/matches"
    headers = {
        "Authorization": f"Bearer {sync_token}",
        "Content-Type": "application/json"
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(sync_url, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                _remote_matches_data = clean_matches
                logger.success(f"Cloudflare: Synced matches feed ({len(clean_matches)} matches) successfully.")
                return True
            logger.warning(
                f"Cloudflare: Sync matches attempt {attempt}/{retries} failed (HTTP {resp.status_code}): {resp.text}"
            )
        except Exception as e:
            logger.warning(f"Cloudflare: Sync matches attempt {attempt}/{retries} connection error: {e}")
        if attempt < retries:
            time.sleep(2 ** (attempt - 1))

    logger.error(f"Cloudflare: Failed to sync matches feed after {retries} attempts.")
    return False


def sync_channels(
    channels_map: dict[str, list[dict]],
    cloudflare_api_url: str,
    sync_token: str,
    retries: int = 3
) -> bool:
    """
    Pushes multi-channel stream dictionary to POST {cloudflare_api_url}/sync/channels with Bearer token authentication.
    Skips network request if local channels map matches the live remote state from Step 1.
    """
    global _remote_channels_data
    if not cloudflare_api_url:
        logger.error("Cloudflare: CLOUDFLARE_API_URL is not configured.")
        return False

    # Smart Diff: If remote channels state is identical to new map, skip network write
    if _remote_channels_data is not None and _remote_channels_data == channels_map:
        skip_msg = f"{logger.COLOR_DARK_GRAY}Skipping update.{logger.COLOR_RESET}"
        logger.info(f"Cloudflare: Channels feed is already up to date. {skip_msg}")
        return True

    payload = {"channels": channels_map}
    sync_url = f"{cloudflare_api_url.rstrip('/')}/sync/channels"
    headers = {
        "Authorization": f"Bearer {sync_token}",
        "Content-Type": "application/json"
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(sync_url, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                _remote_channels_data = channels_map
                stream_count = sum(len(ch_list) for ch_list in channels_map.values())
                logger.success(
                    f"Cloudflare: Synced channels feed ({len(channels_map)} matches, {stream_count} streams) successfully."
                )
                return True
            logger.warning(
                f"Cloudflare: Sync channels attempt {attempt}/{retries} failed (HTTP {resp.status_code}): {resp.text}"
            )
        except Exception as e:
            logger.warning(f"Cloudflare: Sync channels attempt {attempt}/{retries} connection error: {e}")
        if attempt < retries:
            time.sleep(2 ** (attempt - 1))

    logger.error(f"Cloudflare: Failed to sync channels feed after {retries} attempts.")
    return False
