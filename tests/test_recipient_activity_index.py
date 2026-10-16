"""Registry snapshots own recipient lookup; activity reads own their requested set."""

from unittest.mock import patch

from agent_comms.activity import Activity, ActivityLog, ActivityState
from agent_comms.agent_activity import UnavailableRecipientActivity
from agent_comms.audience_manifest import FrozenRecipient
from agent_comms.comms import Comms
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
    lookup = wanted.incarnation.stable_thread_lookup
    assert snapshot.thread_for_lookup(lookup) is wanted
    assert snapshot.thread_for_lookup("not-a-recipient") is None
    with patch.object(ThreadIncarnation, "stable_thread_lookup", property(
        lambda self: (_ for _ in ()).throw(AssertionError("rehashing unrelated registry"))
    )):
        observations = comms.agents.observe_recipients(
            (FrozenRecipient("not-a-recipient", "gone"),
             FrozenRecipient(lookup, wanted.name), FrozenRecipient(lookup, wanted.name)),
            snapshot=snapshot,
        )
    assert set(observations) == {"not-a-recipient", lookup}
    assert isinstance(observations["not-a-recipient"], UnavailableRecipientActivity)


def test_activity_log_projects_only_requested_threads(tmp_path):
    log = ActivityLog(tmp_path / "activity.jsonl")
    for index in range(10):
        log.emit(Activity(f"peer-{index}", ActivityState.THINKING, "working"))
    assert set(log.all_current(threads=("peer-7",))) == {"peer-7"}
    assert log.all_current(threads=()) == {}
    assert len(log.all_current()) == 10
