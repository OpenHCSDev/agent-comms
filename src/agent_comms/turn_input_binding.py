"""Durable original binding and its selected-summary one-shot authority."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .backend import _session_revision
from .compaction_send_admission import native_input_admitted
from .errors import RelationViolationError
from .selected_source import SelectedAdmissionSource
from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission
from .text_digest import TextDigest
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
        for key in keys:
            row = self.dispositions.read().lookup(key)
            if not row.unresolved or not row.matches_admission(admission):
                return False
            if already_bound:
                if not row.matches_native(native_id=native_id, turn_id=turn.value, text=text):
                    return False
            elif not self.dispositions.bind(
                key, admission=admission, turn_id=turn.value, native_id=native_id, text=text
            ):
                return False
        return True


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
            len(keys) != 1
            or already_bound
            or (revision := _session_revision(current.session_file)) is None
        ):
            self.invalidate()
            return False
        original = self.dispositions.read().lookup(keys[0])
        if not original.exists:
            self.invalidate()
            return False
        digest = TextDigest.of(text)
        identity = SelectedAdmissionIdentity(
            source=SelectedAdmissionSource(
                incarnation=current.incarnation,
                owner=current.process_identity,
                turn=turn,
                ingress_key=keys[0],
                admission_generation=admission,
                correction_witness=f"{admission}:{digest.value}",
                input_digest=digest,
                original_digest=original.digest,
                reserved_revision=self.selected._identity.source.reserved_revision,
            ),
            session_revision=revision,
        )
        try:
            self.dispositions.read().compaction_rows(current, keys[0], self.inputs)
        except RelationViolationError:
            self.invalidate()
        return self.selected.consume_bound_original(
            wire_root=self.root,
            session_file=current.session_file,
            identity=identity,
            native_id=native_id,
            sent_text=text,
            dispositions=self.dispositions,
        )
