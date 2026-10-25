"""The AgentBackend family: registry, declaration checks, lookup and Pi conversions."""

from __future__ import annotations

from agent_comms.repo import Checkout
import json
import runpy
import sys
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

import agent_comms.agent_backend as ab
from agent_comms.field_codec import InputId
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.native_arguments import NativeArguments
from agent_comms.pi_durable_backend import PiDurableBackend
from agent_comms.pi_native_backend import PI_0_85_1_EVENT_KINDS, PiNativeBackend, PiSessionFile
from agent_comms.threads import Thread
from agent_comms.unstarted_runtime import UnstartedRuntime
from agent_comms.thread_model import DeclaredModel
from agent_comms.session_file import NoSessionFile, RegistrySessionLocation

# -- registry ---------------------------------------------------------------


def test_registry_holds_the_pi_native_backend_and_is_frozen():
    assert ab.AgentBackend.names() == ("pi_native", "pi_durable")
    assert ab.AgentBackend.member_named("pi_native") is PiNativeBackend
    assert ab.NewThreadBackend.selected() is PiNativeBackend
    assert isinstance(ab.AgentBackend.__registry__, MappingProxyType)
    assert ab.AgentBackend.member_named("pi_durable") is PiDurableBackend


def test_a_backend_declared_after_sealing_fails_loud():
    with pytest.raises(ab.BackendDeclarationError):

        class LateBackend(ab.AgentBackend):  # noqa: F841
            pass

    assert ab.AgentBackend.names() == ("pi_native", "pi_durable")


def test_pi_native_serves_threads_with_every_thread_capability():
    assert issubclass(PiNativeBackend, ab.NewThreadBackend)
    assert issubclass(PiNativeBackend, ab.ThreadServingBackend)
    assert issubclass(PiNativeBackend, ab.Interrupts)
    assert issubclass(PiNativeBackend, ab.Forks)
    assert not issubclass(PiDurableBackend, ab.ThreadServingBackend)
    assert not PiNativeBackend.__abstractmethods__
    assert PiNativeBackend.location_type is PiSessionFile


@pytest.mark.parametrize("session_file, location", [
    (RegistrySessionLocation.from_path("/home/ts/wt/test/session.jsonl", Path("/home/ts/wt")),
     PiSessionFile("/home/ts/wt/test/session.jsonl")),
    (NoSessionFile(), UnstartedRuntime(PiNativeBackend)),
])
def test_runtime_location_round_trip_uses_declared_backend(session_file, location, *, thread_registration):
    thread = thread_registration.declare(Thread('location', frozenset(), Checkout(Path('/home/ts/wt/test')), session_file=session_file))
    restored = FieldCodec.decode(Thread, FieldCodec.encode(thread))
    restored = replace(restored, session_file=restored.session_file.bound_to(Path("/home/ts/wt")))
    assert restored.runtime == location
    assert restored.runtime is restored.runtime
    assert restored.backend is PiNativeBackend




# -- declaration checks -------------------------------------------------------


@pytest.fixture
def backend_declarations():
    # Re-execute the actual declaration module in a fresh namespace. The canonical
    # package has already sealed its runtime family; none of that state is changed.
    scope = SimpleNamespace(**runpy.run_path(ab.__file__, run_name=ab.__name__))

    class Location(scope.RuntimeLocation):
        def history_source(self, root, incarnation):
            raise NotImplementedError("Declaration checks do not open history")

        def storage_files(self):
            return ()

    class RuntimeMethods(scope.AgentBackend, declared_name=None):
        @classmethod
        def for_worker(cls, runner, session_id):
            return cls()

        @classmethod
        def new_location(cls, root, worktree):
            return cls.location_type()

        def run_turn(self, thread, content, delivery):
            raise NotImplementedError("Declaration checks do not run turns")

        async def abort(self, owner):
            raise NotImplementedError("Declaration checks do not own turns")

        async def close_idle(self):
            pass

        async def compact(self, thread, instructions):
            return scope.NothingToCompaction()

    class Native(RuntimeMethods, scope.NewThreadBackend):
        location_type = Location
        event_kinds = frozenset()

    scope.RuntimeMethods = RuntimeMethods
    scope.Native = Native
    return scope


def test_incomplete_thread_backend_stays_abstract_and_cannot_rebind_a_location(backend_declarations):
    scope = backend_declarations
    original = PiSessionFile("saved-session").backend

    class IncompleteNative(scope.Native):
        location_type = PiSessionFile
        compact = scope.Compacts.compact

    with pytest.raises(TypeError, match="compact"):
        IncompleteNative()
    assert IncompleteNative not in scope.AgentBackend.members_with(scope.AgentBackend)
    assert PiSessionFile("saved-session").backend is original
    scope.AgentBackend.seal()
    assert scope.Native.new_location(None, None).backend is scope.Native
    assert scope.NewThreadBackend.selected() is scope.Native
    with pytest.raises(TypeError, match="run_turn"):
        scope.ThreadServingBackend()


def test_sealing_rejects_competing_location_owners_before_attaching_any(backend_declarations):
    scope = backend_declarations

    class Rival(scope.RuntimeMethods):
        location_type = scope.Native.location_type
        event_kinds = frozenset()

    assert scope.NewThreadBackend.selected() is scope.Native
    with pytest.raises(scope.BackendDeclarationError, match="belongs to several backends"):
        scope.AgentBackend.seal()
    with pytest.raises(AttributeError):
        scope.Native.new_location(None, None).backend


def test_new_thread_selection_requires_one_concrete_nominal_member(backend_declarations):
    empty = SimpleNamespace(**runpy.run_path(ab.__file__, run_name=ab.__name__))
    with pytest.raises(empty.BackendDeclarationError, match="exactly one backend"):
        empty.NewThreadBackend.selected()
    scope = backend_declarations

    class SecondDefault(scope.Native):
        pass

    with pytest.raises(scope.BackendDeclarationError, match="exactly one backend"):
        scope.NewThreadBackend.selected()
    with pytest.raises(scope.BackendDeclarationError, match="exactly one backend"):
        scope.AgentBackend.seal()


class _ProbeEvent(DeclaredFamily):
    @classmethod
    def wire_kind(cls):
        return cls.declared_name

    @classmethod
    def known_kinds(cls):
        return (cls.wire_kind(),)


class AlphaProbe(_ProbeEvent):
    pass


class BetaProbe(_ProbeEvent):
    pass


class FallbackProbe(_ProbeEvent):
    @classmethod
    def known_kinds(cls):
        return ()


def test_converter_family_must_cover_exactly_the_runtime_event_kinds():
    class Runtime:
        __name__ = "Runtime"
        event_kinds = frozenset({"alpha_probe", "gamma"})

    problems = ab._event_family_problems(Runtime, _ProbeEvent)
    assert problems == [
        "_ProbeEvent has no converter for event kinds ['gamma']",
        "_ProbeEvent converts event kinds the runtime never emits ['beta_probe']",
    ]
    Runtime.event_kinds = frozenset({"alpha_probe", "beta_probe"})
    assert ab._event_family_problems(Runtime, _ProbeEvent) == []


def test_pi_event_family_matches_pi_0_85_1_when_imported_fresh():
    """The import-time check holds for a fresh interpreter (no test-declared kinds)."""
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", (
            "import agent_comms.pi_events as p\n"
            "from agent_comms.pi_native_backend import PiNativeBackend as B\n"
            "kinds = {k for m in p.NativePiEvent.members_with(p.NativePiEvent) for k in m.known_kinds()}\n"
            "assert kinds == B.event_kinds, kinds ^ B.event_kinds\n"
        )],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert {"turn_start", "queue_update", "extension_error"} <= PI_0_85_1_EVENT_KINDS


# -- thread declaration -------------------------------------------------------


def test_thread_declares_its_backend_once_and_keeps_it_on_reregistration(*, thread_registration):
    thread = thread_registration.declare(Thread('owner', frozenset(), Checkout(Path('/tmp/owner'))))
    assert thread.backend is PiNativeBackend
    encoded = FieldCodec.encode(thread)
    assert "backend" not in encoded
    assert "runtime" not in encoded
    assert encoded["session_file"] is None
    assert FieldCodec.decode(Thread, encoded) == thread
    assert FieldCodec.decode(Thread, {**encoded, "backend": "pi_native"}) == thread
    with pytest.raises(ValueError):
        FieldCodec.decode(Thread, {**encoded, "runtime": {"backend": "pi_durable", "location": None}})
    assert thread.for_registration("owner", thread).backend is PiNativeBackend


def test_cli_registry_reads_do_not_load_the_pi_runtime(tmp_path):
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys, agent_comms.cli\n"
            "from pathlib import Path\n"
            "from agent_comms.threads import Thread\n"
            "Thread('a', frozenset(), '/tmp').backend\n"
            "assert 'agent_comms.backend' not in sys.modules\n"
            "assert 'agent_comms.tracked_turn' not in sys.modules\n"
            "assert 'agent_comms.turn_watchdog' not in sys.modules\n"
        ), str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


# -- lookup -------------------------------------------------------------------


class _Runner:
    agent_bin = "pi"
    agent_args = NativeArguments.parse(["--provider", "p", "--model", "m"])

    def __init__(self, comms):
        self.comms = comms
        self.persistent_backends = {}
        self.published = []

    async def observe_selected_preparation(self, session_id, thread, state, info):
        self.published.append((session_id, thread.name, info))


def test_runner_obtains_one_backend_per_session_from_the_thread_declaration(comms):
    from agent_comms.errors import RelationViolationError
    from agent_comms.turn_runner import TurnRunner

    runner = _Runner(comms)
    thread = Thread('owner', frozenset(), Checkout(Path('/tmp/owner')), model=DeclaredModel('p/m'))
    backend = TurnRunner.backend_for(runner, "session", thread)
    assert type(backend) is PiNativeBackend
    assert TurnRunner.backend_for(runner, "session", thread) is backend
    assert backend.arguments(thread) == _Runner.agent_args.with_model("p/m").argv

    from agent_comms.pi_native_backend import PersistentPiSession

    runner.persistent_backends["other"] = PersistentPiSession()
    with pytest.raises(RelationViolationError):
        TurnRunner.backend_for(runner, "other", thread)


# -- Pi conversions -----------------------------------------------------------


def _session_lines(path: Path) -> None:
    rows = [
        {"type": "session", "id": "s", "version": 3, "timestamp": "2026-10-10T10:00:00.000Z",
         "cwd": "/tmp"},
        {"type": "message", "id": "u1", "parentId": None, "timestamp": "2026-10-10T10:00:01.000Z",
         "message": {"role": "user", "content": "hello there"}},
        {"type": "message", "id": "a1", "parentId": "u1", "timestamp": "2026-10-10T10:00:02.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "hi back"}],
                     "stopReason": "stop"}},
        {"type": "message", "id": "t1", "parentId": "a1", "timestamp": "not a time",
         "message": {"role": "toolResult", "toolCallId": "c", "toolName": "read",
                     "content": [{"type": "text", "text": "file body"}]}},
        {"type": "compaction", "id": "c1", "parentId": "t1", "timestamp": "2026-10-10T10:00:04.000Z",
         "summary": "earlier work", "firstKeptEntryId": "a1", "tokensBefore": 10},
        {"type": "label", "id": "l1", "parentId": "c1"},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))




# -- inbox records --------------------------------------------------------------


def test_inbox_records_say_which_inputs_they_carry():
    input_id = InputId.mint()
    request = ab.InputRequest(
        input_id=input_id, content=ab.InputContent(text="t"), when_busy=ab.SteerInputPolicy,
    )
    assert request.carried_inputs == (input_id,)
    assert ab.SendNow((input_id,)).carried_inputs == ()
