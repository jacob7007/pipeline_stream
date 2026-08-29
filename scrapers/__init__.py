import logger
from . import from_yallashoot
from . import footyy

# Plugin contract — every scraper plugin must implement:
#   can_handle(soup: BeautifulSoup) -> bool
#   parse_matches(soup: BeautifulSoup, source_url: str, default_date: str, source_tz: str | int = None) -> list[dict]
#   extract_channels(match_url: str, proxies: dict = None) -> list[dict]

# Validates plugins at startup so incomplete plugins are skipped immediately rather than failing mid-run
def validate_plugins(plugins: list) -> list:
    valid_plugins = []
    required_methods = ("can_handle", "parse_matches", "extract_channels")

    for plugin in plugins:
        plugin_name = getattr(plugin, "__name__", str(plugin))
        missing = [method for method in required_methods if not hasattr(plugin, method)]

        if missing:
            missing_str = ", ".join(missing)
            logger.error(f"Plugin '{plugin_name}' is missing required method(s): {missing_str}. Skipping plugin.")
        else:
            valid_plugins.append(plugin)

    return valid_plugins

# Registry of all active and validated scraper plugins
SCRAPER_PLUGINS = validate_plugins([from_yallashoot, footyy])
