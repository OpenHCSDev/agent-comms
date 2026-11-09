"""Selected original inputs captured by the qualified install worker."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.context_store import ContextManifestReference
from agent_comms.execution.native_session_reopen import NativeSessionIdentity
from agent_comms.installer.interpreter_operation import RuntimeProbe, TargetProbe
from agent_comms.installer.practice import PracticeCopy, PracticeMeasurement
from agent_comms.nominal.field_codec import PathText, ThreadLookup


@dataclass(frozen=True)
class SelectedInputsProbe(TargetProbe, RuntimeProbe[PracticeMeasurement]):
    """Explicit selected capture in the target worker's existing retargeted hold.

    Not the ordinary complete rehearsal, a new hold/stop operation, or native
    launch authority. Original coordinates and the allowance belong to this
    request; no supplied callback runs outside the captured source lifetime.
    """

    root: Annotated[Path, PathText]
    destination: Annotated[Path, PathText]
    maximum_bytes: int
    owners: tuple[ThreadLookup, ...] = ()
    identities: tuple[NativeSessionIdentity, ...] = ()
    requests: tuple[ContextManifestReference, ...] = ()

    def run(self) -> PracticeMeasurement:
        return PracticeCopy(self.root, self.destination).capture_inputs(
            self.owners, maximum_bytes=self.maximum_bytes,
            identities=self.identities, requests=self.requests,
        )
