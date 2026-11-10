"""Declared names link where they stand alone, longest first, with an optional @."""

import pickle

from agent_comms.comms import Comms
from agent_comms.messaging.display_references import (
    NO_HISTORY_SOURCE,
    CurrentDisplayReferences,
    DisplayReferences,
)
from agent_comms.messaging.historical_views import HistorySource
from agent_comms.messaging.messages import InfoKind, Message, PublicRecord
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.threads import NativeThread


def spans(text: str) -> list[str]:
    references = CurrentDisplayReferences("/root", {
        name: RecordedThreadIncarnation(name, float(index))
        for index, name in enumerate(("nra", "nra-fork", "code"), start=1)
    })
    return [text[match.start:match.end] for match in references.find(text)]


def test_a_longer_name_wins_and_at_is_part_of_the_link():
    assert spans("ask @nra-fork then nra.") == ["@nra-fork", "nra"]


def test_names_inside_other_words_paths_and_dotted_names_do_not_link():
    assert spans("xnra nra-other src/nra nra/x nra.py nra:1 #code") == []


def test_no_targets_finds_nothing():
    assert DisplayReferences("/root", {}).find("nra") == ()


def test_source_projection_reuses_real_captures_without_merging_revisions(comms: Comms) -> None:
    comms.channels.create_tag("team")
    original = comms.registry.declare(NativeThread(
        name="reference", tags=frozenset(), worktree=Checkout(comms.root.parent)))
    references = CurrentDisplayReferences.from_registry(
        str(comms.root), comms.registry.snapshot(), ("#team",))
    earlier = HistorySource.capture_original(comms.root)
    recorded = references.for_source(earlier)
    assert references.for_source(NO_HISTORY_SOURCE) is references
    assert references.for_source(HistorySource.capture_original(comms.root)) is recorded
    assert recorded.targets["#team"] == references.targets["#team"]
    assert recorded.find(f"@{original.name}")[0].reference.incarnation == original.incarnation

    comms.registry.rename(original.name, "renamed-reference")
    later = HistorySource.capture_original(comms.root)
    assert later.key == earlier.key
    assert later.snapshot_bus_revision == earlier.snapshot_bus_revision
    assert later.snapshot_registry_revision != earlier.snapshot_registry_revision
    renamed = references.for_source(later)
    assert renamed is not recorded
    assert references.for_source(later) is renamed
    assert recorded.find("@renamed-reference") == ()
    assert renamed.find("@renamed-reference")[0].reference.name == "renamed-reference"
    assert renamed.targets["#team"] == recorded.targets["#team"]
    assert references.for_source(earlier) is recorded

    comms.bus.bus.append(lambda seq, db: PublicRecord.from_message(
        Message("renamed-reference", "#team", "another bus revision", InfoKind, seq=seq)))
    appended = HistorySource.capture_original(comms.root)
    assert appended.key == later.key
    assert appended.snapshot_registry_revision == later.snapshot_registry_revision
    assert appended.snapshot_bus_revision != later.snapshot_bus_revision
    after_append = references.for_source(appended)
    assert after_append is not renamed
    assert after_append == renamed
    assert references.for_source(appended) is after_append

    restored = pickle.loads(pickle.dumps(references, protocol=5))
    assert restored == references
    restored_recorded = restored.for_source(earlier)
    assert restored_recorded == recorded
    assert restored_recorded is not recorded
    assert restored.for_source(earlier) is restored_recorded
