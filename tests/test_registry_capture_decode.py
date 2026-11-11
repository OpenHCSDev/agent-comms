"""Captured roots decode through their slice policies without a complete JSON export."""

import pytest

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import (
    InvalidRegistryDocument,
    RegistryDocument,
    RegistrySnapshot,
    ThreadRows,
)
from agent_comms.registry.registry_store import UncachedRegistry
from agent_comms.registry.registry_tables import RegistryCapture, RegistryNamedRow
from agent_comms.registry.thread_identity import GenerationCounter
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import RegistryFile
from agent_comms.storage.row_mapping import RowMapping


def test_captured_refresh_uses_changed_rows_and_cold_admission_uses_the_same_policy(tmp_path, monkeypatch):
    registry = Registration(RegistryFile.path_in(tmp_path))
    for index in range(24):
        registry.declare(NativeThread(f"worker-{index}", frozenset(), Checkout(tmp_path)))
    before = registry.store.read_revision()
    registry.declare(NativeThread("new-worker", frozenset(), Checkout(tmp_path)))
    following = registry.store.rows.captured(before.source)
    roots = tuple(family.mapping_in(capture.raw)
                  for capture in (before.source, following)
                  for family in RegistryNamedRow.families())
    original = RowMapping.__iter__

    def no_complete_export(capture):
        raise AssertionError("internal decoding exported complete JSON")

    def no_captured_enumeration(rows):
        if any(rows is root for root in roots):
            raise AssertionError("decoding enumerated a captured row mapping")
        return original(rows)

    monkeypatch.setattr(RegistryCapture, "wire_document", no_complete_export)
    monkeypatch.setattr(RowMapping, "__iter__", no_captured_enumeration)
    refreshed = before.decoded(registry.store, following)
    cold = UncachedRegistry().decoded(registry.store, following)
    assert set(refreshed.threads) == set(cold.threads) == {
        *(f"worker-{index}" for index in range(24)), "new-worker",
    }
    assert tuple(refreshed.threads) == tuple(cold.threads)
    assert "new-worker" not in before.document.threads
    assert refreshed.owners.generations == cold.owners.generations
    assert refreshed.claims.generations == cold.claims.generations


def test_lowered_generation_ceiling_rechecks_unchanged_generation_rows(tmp_path):
    registry = Registration(RegistryFile.path_in(tmp_path))
    registry.declare(NativeThread("worker", frozenset(), Checkout(tmp_path)))
    before = registry.store.read_revision()
    owners = FieldCodec.record_key(RegistryDocument, "owners")
    counter = FieldCodec.record_key(GenerationCounter, "counter")
    registry.store.rewrite_stored(lambda raw: {
        **raw, owners: {**raw[owners], counter: 0},
    })
    following = registry.store.rows.captured(before.source)
    with pytest.raises(InvalidRegistryDocument):
        before.decoded(registry.store, following)


def test_warm_membership_validation_does_not_enumerate_rows_or_unchanged_aliases(tmp_path, monkeypatch):
    registry = Registration(RegistryFile.path_in(tmp_path))
    for index in range(24):
        registry.declare(NativeThread(f"worker-{index}", frozenset(), Checkout(tmp_path)))
    for index in range(8):
        registry.rename(f"worker-{index}", f"renamed-{index}")
    before = registry.store.read_revision()
    registry.declare(NativeThread("new-worker", frozenset(), Checkout(tmp_path)))
    following = registry.store.rows.captured(before.source)
    original = RowMapping.__iter__

    def no_complete_mapping(rows):
        if len(rows):
            raise AssertionError("warm refresh enumerated an unchanged row collection")
        return original(rows)

    monkeypatch.setattr(RowMapping, "__iter__", no_complete_mapping)
    refreshed = before.decoded(registry.store, following)
    assert refreshed.require("new-worker").name == "new-worker"
    assert refreshed.require("worker-0").name == "renamed-0"


def test_snapshot_alias_validation_does_not_enumerate_declarations(tmp_path, monkeypatch):
    registry = Registration(RegistryFile.path_in(tmp_path))
    registry.declare(NativeThread("worker", frozenset(), Checkout(tmp_path)))
    registry.rename("worker", "renamed")
    document = registry.store.read_revision().document

    def no_declaration_walk(rows):
        raise AssertionError("snapshot enumerated declarations to check aliases")

    monkeypatch.setattr(ThreadRows, "__iter__", no_declaration_walk)
    snapshot = RegistrySnapshot.capture(document)
    assert snapshot.require("worker").name == "renamed"


@pytest.mark.parametrize("select", [
    lambda document: document.statuses,
    lambda document: document.owners.generations,
    lambda document: document.claims.generations,
])
def test_publication_rejects_missing_membership_before_committing(tmp_path, select):
    registry = Registration(RegistryFile.path_in(tmp_path))
    registry.declare(NativeThread("worker", frozenset(), Checkout(tmp_path)))
    before = registry.store.rows.mark()
    with registry.store.editing() as edit:
        del select(edit.document)["worker"]
        with pytest.raises(InvalidRegistryDocument):
            edit.commit()
    assert registry.store.rows.mark() == before
    assert registry.require("worker").name == "worker"


def test_removed_declaration_cannot_orphan_an_unchanged_alias(tmp_path):
    registry = Registration(RegistryFile.path_in(tmp_path))
    registry.declare(NativeThread("worker", frozenset(), Checkout(tmp_path)))
    registry.rename("worker", "renamed")
    before = registry.store.rows.mark()
    with registry.store.editing() as edit:
        del edit.document.threads["renamed"]
        del edit.document.statuses["renamed"]
        with pytest.raises(InvalidRegistryDocument, match="alias"):
            edit.commit()
    assert registry.store.rows.mark() == before
    assert registry.require("worker").name == "renamed"
