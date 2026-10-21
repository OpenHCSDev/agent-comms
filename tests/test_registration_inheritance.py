"""Current registration declarations, incarnation and saved relationship ownership."""

from typing import Annotated
from dataclasses import dataclass, replace

from agent_comms.thread_text import SavedSessionFile
from agent_comms import field_codec
from agent_comms.comms import wire
from agent_comms.pi_vocabulary import HighThinkingLevel
from agent_comms.registration_inheritance import PredicateInheritance
from agent_comms.threads import Thread
from agent_comms.thread_text import GivenThreadText
from agent_comms.thread_model import DeclaredModel
from agent_comms.turn_lease import NoTurnLease


def test_unchanged_registration_retains_generated_creation_marker(*, thread_registration):
    incoming = thread_registration.new_thread('fresh', frozenset(), '/wt')
    assert incoming._generated_created_at
    assert incoming.for_registration("fresh", None) is incoming
    assert incoming._generated_created_at


def test_declared_new_inheritance_case_needs_no_registration_consumer_edit(thread_registration):
    class InheritUnsetCount(PredicateInheritance):
        @staticmethod
        def inherits(incoming: object) -> bool:
            return incoming == -1

    @dataclass(frozen=True, slots=True)
    class CountedThread(Thread):
        count: Annotated[int, field_codec.RegistrationPolicy(InheritUnsetCount)] = -1

    previous = thread_registration.new_thread('owner', frozenset(), '/wt', kind=CountedThread, created_at=42.0, count=9)
    omitted = replace(previous, count=-1)
    assert omitted.for_registration("owner", previous) == previous
    explicit = replace(previous, count=0)
    assert explicit.for_registration("owner", previous) is explicit


def test_registry_redeclaration_inherits_metadata_but_respects_explicit_values(tmp_path):
    comms = wire(tmp_path)
    initial = comms.registry.new_thread(
        "owner",
        frozenset({"team"}),
        '/old',
        session_file=SavedSessionFile(str(tmp_path / "session.jsonl")),
        model=DeclaredModel('provider/model'),
        thinking_level=HighThinkingLevel,
        auto_title_pending=True,
        title=GivenThreadText("retained"),
    )
    initial = replace(initial, worktree=str(tmp_path))
    comms.registry.declare(initial)
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(tmp_path)))
    restored = comms.registry.require("owner")
    assert restored == initial
    assert restored.incarnation == initial.incarnation
    updated = replace(restored, tags=frozenset({"new"}), title=GivenThreadText(""), auto_title_pending=False)
    comms.registry.declare(updated)
    current = comms.registry.require("owner")
    assert current.tags == frozenset({"new"}) and current.title == GivenThreadText("")
    assert current.auto_title_pending  # Existing pending ownership is retained, not reset.
    assert current.incarnation == initial.incarnation
    assert current.channel_scope_generation == restored.channel_scope_generation + 1


def test_operational_alias_redeclaration_returns_preserved_current_owner(tmp_path):
    comms = wire(tmp_path)
    first = comms.registry.declare(comms.registry.new_thread('before', frozenset({'team'}), str(tmp_path)))
    comms.registry.rename("before", "after")
    returned = comms.registry.declare(comms.registry.new_thread('before', frozenset(), str(tmp_path), title=GivenThreadText('new')))
    assert returned == comms.registry.require("after")
    assert returned.incarnation.created_at == first.created_at
    assert returned.name == "after" and returned.tags == first.tags
    assert returned.title == GivenThreadText("new")


def test_project_round_trip_excludes_current_project_from_history(tmp_path):
    first, second = tmp_path / 'a', tmp_path / 'b'
    first.mkdir()
    second.mkdir()
    comms = wire(tmp_path / 'wire')
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(first)))
    comms.threads.set_project('owner', str(second))
    comms.threads.set_project('owner', str(first))
    current = comms.registry.require('owner')
    assert current.worktree == str(first)
    assert current.earlier_projects == (second,)


def test_project_change_retains_executing_source_but_refuses_another_input(tmp_path):
    import os

    import pytest

    from agent_comms.child_process import ProcessIdentity
    from agent_comms.coordination_errors import StaleFence
    from agent_comms.coordination_response import LiveResponseOwner

    old, new = tmp_path / 'old', tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / 'wire')
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(old), process_identity=ProcessIdentity.capture(os.getpid())))
    original = comms.agents.begin_turn('owner', 'original-turn')
    try:
        comms.threads.set_project('owner', str(new))
        with pytest.raises(StaleFence):
            original.require_snapshot(comms.registry.snapshot(), 'New input refused')
        saved = str(old / 'native.jsonl')
        observed = comms.registry.attach_native_session(original, saved)
        current = comms.registry.require('owner')
        assert current.worktree == str(new) and current.earlier_projects == (old,)
        assert current.session_file == observed.thread.session_file == SavedSessionFile(saved)
        assert observed.thread.worktree == str(old)
        assert observed.turn_lease == original.turn_lease == current.turn_lease
        # Later native observations use the same source and preserve old -> new.
        observed = comms.registry.attach_native_session(observed, saved)
        assert observed.thread.worktree == str(old)
        with pytest.raises(StaleFence):
            observed.require_snapshot(comms.registry.snapshot(), 'New input refused')
        LiveResponseOwner(thread=observed.thread,
            claimed_generation=original.claimed_generation).require_snapshot(
                comms.registry.snapshot(), 'Original reply publication')
    finally:
        assert comms.agents.finish_turn(original.turn_lease)
    assert comms.registry.require('owner').turn_lease == NoTurnLease()


def test_source_publication_refuses_unrecorded_project_replacement(tmp_path):
    import os

    import pytest

    from agent_comms.child_process import ProcessIdentity
    from agent_comms.coordination_errors import StaleFence

    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(tmp_path / 'old'), process_identity=ProcessIdentity.capture(os.getpid())))
    original = comms.agents.begin_turn('owner', 'original-turn')
    current = comms.registry.require('owner')
    comms.registry.register(replace(current, worktree=str(tmp_path / 'other')))
    before = comms.registry.store.path.read_bytes()
    with pytest.raises(StaleFence):
        comms.registry.attach_native_session(original, str(tmp_path / 'native.jsonl'))
    assert comms.registry.store.path.read_bytes() == before
    assert comms.agents.finish_turn(original.turn_lease)
