# Plugin facade for the FooTyy / TodayM widget family.
# All callers interact only with this interface — internal submodule structure is private.
from .matches import can_handle, parse_matches
from .channels import extract_channels

__all__ = ["can_handle", "parse_matches", "extract_channels"]
