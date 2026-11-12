"""Proxy mileage-filing subsystem.

A public-transport statistics operator uses these endpoints to file monthly
line mileage through a regulator portal that only exposes a web UI.  All
browser interaction goes through Playwright; persistence uses SQLite and a
single worker guarantees that only one filing task runs at a time.
"""

from .config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
