from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

DEFAULT_TIMEZONE = "UTC"


def resolve_timezone(name: str | None) -> str:
    """An IANA name the caller sent, or UTC.

    Validated here rather than passed straight through: an unknown name makes the
    aggregation itself fail, which would turn a bad query parameter into a broken page.
    """
    if not name:
        return DEFAULT_TIMEZONE
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.bind(timezone=name).warning("Unknown timezone, falling back to UTC")
        return DEFAULT_TIMEZONE
    return name
