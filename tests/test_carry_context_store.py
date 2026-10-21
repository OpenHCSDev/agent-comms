"""The retired context format is consumed only by the installer's one-shot step."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from cutover.carry_context_store import (
    ContextCheckpoint,
    ContextEntryManifestObservation,
    ContextEnvelope,
    ContextManifests,
    ContextMarker,
    ContextPrefix,
    ContextRootId,
    RecordedContextManifestObservation,
    RecordedContextManifestSources,
)

from agent_comms.context_store import (
    AbsentContext,
    CapturedContext,
    ContextEntry,
    ContextStore,
    ContextStoreIdentity,
)
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.carry import Carried
from agent_comms.messages import InfoKind, Message
from agent_comms.turn_context import ContextManifest
from bus_record_fixture import context_comms
from test_context_manifest_deltas import context


@dataclass(frozen=True)
class ContextHistory:
    step: ContextManifests
    retained: bytes
    manifests: tuple[ContextManifest, ...]


@pytest.fixture
def context_history(tmp_path):
    owner = context_comms(tmp_path / "current").registry.require("Alice")
    manifests = (context(owner, tmp_path, 2, 1), context(owner, tmp_path, 3, 2))
    root = tmp_path / "legacy"
    root.mkdir()
    retained = (
        b"  "
        + json.dumps(
            Message("sender", owner.name, "Retained bytes. " * 400, InfoKind, seq=1).to_wire()
        ).encode()
        + b"\r\n"
    )
    raw = bytearray(retained)
    sources = []
    prior = AbsentContext()
    for manifest in manifests:
        record, entries = prior.capture(manifest)
        raw.extend(
            json.dumps(
                FieldCodec.encode(ContextEnvelope(ContextEntryManifestObservation(entries)))
            ).encode()
            + b"\n"
        )
        offset = len(raw)
        line = (
            json.dumps(
                FieldCodec.encode(ContextEnvelope(RecordedContextManifestObservation(record)))
            ).encode()
            + b"\n"
        )
        raw.extend(line)
        sources.append(RecordedContextManifestSources(offset, len(line), manifest.thread))
        prior = CapturedContext(ContextStoreIdentity("a" * 32), offset, record, manifest)
    assert record.segments[2].inherited_entries == 2
    (root / "bus.jsonl").write_bytes(raw)
    marker = ContextMarker(1, 0, ContextRootId("a" * 32))
    (root / marker.filename).write_text(json.dumps(FieldCodec.encode(marker)))
    digest = ContextPrefix.seed
    for line in raw.splitlines(keepends=True):
        digest = hashlib.sha256(digest + len(line).to_bytes(8, "big") + line).digest()
    path = ContextCheckpoint.path_in(root)
    with sqlite3.connect(path) as db:
        ContextPrefix.create(db)
        RecordedContextManifestSources.create(db)
        ContextPrefix(
            1,
            2,
            marker.root_id,
            len(raw),
            1,
            digest.hex(),
            hashlib.sha256(raw[-ContextPrefix.tail_bytes :]).hexdigest(),
        ).insert(db)
        # The predecessor stored a plain SQLite text cell, not JSON text.
        # Keep this input independent of the converter's chosen storage codec.
        db.execute("UPDATE prefix_certificate SET root_id=?", (marker.root_id.value,))
        for source in sources:
            source.insert(db)
    path.chmod(0o600)
    return ContextHistory(
        ContextManifests(root, root, tmp_path / "spill", True), retained, manifests
    )


def test_worker_restores_context_predecessors_and_retains_message_bytes(context_history):
    history = context_history
    step = history.step
    original = (step.root / "bus.jsonl").read_bytes()
    assert replace(step, apply=False).carry().changed == 3
    assert (step.root / "bus.jsonl").read_bytes() == original
    assert not ContextStore.path_in(step.root).exists()
    checkout = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_comms.installer.step_worker",
            step.name(),
            "carry",
            json.dumps(FieldCodec.encode(step)),
        ],
        cwd=checkout,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(checkout / "src"), str(checkout / "tools"))),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    carried = FieldCodec.decode(Carried, FieldCodec.parse(result.stdout))
    assert carried.changed == 3
    assert carried.detail["requests"] == 2
    assert (step.root / "bus.jsonl").read_bytes() == history.retained
    assert not ContextCheckpoint.path_in(step.root).exists()
    assert step.carry() == Carried(0)
    store = ContextStore(step.root)
    with store.snapshot() as db:
        assert ContextEntry.count(db) == 3
    latest = store.prior(history.manifests[-1].thread)
    assert isinstance(latest, CapturedContext)
    assert latest.value.segments[:2] == history.manifests[-1].segments[:2]
    assert latest.value.segments[2].contributors == history.manifests[-1].segments[2].contributors


def test_changed_prefix_never_publishes_partial_context_or_replaces_input(context_history):
    step = context_history.step
    path = step.root / "bus.jsonl"
    damaged = b"\t" + path.read_bytes()[1:]
    path.write_bytes(damaged)
    checkpoint = ContextCheckpoint.path_in(step.root).read_bytes()
    with pytest.raises(RelationViolationError):
        step.carry()
    assert path.read_bytes() == damaged
    assert ContextCheckpoint.path_in(step.root).read_bytes() == checkpoint
    assert not ContextStore.path_in(step.root).exists()
    assert not tuple(step.root.glob(".move-context-*"))
