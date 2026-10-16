"""Registry snapshots own recipient lookup; activity reads own their requested set."""

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_comms.activity import Activity, ActivityLog, ActivityState
from agent_comms.agent_activity import UnavailableRecipientActivity
from agent_comms.audience_manifest import FrozenRecipient
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.read_ledger import ReadLedger
from agent_comms.thread_identity import (
    DeletedThreadIncarnation,
    RecordedThreadIncarnation,
    ThreadIncarnation,
    UnrecordedThreadIncarnation,
)
from agent_comms.threads import Thread


def test_recipient_index_is_built_once_and_reused_for_small_window(tmp_path):
    comms = Comms(tmp_path / "bus")
    for index in range(12):
        comms.registry.declare(Thread(f"peer-{index}", frozenset(), str(tmp_path), created_at=1_700_000_000.0 + index))
    snapshot = comms.registry.snapshot()
    assert FieldCodec.encode(snapshot)["threads"]
    wanted = snapshot.threads["peer-7"]
    lookup = wanted.incarnation.lookup
    assert snapshot.by_lookup[lookup] is wanted
    assert snapshot.thread_for_recipient(lookup, wanted.name) is wanted
    with pytest.raises(KeyError):
        snapshot.thread_for_recipient("not-a-recipient", wanted.name)
    with pytest.raises(RelationViolationError, match="recipient name"):
        snapshot.thread_for_recipient(lookup, "peer-8")
    assert snapshot.by_lookup.get("not-a-recipient") is None
    with patch.object(RecordedThreadIncarnation, "lookup", property(
        lambda self: (_ for _ in ()).throw(AssertionError("rehashing unrelated registry"))
    )):
        observations = comms.agents.observe_recipients(
            (FrozenRecipient("not-a-recipient", "gone"),
             FrozenRecipient(lookup, wanted.name), FrozenRecipient(lookup, wanted.name)),
            snapshot=snapshot,
        )
    assert set(observations) == {"not-a-recipient", lookup}
    assert isinstance(observations["not-a-recipient"], UnavailableRecipientActivity)


def test_creation_lookup_is_cached_on_the_renamable_incarnation(tmp_path):
    thread = Thread("worker", frozenset(), str(tmp_path), created_at=1_700_000_000.0)
    incarnation = thread.incarnation
    assert thread.incarnation is incarnation
    original_sha = hashlib.sha256
    with patch("agent_comms.thread_identity.hashlib.sha256", wraps=original_sha) as hashed:
        first = incarnation.lookup
        assert incarnation.lookup == first
        assert hashed.call_count == 1
    renamed = replace(thread, name="renamed")
    assert renamed.incarnation.lookup == first
    assert FieldCodec.encode(incarnation) == {"name": "worker", "created_at": 1_700_000_000.0}
    deleted = ThreadIncarnation.deleted("worker")
    historical = ThreadIncarnation("historical", 0.0)
    assert isinstance(incarnation, RecordedThreadIncarnation)
    assert isinstance(deleted, DeletedThreadIncarnation)
    assert isinstance(historical, UnrecordedThreadIncarnation)
    assert not hasattr(deleted, "lookup") and not hasattr(historical, "lookup")
    with pytest.raises(ValueError, match="positive recorded birth"):
        deleted.require_recorded()
    with pytest.raises(ValueError, match="positive recorded birth"):
        GoalWaitTarget("historical", 0.0)


def test_archived_duplicate_birth_keeps_names_without_selecting_an_arbitrary_owner(tmp_path):
    comms = Comms(tmp_path / "bus")
    for name, birth in (("old", 1700.0), ("second", 1701.0)):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path), created_at=birth))
    source = comms.registry.snapshot()
    archived = replace(source, threads={
        **source.threads,
        "second": replace(source.threads["second"], created_at=1700.0),
    })
    lookup = source.threads["old"].incarnation.lookup
    assert archived.by_lookup.get(lookup) is None
    with pytest.raises(KeyError):
        archived.thread_for_recipient(lookup, "old")
    ledger = ReadLedger(tmp_path / "archive-reads.json")
    with patch.object(ReadLedger, "seen_sequences", return_value=frozenset({7})):
        for name in ("old", "second"):
            assert ledger.historical_displayed_recipient(
                SimpleNamespace(seq=7), FrozenRecipient(lookup, name), archived
            ) == (archived.threads[name].incarnation,)
        assert ledger.historical_displayed_recipient(
            SimpleNamespace(seq=7), FrozenRecipient("wrong-lookup", "old"), archived
        ) == ()
    with pytest.raises(RelationViolationError, match="creation identities are ambiguous"):
        archived.require_unambiguous_ownership()
    unrecorded = replace(source, threads={
        **source.threads,
        "second": replace(source.threads["second"], created_at=0.0),
    })
    assert isinstance(unrecorded.threads["second"].incarnation, UnrecordedThreadIncarnation)
    assert not hasattr(unrecorded.threads["second"].incarnation, "lookup")
    assert lookup in unrecorded.by_lookup
    with pytest.raises(RelationViolationError, match="creation identities are ambiguous or unrecorded"):
        unrecorded.require_unambiguous_ownership()


def test_activity_log_projects_only_requested_threads(tmp_path):
    log = ActivityLog(tmp_path / "activity.jsonl")
    for index in range(10):
        log.emit(Activity(f"peer-{index}", ActivityState.THINKING, "working"))
    assert set(log.current_for(("peer-7",))) == {"peer-7"}
    assert log.current_for(()) == {}
    assert len(log.all_current()) == 10
