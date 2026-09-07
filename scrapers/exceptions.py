"""
Custom exception classes for competitor scraper plugins.
Enables scraper plugins to raise explicit failures (e.g. missing widgets,
anti-bot shields, structural DOM changes) instead of returning empty lists.
"""


class ScraperPluginError(Exception):
    """Raised when a scraper plugin encounters a fatal parsing or structural error."""
    def __init__(self, message: str, plugin_name: str = ""):
        super().__init__(message)
        self.plugin_name = plugin_name
        self.message = message
