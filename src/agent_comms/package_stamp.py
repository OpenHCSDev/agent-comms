"""Source checkouts use their own repository; builds replace this module."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .core_stamp import CoreStamp
from .repo import Repo


@dataclass(frozen=True)
class CheckoutStamp(CoreStamp):
    value: str

    def commit(self) -> str:
        return Repo.git(self.value, "rev-parse", "HEAD").strip()

    def producer_digests(
        self, declared: Mapping[str, Callable[[], str]], installed: Callable[[], dict[str, str]],
    ) -> dict[str, str]:
        return {name: digest() for name, digest in declared.items()}


STAMP = CheckoutStamp(str(Path(__file__).resolve().parents[2]))
