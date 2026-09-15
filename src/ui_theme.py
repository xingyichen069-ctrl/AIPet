"""Shared local-time rule for desktop window themes."""
from datetime import datetime

TOUHOU_DAY = {
    'top': '#fffaf1', 'bottom': '#f5eddf',
    'vermilion': '#a9403b', 'gold': '#c39b65',
}
TOUHOU_NIGHT = {
    'top': '#191e2a', 'bottom': '#121723',
    'vermilion': '#c86760', 'gold': '#b99969',
}


def touhou_palette(day):
    return TOUHOU_DAY if day else TOUHOU_NIGHT


def is_daytime(now: datetime | None = None) -> bool:
    """Use the existing 07:00–18:00 daytime interval."""
    return 7 <= (now or datetime.now()).hour < 18
