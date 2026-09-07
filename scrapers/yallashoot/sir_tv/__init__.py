SITE_SLUG = "sir-tv"


def can_handle_url(url: str) -> bool:
    """Pre-flight check: determines whether a URL belongs to the Sir-TV / YasirTV domain family."""
    lower_u = url.lower()
    return any(k in lower_u for k in ("sir-tv", "tvsir", "yasirtv"))


__all__ = ["SITE_SLUG", "can_handle_url"]
