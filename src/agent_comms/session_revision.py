"""One observed revision of a saved session file and its input-proof sidecar."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .declared_family import DeclaredFamily
from .native_storage import NativeSessionFiles
from .private_path import FileRevision


class SessionRevisionUnavailable(ValueError):
    """The selected filesystem observation cannot supply a revision."""


class InputProofRevision(DeclaredFamily, affix="ProofRevision"):
    """Observation of the original proof journal, never proof of an input."""


@dataclass(frozen=True, slots=True)
class MissingInputProofRevision(InputProofRevision):
    """The sidecar path is absent; no input-history or replay fact is inferred."""


@dataclass(frozen=True, slots=True)
class PresentInputProofRevision(InputProofRevision):
    revision: FileRevision


class SessionObservation(DeclaredFamily, affix="Observation"):
    def matches(self, reserved: SessionRevision) -> bool:
        return False

    @abstractmethod
    def require_available(self) -> SessionRevision: ...


@dataclass(frozen=True)
class UnavailableSessionObservation(SessionObservation):
    error: Exception

    def require_available(self) -> SessionRevision:
        raise SessionRevisionUnavailable(str(self.error)) from self.error


@dataclass(frozen=True, slots=True)
class SessionRevision(SessionObservation):
    """One native-file revision and its separately observed input-proof resource.

    A retained child is current while both are unchanged. None of these
    observations acquires native custody.
    """

    native: FileRevision
    input_proof: InputProofRevision

    @classmethod
    def observe(cls, source_path: str | None) -> SessionObservation:
        if not source_path:
            return UnavailableSessionObservation(ValueError("No selected native session"))
        try:
            native = FileRevision.from_stat(Path(source_path).stat())
            try:
                proof = PresentInputProofRevision(
                    FileRevision.from_stat(NativeSessionFiles(Path(source_path)).input_proof.stat())
                )
            except FileNotFoundError:
                proof = MissingInputProofRevision()
            return cls(native, proof)
        except OSError as error:
            # Unreadable proof is not a missing proof. Deny source acquisition.
            return UnavailableSessionObservation(error)

    def require_available(self) -> SessionRevision:
        return self

    def matches(self, reserved: SessionRevision) -> bool:
        return self == reserved

    def current(self, source_path: str | None) -> bool:
        return self.observe(source_path).matches(self)
