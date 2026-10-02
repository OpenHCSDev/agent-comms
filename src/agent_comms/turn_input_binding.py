"""Durable original binding and its selected-summary one-shot authority."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .selected_source import SessionRevision, SessionRevisionUnavailable
from .compaction_send_admission import native_input_admitted
from .errors import RelationViolationError
from .selected_source import SelectedAdmissionSource
from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission
from .thread_identity import TurnId

if TYPE_CHECKING:
    from .input_disposition import InputDispositions
    from .input_drain import InputDrain
    from .threads import Thread


@dataclass(frozen=True, kw_only=True)
class TurnInputBinding(ABC):
    root: Path
    dispositions: InputDispositions

    @abstractmethod
    def invalidate(self) -> None: ...

    @abstractmethod
    def available(self, current: Thread) -> bool: ...

    @abstractmethod
    def bind(
        self,
        *,
        current: Thread,
        keys: tuple[str, ...],
        admission: int,
        turn: TurnId,
        native_id: str,
        text: str,
        already_bound: bool,
    ) -> bool: ...


class OrdinaryTurnBinding(TurnInputBinding):
    def invalidate(self) -> None:
        pass

    def available(self, current: Thread) -> bool:
        return native_input_admitted(self.root, current.session_file)

    def bind(
        self,
        *,
        current: Thread,
        keys: tuple[str, ...],
        admission: int,
        turn: TurnId,
        native_id: str,
        text: str,
        already_bound: bool,
    ) -> bool:
        document = self.dispositions.read()
        if any(not document.lookup(key).unresolved
               or not document.lookup(key).matches_admission(admission) for key in keys):
            return False
        if already_bound:
            return all(document.lookup(key).matches_native(
                native_id=native_id, turn_id=turn.value, text=text
            ) for key in keys)
        return self.dispositions.bind_originals(
            keys, admission=admission, turn_id=turn.value, native_id=native_id, text=text
        )


@dataclass(frozen=True, kw_only=True)
class SelectedOriginalBinding(TurnInputBinding):
    selected: SelectedSummaryAdmission
    inputs: InputDrain

    def invalidate(self) -> None:
        self.selected.invalidate()

    def available(self, current: Thread) -> bool:
        # Only this exact ephemeral capability may bypass the selected journal barrier.
        return True

    def bind(
        self,
        *,
        current: Thread,
        keys: tuple[str, ...],
        admission: int,
        turn: TurnId,
        native_id: str,
        text: str,
        already_bound: bool,
    ) -> bool:
        # The canonical revision reader already owns absent/unreadable sessions;
        # do not reconstruct that state from the optional path here.
        if (
            not keys
            or already_bound
        ):
            self.invalidate()
            return False
        try:
            revision = SessionRevision.observe(current.session_file).require_available()
        except SessionRevisionUnavailable:
            self.invalidate()
            return False
        try:
            source = SelectedAdmissionSource.capture(
                current, turn, admission, keys, self.dispositions.read(), text,
                self.selected.original_source.reserved_revision,
            )
        except ValueError:
            self.invalidate()
            return False
        identity = SelectedAdmissionIdentity(source=source, session_revision=revision)
        try:
            self.dispositions.read().compaction_rows(current, keys, self.inputs)
        except RelationViolationError:
            self.invalidate()
            return False
        return self.selected.consume_bound_original(
            wire_root=self.root,
            session_file=current.session_file,
            identity=identity,
            native_id=native_id,
            sent_text=text,
            dispositions=self.dispositions,
        )
