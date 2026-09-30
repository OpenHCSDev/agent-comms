"""Acquired original-format observation for an owned target-format fixture.

The one-shot outside-src C3 operation is shared with the stopped batch. Its
original installed process owns original decoding, and current FieldCodec owns
strict target decoding. Launch credentials are captured by RetainedOwnerLaunch
from the exact process, remain in RAM, and never cross the JSON pipe.
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
from typing import ClassVar

from agent_comms.field_codec import FieldCodec
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.registry_document import RegistryDocument
from agent_comms.threads import Thread
from owner_read_projection import (
    CurrentThreadProjection, OwnerReadProjection, RetiredGoalReportProjection,
)


@dataclass(frozen=True)
class OriginalOwnerRead:
    document: RegistryDocument
    selection: OwnerRestartSelection


@dataclass(frozen=True)
class OriginalTypedCapture:
    root: Path
    original_python: Path
    projection: ClassVar[type[OwnerReadProjection]] = RetiredGoalReportProjection

    def observe(self, name: str, expected: OwnerRestartSelection | None = None) -> OriginalOwnerRead:
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        packet = subprocess.run([
            str(self.original_python), str(Path(__file__).with_name('read_original_owner.py')),
            str(self.root), name, self.projection.declared_name,
        ], input=json.dumps(FieldCodec.encode(expected)) if expected is not None else '',
            env=environment, capture_output=True, text=True, check=True)
        return FieldCodec.decode(OriginalOwnerRead, json.loads(packet.stdout))

    def read(self, name: str) -> CapturedOriginalOwner:
        observed = self.observe(name)
        snapshot = observed.document.snapshot()
        source = observed.selection.require_current(snapshot)
        retained = RetainedOwnerLaunch.capture(source, snapshot,
            interpreter=str(self.original_python))
        captured = CapturedOriginalOwner(self, observed.selection, source, retained)
        captured.require_current()
        return captured


class CurrentTypedCapture(OriginalTypedCapture):
    """Same proof and launch custody for an authenticated current producer."""

    projection = CurrentThreadProjection


@dataclass(frozen=True)
class CapturedOriginalOwner:
    reader: OriginalTypedCapture = field(repr=False)
    selection: OwnerRestartSelection
    source: Thread
    retained: RetainedOwnerLaunch = field(repr=False)

    def require_current(self) -> Thread:
        """Fresh original shared read; owner AND admission AND process proof."""
        observed = self.reader.observe(self.source.name, self.selection)
        return self.selection.require_current(observed.document.snapshot())
