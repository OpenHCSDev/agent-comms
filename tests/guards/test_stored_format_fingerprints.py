"""Stored declarations cannot change without their owner's registered conversion."""
from __future__ import annotations

import importlib
from inspect import isabstract
import json
from pathlib import Path
import pkgutil
import subprocess

from dataclasses import dataclass

import pytest
import agent_comms
from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import DocumentConversion, LockedStore
from agent_comms.store_files import StoreOwner

MANIFEST = Path(__file__).with_name("stored_format_fingerprints.json")
ROOT = Path(__file__).resolve().parents[2]


def stored_documents() -> dict[str, type[LockedStore]]:
    # Load the production declarations, not a hand-maintained list of stores.
    for module in pkgutil.walk_packages(agent_comms.__path__, agent_comms.__name__ + "."):
        importlib.import_module(module.name)
    return {
        f"{owner.__module__}.{owner.__qualname__}": owner
        for owner in StoreOwner.members_with(LockedStore)
        if owner.__module__.split(".")[0] == agent_comms.__name__
    }


def previous_manifest() -> dict:
    base = subprocess.check_output(
        ["git", "merge-base", "HEAD", "origin/main"], cwd=ROOT, text=True,
    ).strip()
    relative = MANIFEST.relative_to(ROOT).as_posix()
    files = subprocess.check_output(
        ["git", "ls-tree", "--name-only", base, "--", relative], cwd=ROOT, text=True,
    ).splitlines()
    if not files:
        return {"stores": {}, "conversions": []}  # Initial recorded baseline only.
    return json.loads(subprocess.check_output(
        ["git", "show", f"{base}:{relative}"], cwd=ROOT, text=True,
    ))


def require_formats(documents: dict[str, type[LockedStore]], manifest: dict, previous: dict) -> None:
    recorded = manifest["stores"]
    assert documents.keys() == recorded.keys(), (
        f"Stored document membership differs: unrecorded={documents.keys() - recorded.keys()}, "
        f"removed={recorded.keys() - documents.keys()}"
    )
    for name, store in documents.items():
        current = store.format_fingerprint()
        old = recorded[name]
        assert store.format_fingerprint.__func__ is LockedStore.format_fingerprint.__func__, name
        assert old == current, f"{name}: stored format changed {old} -> {current}; register a conversion"
        conversion = store.conversion
        implementation = f"{conversion.__module__}.{conversion.__qualname__}"
        for source in conversion.source_fingerprints:
            change = {"store": name, "source": source, "target": current, "conversion": implementation}
            assert (issubclass(conversion, DocumentConversion) and not isabstract(conversion)
                    and change in manifest["conversions"]), (
                f"{name}: no registered conversion {source} -> {current} on {implementation}"
            )
    assert previous["stores"].keys() <= recorded.keys(), "A recorded store was removed without a conversion"
    # Existing history is immutable; changing the manifest cannot erase a missing conversion.
    assert all(change in manifest["conversions"] for change in previous["conversions"]), (
        "Registered format conversion history was removed or rewritten"
    )
    for name, old in previous["stores"].items():
        current = recorded[name]
        if current == old:
            continue
        conversion = documents[name].conversion
        implementation = f"{conversion.__module__}.{conversion.__qualname__}"
        change = {"store": name, "source": old, "target": current, "conversion": implementation}
        assert (old in conversion.source_fingerprints
                and issubclass(conversion, DocumentConversion) and not isabstract(conversion)
                and change in manifest["conversions"]), (
            f"{name}: no registered conversion {old} -> {current} on {implementation}"
        )


def test_stored_format_fingerprints(commander_script):
    require_formats(stored_documents(), json.loads(MANIFEST.read_text()), previous_manifest())


def test_recorded_change_requires_a_real_declared_conversion(tmp_path, monkeypatch):
    @dataclass
    class Before:
        count: int

    @dataclass
    class After:
        count: int
        extra: int = 0

    class ExampleStore(LockedStore[Before], declared_name=None):
        record_type = Before

        def empty(self):
            return Before(0)

    store = ExampleStore
    old = store.format_fingerprint()
    monkeypatch.setattr(ExampleStore, "record_type", After)
    current = store.format_fingerprint()
    documents = {"example.Store": store}
    previous = {"stores": {"example.Store": old}, "conversions": []}
    manifest = {"stores": {"example.Store": current}, "conversions": []}
    with pytest.raises(AssertionError, match=f"{old} -> {current}"):
        require_formats(documents, manifest, previous)

    class AddField(DocumentConversion):
        source_fingerprints = frozenset({old})

        @classmethod
        def converted(cls, raw):
            return {**raw, "extra": 0}

    monkeypatch.setattr(ExampleStore, "conversion", AddField)
    # A declaration alone cannot silently update the committed record.
    with pytest.raises(AssertionError, match=f"{old} -> {current}"):
        require_formats(documents, manifest, previous)
    manifest["conversions"].append({
        "store": "example.Store", "source": old, "target": current,
        "conversion": f"{AddField.__module__}.{AddField.__qualname__}",
    })
    require_formats(documents, manifest, previous)
    assert FieldCodec.decode_document(After, AddField.converted({"count": 2})) == After(2, 0)
    assert not list(tmp_path.iterdir())


def test_schema_sets_are_unordered_but_default_data_and_tuple_positions_are_not():
    normalize = LockedStore._canonical_schema
    assert normalize({"required": ["b", "a"]}) == normalize({"required": ["a", "b"]})
    assert normalize({"default": {"enum": [2, 1]}}) != normalize({"default": {"enum": [1, 2]}})
    assert normalize({"prefixItems": [{"type": "string"}, {"type": "number"}]}) != normalize(
        {"prefixItems": [{"type": "number"}, {"type": "string"}]})
