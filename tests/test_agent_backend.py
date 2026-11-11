"""The AgentBackend family: registry, declaration checks, lookup and Pi conversions."""

from __future__ import annotations

import os
import runpy
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_comms.agent_backend as ab
from agent_comms.execution.native_arguments import NativeArguments
from agent_comms.execution.pi_durable_backend import PiDurableBackend
from agent_comms.execution.session_file import NoSessionFile, RegistrySessionLocation
from agent_comms.execution.turn_inbox import (
    InputContent,
    InputRequest,
    SendNow,
    SteerInputPolicy,
)
from agent_comms.execution.unstarted_runtime import UnstartedRuntime
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import AcpSessionId, FieldCodec, InputId
from agent_comms.pi_native_backend import PI_0_85_1_EVENT_KINDS, PiNativeBackend, PiSessionFile
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.threads import NativeThread, Thread

# -- registry ---------------------------------------------------------------


def test_registry_holds_the_pi_backends_and_selects_native_for_new_threads():
    assert ab.AgentBackend.names() == ("pi_native", "pi_durable")
    assert ab.AgentBackend.member_named("pi_native") is PiNativeBackend
    assert ab.AgentBackend.member_named("pi_durable") is PiDurableBackend
    assert ab.NewThreadBackend.selected() is PiNativeBackend


def test_a_backend_declared_after_sealing_fails_loud():
    with pytest.raises(ab.BackendDeclarationError):

        class LateBackend(ab.AgentBackend):  # noqa: F841
            pass

    assert ab.AgentBackend.names() == ("pi_native", "pi_durable")


@pytest.mark.parametrize("session_file, location", [
    (RegistrySessionLocation.from_path("/home/ts/wt/test/session.jsonl", Path("/home/ts/wt")),
     PiSessionFile("/home/ts/wt/test/session.jsonl")),
    (NoSessionFile(), UnstartedRuntime(PiNativeBackend)),
])
def test_runtime_location_round_trip_uses_declared_backend(session_file, location, *, thread_registration):
    thread = thread_registration.declare(NativeThread('location', frozenset(), Checkout(Path('/home/ts/wt/test')), session_file=session_file))
    restored = FieldCodec.decode(Thread, FieldCodec.encode(thread))
    restored = replace(restored, session_file=restored.session_file.bound_to(Path("/home/ts/wt")))
    assert restored.runtime == location
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
            "import agent_comms.execution.pi_events as p\n"
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
    thread = thread_registration.declare(NativeThread('owner', frozenset(), Checkout(Path('/tmp/owner'))))
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
            "from agent_comms.registry.threads import NativeThread\n"
            "NativeThread('a', frozenset(), '/tmp').backend\n"
            "assert 'agent_comms.execution.backend' not in sys.modules\n"
            "assert 'agent_comms.execution.tracked_turn' not in sys.modules\n"
            "assert 'agent_comms.execution.turn_watchdog' not in sys.modules\n"
        ), str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


# -- lookup -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_obtains_one_backend_per_session_from_the_thread_declaration(comms):
    from agent_comms.execution.acp import CommsAgent
    from agent_comms.errors import RelationViolationError
    from agent_comms.execution.child_process import ProcessIdentity
    from agent_comms.execution.native_arguments import GivenArguments, GivenBinary
    from agent_comms.execution.persistent_pi_session import PersistentPiSession

    agent = CommsAgent(
        comms, auto_wake=False, agent_bin=GivenBinary("pi"),
        agent_args=GivenArguments(("--provider", "p", "--model", "m")),
    )
    try:
        for name in ("session", "other"):
            thread = comms.registry.declare(NativeThread(
                name, frozenset(), Checkout(comms.root.parent), model=DeclaredModel('p/m'),
                process_identity=ProcessIdentity.capture(os.getpid())))
            await agent.sessions.bind_owned(thread, AcpSessionId(name))
        session = agent.inputs.state("session").owned()
        other = agent.inputs.state("other").owned()
        assert agent.inputs.state("attachment").executors() == ()
        assert session.executors() == (session,)
        assert session.configuration is other.configuration is agent.inputs.configuration
        assert session.stop is not other.stop
        thread = comms.registry.require("session")
        backend = agent.turns.backend_for("session", thread)
        assert type(backend) is PiNativeBackend
        assert agent.turns.backend_for("session", thread) is backend
        assert backend.configuration is session.configuration
        assert backend.arguments(thread) == session.configuration.agent_args.with_model("p/m").argv

        agent.turns.persistent_backends["other"] = PersistentPiSession()
        with pytest.raises(RelationViolationError):
            agent.turns.backend_for("other", thread)
    finally:
        await agent.shutdown()


# -- inbox records --------------------------------------------------------------


def test_inbox_records_say_which_inputs_they_carry():
    input_id = InputId.mint()
    request = InputRequest(
        input_id=input_id, content=InputContent(text="t"), when_busy=SteerInputPolicy,
    )
    assert request.carried_inputs == (input_id,)
    assert SendNow((input_id,)).carried_inputs == ()
