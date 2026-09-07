# Engine facade for the YallaShoot template family.
# Matches are parsed using the shared engine (matches.py).
# Stream channels are routed strictly to explicit website sub-folders (e.g. sir_tv).
# There is NO default/fallback channel extractor.
from . import matches
from . import sir_tv
from .matches import can_handle, parse_matches

PLUGIN_NAME = "yallashoot"

REGISTERED_WEBSITES = [
    sir_tv,
]


def can_handle_url(url: str) -> bool:
    """Pre-flight check: determines if a URL belongs to any registered website under the YallaShoot engine."""
    return any(site.can_handle_url(url) for site in REGISTERED_WEBSITES)


def get_source_name(url: str) -> str:
    """Returns the namespaced source identifier, e.g. 'yallashoot/sir-tv'."""
    for site in REGISTERED_WEBSITES:
        if site.can_handle_url(url):
            return f"{PLUGIN_NAME}/{site.SITE_SLUG}"
    return PLUGIN_NAME


def extract_channels(match_url: str, proxies: dict = None) -> list[dict]:
    """Routes channel extraction to the matching website's channels.py. Returns [] if website is not registered."""
    for site in REGISTERED_WEBSITES:
        if site.can_handle_url(match_url):
            return site.channels.extract_channels(match_url, proxies=proxies)
    return []


__all__ = [
    "PLUGIN_NAME",
    "can_handle_url",
    "can_handle",
    "parse_matches",
    "extract_channels",
    "get_source_name",
    "REGISTERED_WEBSITES",
]
