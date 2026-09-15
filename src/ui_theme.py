"""Shared local-time rule for desktop window themes."""
from datetime import datetime


def is_daytime(now: datetime | None = None) -> bool:
    """Use the existing 07:00–18:00 daytime interval."""
    return 7 <= (now or datetime.now()).hour < 18
