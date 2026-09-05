"""
REVIVE Notifications & Alerts

Scans a completed pipeline run and surfaces the events worth a
person's attention: systemic PSR alerts, A2A recoveries that beat a
policy block, and high-value stopped cases.
"""

from .notifications import (
    Notification,
    generate_notifications,
)

__all__ = [
    "Notification",
    "generate_notifications",
]