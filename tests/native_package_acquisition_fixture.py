"""Trace native-package tree hashing and content reads, in every thread."""

from __future__ import annotations

import io
import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import CodeType, FrameType


@dataclass
class PackageTrace:
    digest_code: CodeType
    tree_hash_calls: int = 0
    content_reads: int = 0

    @contextmanager
    def active(self) -> Iterator[PackageTrace]:
        """Observe acquisitions in the coordinator's worker threads as well as its caller."""
        previous_thread = threading.getprofile()
        previous_caller = sys.getprofile()
        threading.setprofile_all_threads(self)
        try:
            yield self
        finally:
            threading.setprofile_all_threads(previous_thread)
            sys.setprofile(previous_caller)

    def __call__(self, frame: FrameType, event: str, argument: object) -> None:
        # CPython owns these external profiling events; compare function identities.
        if event == "call" and frame.f_code is self.digest_code:
            self.tree_hash_calls += 1
        elif event == "c_call" and argument in (io.open, os.open, os.read):
            self.content_reads += 1
