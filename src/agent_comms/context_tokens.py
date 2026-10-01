"""Segment estimates come from the pinned Pi estimator, never a second formula."""

from __future__ import annotations
import asyncio
from dataclasses import dataclass
from pathlib import Path
from .pi_helper import PiHelper
from .native_package import verify_native_package


@dataclass(frozen=True)
class ContextTokenRequest:
    package: str
    segments: tuple[str, ...]


@dataclass(frozen=True)
class ContextTokenCounts:
    counter: str
    counts: tuple[int, ...]


class ContextTokensHelper(PiHelper):
    script = Path(__file__).with_name("_pi_helpers") / "context_tokens.mjs"
    request = ContextTokenRequest
    result = ContextTokenCounts


class NativeTokenCounter:
    def __init__(self, package: Path):
        self.package = package

    def measure(self, segments: tuple[str, ...]) -> ContextTokenCounts:
        verify_native_package(self.package)
        return asyncio.run(
            ContextTokensHelper.run(
                ContextTokenRequest(str(self.package), segments), cwd=Path.cwd()
            )
        )
