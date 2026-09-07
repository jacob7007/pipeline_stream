# Plugin facade for the FooTyy / TodayM widget family.
# All callers interact only with this interface — internal submodule structure is private.
from .matches import can_handle, parse_matches
from .channels import extract_channels

PLUGIN_NAME = "footyy"


def can_handle_url(url: str) -> bool:
    """Pre-flight check: determines whether a URL belongs to the FooTyy / TodayM widget family."""
    lower_u = url.lower()
    return any(k in lower_u for k in ("footyy", "todaym", "egy4"))


__all__ = ["PLUGIN_NAME", "can_handle_url", "can_handle", "parse_matches", "extract_channels"]

