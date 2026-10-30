"""Compaction-deal call records and their first-event transitions."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


from .compaction_notices import CompactionNotice, IgnoredCompactionDeal, StartedRuleReading
from .declared_family import DeclaredFamily
from .field_codec import boundary_decoder
from .store_files import StoreGuard

class GateNotices(DeclaredFamily):
    """The existing call record's leading byte owns which first event remains to report."""

    @classmethod
    def marker(cls) -> bytes:
        return cls.declared_name.encode("ascii")

    @classmethod
    @boundary_decoder
    def from_marker(cls, marker: bytes) -> GateNotices:
        return cls.member_named((marker or QuietGate.marker()).decode("ascii"))()

    def refused(self) -> tuple[type[CompactionNotice], ...]:
        return ()

    def first_read(self) -> tuple[type[CompactionNotice], ...]:
        return ()


class QuietGate(GateNotices, declared_name="."):
    def refused(self) -> tuple[type[CompactionNotice], ...]:
        return (IgnoredCompactionDeal,)


class RefusedGate(GateNotices, declared_name="!"):
    def first_read(self) -> tuple[type[CompactionNotice], ...]:
        return (StartedRuleReading,)


@dataclass(frozen=True)
class RecordedGateCall:
    """The call's ordinal and pre-call state, used only while its counter is locked."""

    notices: GateNotices
    number: int
    record: BinaryIO

    def refused(self) -> tuple[type[CompactionNotice], ...]:
        self.record.seek(0)
        self.record.write(RefusedGate.marker())
        return self.notices.refused()



class DealtCalls(StoreGuard):
    """One byte per completed/refused call, retaining the existing .calls representation.

    The leading byte also records that a refusal occurred. Its remaining bytes are
    count padding, not another log to scan. Parallel refusals claim the first notice
    under this file's lock; ordinary counts remain one atomic append.
    """

    @property
    @abstractmethod
    def stem(self) -> Path:
        """The backend owns where its session record lives."""

    @property
    def calls(self) -> Path:
        return self.stem.with_suffix(".calls")

    def count_call(self) -> int:
        with self.calls.open("ab", buffering=0) as calls:
            calls.write(QuietGate.marker())
            return calls.tell()

    @contextmanager
    def recorded_call(self) -> Iterator[RecordedGateCall]:
        """Order the refusal/read transition and its publication against parallel calls.

        The first-read record and the refusal marker must observe the same order as
        their ordinals. Keep this session's lock through publication so the read notice
        cannot overtake the refusal it answers. Ordinary tool completions only append.
        """
        with self.write_guard(self.calls):
            fd = os.open(self.calls, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, "r+b", buffering=0) as record:
                notices = GateNotices.from_marker(record.read(1))
                yield RecordedGateCall(notices, self.count_call(), record)
