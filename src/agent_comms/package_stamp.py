"""Source checkouts use their own repository; builds replace this module."""

from pathlib import Path

from .core_stamp import CheckoutStamp

STAMP = CheckoutStamp(str(Path(__file__).resolve().parents[2]))
