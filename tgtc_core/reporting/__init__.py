"""Weekly reporting for the rebuilt core.

The report is measured from the core database -- the provider and delivery receipts
the production runs wrote themselves -- so it needs no run artifacts on a container
volume and survives the container that produced them. The legacy file-based layer
(``weekly_report/``) reads artifacts that no longer exist; this package replaces it
for the rebuilt pipeline and keeps the same agreed window: Friday 00:00 to the
following Friday 00:00, America/Los_Angeles, end exclusive.
"""

from .window import PACIFIC_TZ_NAME, ReportWindow, explicit_window, is_due, partial_window, weekly_window

__all__ = [
    "PACIFIC_TZ_NAME", "ReportWindow", "explicit_window", "is_due", "partial_window", "weekly_window",
]
