"""Registry snapshots own recipient lookup; activity reads own their requested set."""

import hashlib
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_comms.activity import Activity, ActivityLog, ActivityState
from agent_comms.agent_activity import UnavailableRecipientActivity
from agent_comms.audience_manifest import FrozenRecipient
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.thread_identity import ThreadIncarnation
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
    assert snapshot.by_lookup.get("not-a-recipient") is None
    with patch.object(ThreadIncarnation, "lookup", property(
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
    with pytest.raises(ValueError, match="positive recorded birth"):
        assert ThreadIncarnation.deleted("worker").lookup
    with pytest.raises(ValueError, match="positive recorded birth"):
        assert ThreadIncarnation("historical", 0.0).lookup


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
    assert archived.thread_for_recipient(lookup, "old") is archived.threads["old"]
    assert archived.thread_for_recipient(lookup, "second") is archived.threads["second"]
    assert archived.thread_for_recipient(lookup, "missing") is None
    assert archived.thread_for_recipient("wrong-lookup", "old") is None
    with pytest.raises(RelationViolationError, match="creation identities collide"):
        archived.require_unambiguous_ownership()


def test_activity_log_projects_only_requested_threads(tmp_path):
    log = ActivityLog(tmp_path / "activity.jsonl")
    for index in range(10):
        log.emit(Activity(f"peer-{index}", ActivityState.THINKING, "working"))
    assert set(log.all_current(threads=("peer-7",))) == {"peer-7"}
    assert log.all_current(threads=()) == {}
    assert len(log.all_current()) == 10
