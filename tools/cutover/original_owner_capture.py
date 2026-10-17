"""Current-format observation of the selected source owner.

Both ends use the current declarations. Old durable records require the separate
one-shot carry first; there is no source-shape selection or compatibility reader.
Launch credentials remain in RetainedOwnerLaunch's RAM-only ownership.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess

from agent_comms.field_codec import FieldCodec
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.registry_provenance import RegistryProvenance
from agent_comms.historical_views import HistorySource
from agent_comms.threads import Thread
from owner_read_projection import (
    CurrentThreadProjection, OriginalOwnerRead,
    RecordedRegistryProjection,
)


@dataclass(frozen=True)
class CurrentTypedCapture:
    root: Path
    original_python: Path

    def _acquire(self, projection, name, request):
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        packet = subprocess.run([
            str(self.original_python), str(Path(__file__).with_name('read_original_owner.py')),
            str(self.root), name, projection.declared_name,
        ], input=json.dumps(request) if request is not None else '',
            env=environment, capture_output=True, text=True, check=True)
        return json.loads(packet.stdout)

    def observe(self, name: str,
                expected: OwnerRestartSelection | None = None) -> OriginalOwnerRead:
        return FieldCodec.decode(OriginalOwnerRead, self._acquire(
            CurrentThreadProjection, name,
            FieldCodec.encode(expected) if expected is not None else None,
        ))

    def recorded(self) -> HistorySource:
        source = FieldCodec.decode(HistorySource, self._acquire(
            RecordedRegistryProjection, '', FieldCodec.record_schema(RegistryProvenance),
        ))
        source.require_original(self.root)
        return source

    def read(self, name: str) -> CapturedOriginalOwner:
        observed = self.observe(name)
        snapshot = observed.document.snapshot()
        source = observed.selection.require_current(snapshot)
        # The root decoder and the selected running owner are independent.
        # RetainedOwnerLaunch observes and fences the owner's actual process.
        retained = RetainedOwnerLaunch.capture(source, snapshot)
        captured = CapturedOriginalOwner(self, observed.selection, source, retained)
        captured.require_current()
        return captured


@dataclass(frozen=True)
class CapturedOriginalOwner:
    reader: CurrentTypedCapture = field(repr=False)
    selection: OwnerRestartSelection
    source: Thread
    retained: RetainedOwnerLaunch = field(repr=False)

    def require_current(self) -> Thread:
        """Fresh original shared read; owner AND claim_scope AND process proof."""
        observed = self.reader.observe(self.source.name, self.selection)
        return self.selection.require_current(observed.document.snapshot())
