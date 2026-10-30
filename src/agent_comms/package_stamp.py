"""Source checkouts use their own repository; builds replace this module."""

from dataclasses import dataclass
from pathlib import Path

from .core_stamp import CoreStamp
from .repo import Repo


@dataclass(frozen=True)
class CheckoutStamp(CoreStamp):
    """A checkout's commit is whatever its repository holds when asked."""

    def commit(self) -> str:
        return Repo.git(self.value, "rev-parse", "HEAD").strip()


STAMP: CoreStamp = CheckoutStamp(str(Path(__file__).resolve().parents[2]))
