"""
Request-path helpers.

The heavy cleanup that used to run inline on every dashboard load (expiring
stale PENDING requests + blocking Cloudinary deletes in a loop) now lives in
`maintenance.py` and runs in the background scheduler / `run_maintenance`
command. On the request path we only do the cheap, single-query cooldown reset
so students never see a stale cooldown.
"""
import logging

import cloudinary.uploader

from .maintenance import fast_cooldown_reset

logger = logging.getLogger("nodue")


def save_receipt(file):
    """Upload a receipt to Cloudinary and return its secure URL."""
    result = cloudinary.uploader.upload(
        file,
        folder="no_due_receipts",
        resource_type="auto",   # PDF + image both
    )
    return result["secure_url"]


def reset_expired_no_dues(no_due_col=None):
    """
    Kept for backward compatibility with existing view imports.

    Now a thin, cheap wrapper: it only clears elapsed 24h cooldowns (one bulk
    update). Expiring stale PENDING requests and deleting orphaned receipts is
    handled asynchronously by the maintenance scheduler.
    """
    fast_cooldown_reset()
