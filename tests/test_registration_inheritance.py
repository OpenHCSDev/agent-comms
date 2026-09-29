"""Current registration declarations, incarnation and saved relationship custody."""

from dataclasses import dataclass, field, replace

from agent_comms.comms import wire
from agent_comms.registration_inheritance import RegistrationInheritance
from agent_comms.threads import Thread


def test_unchanged_registration_retains_generated_creation_marker():
    incoming = Thread("fresh", frozenset(), "/wt")
    assert incoming._generated_created_at
    assert incoming.for_registration("fresh", None) is incoming
    assert incoming._generated_created_at


def test_declared_new_inheritance_case_needs_no_registration_consumer_edit():
    class InheritUnsetCount(RegistrationInheritance):
        @staticmethod
        def inherits(incoming: object) -> bool:
            return incoming == -1

    @dataclass(frozen=True, slots=True)
    class CountedThread(Thread):
        count: int = field(default=-1, metadata={"registration_inheritance": InheritUnsetCount})

    previous = CountedThread("owner", frozenset(), "/wt", created_at=42.0, count=9)
    omitted = replace(previous, count=-1)
    assert omitted.for_registration("owner", previous) == previous
    explicit = replace(previous, count=0)
    assert explicit.for_registration("owner", previous) is explicit


def test_registry_redeclaration_inherits_metadata_but_respects_explicit_values(tmp_path):
    comms = wire(tmp_path)
    initial = Thread(
        "owner",
        frozenset({"team"}),
        str(tmp_path),
        session_file=str(tmp_path / "session.jsonl"),
        model="provider/model",
        thinking_level="high",
        previous_worktrees=("/old",),
        auto_title_pending=True,
        title="retained",
    )
    comms.registry.declare(initial)
    comms.registry.declare(Thread("owner", frozenset(), str(tmp_path)))
    restored = comms.registry.require("owner")
    assert restored == initial
    assert restored.incarnation == initial.incarnation
    updated = replace(restored, tags=frozenset({"new"}), title="", auto_title_pending=False)
    comms.registry.declare(updated)
    current = comms.registry.require("owner")
    assert current.tags == frozenset({"new"}) and current.title == ""
    assert current.auto_title_pending  # Existing pending ownership is retained, not reset.
    assert current.incarnation == initial.incarnation
    assert current.channel_scope_generation == restored.channel_scope_generation + 1


def test_generated_clock_collision_is_still_allocated_by_locked_registry(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_comms.threads.time.time", lambda: 42.0)
    comms = wire(tmp_path)
    for name in ("first", "second"):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    first, second = comms.registry.require("first"), comms.registry.require("second")
    assert first.created_at == 42.0
    assert second.created_at > first.created_at
    assert first.incarnation != second.incarnation


def test_operational_alias_redeclaration_returns_preserved_current_owner(tmp_path):
    comms = wire(tmp_path)
    first = comms.registry.declare(Thread("before", frozenset({"team"}), str(tmp_path)))
    comms.registry.rename("before", "after")
    returned = comms.registry.declare(Thread("before", frozenset(), str(tmp_path), title="new"))
    assert returned == comms.registry.require("after")
    assert returned.incarnation.created_at == first.created_at
    assert returned.name == "after" and returned.tags == first.tags
    assert returned.title == "new"


def test_generated_claim_clock_collision_preserves_allocated_names_and_identities(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agent_comms.threads.time.time", lambda: 42.0)
    comms = wire(tmp_path)
    first = comms.threads.claim_thread("owner", tags=frozenset(), worktree=str(tmp_path))
    second = comms.threads.claim_thread("owner", tags=frozenset(), worktree=str(tmp_path))
    assert first.name == "owner" and second.name == "owner-2"
    assert first.created_at == 42.0 and second.created_at > first.created_at
    assert comms.registry.require(second.name) == second
    assert first.incarnation != second.incarnation
