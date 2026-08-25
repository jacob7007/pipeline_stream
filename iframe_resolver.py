from datetime import datetime
import logger
from scrapers import SCRAPER_PLUGINS

# Build normalized plugin registry using short module names
PLUGIN_REGISTRY = {p.__name__.split(".")[-1]: p for p in SCRAPER_PLUGINS}


def resolve_match_channels(
    match_url: str,
    status_class: str,
    is_far_future: bool,
    plugin_name: str,
    proxies: dict = None,
    context: dict = None
) -> list[dict]:
    """
    Extracts multi-stream channels for a match from the appropriate scraper plugin.
    Returns an empty list for finished matches or matches >1h away.
    """
    if not match_url or status_class == "finished" or is_far_future:
        return []

    plugin = PLUGIN_REGISTRY.get(plugin_name)
    if plugin is None:
        logger.error(f"No plugin found in registry for: '{plugin_name}'")
        return []

    channels = []
    if hasattr(plugin, "extract_channels"):
        try:
            channels = plugin.extract_channels(match_url, proxies=proxies) or []
        except Exception as ex:
            logger.warning(f"Plugin '{plugin_name}' failed to extract channels: {ex}")

    if not channels and hasattr(plugin, "extract_iframe"):
        try:
            iframe_url = plugin.extract_iframe(match_url, proxies=proxies, context=context) or ""
            if iframe_url:
                channels = [{
                    "id": 1, "name": "Live 1", "quality": "HD", "type": "iframe", "url": iframe_url,
                    "sandbox": "allow-scripts allow-same-origin allow-presentation allow-forms"
                }]
        except Exception as e:
            logger.error(f"Plugin '{plugin_name}' raised error extracting iframe for {match_url}: {e}")

    return channels


# Alias for backward compatibility
resolve_match_iframe = resolve_match_channels

