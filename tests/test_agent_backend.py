"""The AgentBackend family: registry, declaration checks, lookup and Pi conversions."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

import agent_comms.agent_backend as ab
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.native_arguments import NativeArguments
from agent_comms.pi_durable_backend import PiDurableBackend
from agent_comms.pi_native_backend import PI_0_85_1_EVENT_KINDS, PiNativeBackend, PiSessionFile
from agent_comms.threads import Thread
from agent_comms.unstarted_runtime import UnstartedRuntime
from agent_comms.thread_model import DeclaredModel

# -- registry ---------------------------------------------------------------


def test_registry_holds_the_pi_native_backend_and_is_frozen():
    assert ab.AgentBackend.names() == ("pi_native", "pi_durable")
    assert ab.AgentBackend.member_named("pi_native") is PiNativeBackend
    assert ab.AgentBackend.for_new_threads() is PiNativeBackend
    assert isinstance(ab.AgentBackend.__registry__, MappingProxyType)
    assert ab.AgentBackend.member_named("pi_durable") is PiDurableBackend


def test_a_backend_declared_after_sealing_fails_loud():
    with pytest.raises(ab.BackendDeclarationError):

        class LateBackend(ab.AgentBackend):  # noqa: F841
            pass

    assert ab.AgentBackend.names() == ("pi_native", "pi_durable")


def test_pi_native_serves_threads_with_every_thread_capability():
    assert PiNativeBackend.serves_threads
    for role in (*ab.THREAD_CAPABILITIES, ab.Interrupts, ab.Forks):
        assert issubclass(PiNativeBackend, role), role
    assert not PiNativeBackend.__abstractmethods__
    assert PiNativeBackend.location_type is PiSessionFile


@pytest.mark.parametrize("location", [
    PiSessionFile("/home/ts/wt/test/session.jsonl"), UnstartedRuntime(PiNativeBackend),
])
def test_runtime_location_round_trip_uses_declared_backend(location, *, thread_registration):
    thread = thread_registration.new_thread('location', frozenset(), '/home/ts/wt/test', session_file=location.read_key)
    restored = FieldCodec.decode(Thread, FieldCodec.encode(thread))
    assert restored.runtime == location
    assert restored.runtime is restored.runtime
    assert restored.backend is PiNativeBackend




# -- declaration checks -------------------------------------------------------


class _Location(ab.RuntimeLocation):
    def location_payload(self):
        return None


class _OtherLocation(ab.RuntimeLocation):
    def location_payload(self):
        return None


def _declared(name, *roles, location=_Location, new_threads=False, serves=True, kinds=frozenset()):
    return type(name, roles or (object,), {
        "location_type": location, "serves_new_threads": new_threads,
        "serves_threads": serves, "event_kinds": kinds,
    })


def test_validation_names_missing_roles_and_abstract_methods():
    class HalfBackend(ab.Steers):
        pass

    HalfBackend.location_type = _Location
    HalfBackend.serves_threads = True
    HalfBackend.serves_new_threads = True
    HalfBackend.event_kinds = frozenset()
    with pytest.raises(ab.BackendDeclarationError) as error:
        ab.validate_backends([HalfBackend])
    message = str(error.value)
    for role in ("Compacts",):
        assert role in message
    assert "leaves methods abstract" in message
    for method in ("run_turn", "abort", "close_idle"):
        assert repr(method) in message


def test_validation_requires_one_location_type_per_backend_and_one_default():
    first = _declared("FirstBackend", serves=False, new_threads=True)
    second = _declared("SecondBackend", serves=False, new_threads=True)
    with pytest.raises(ab.BackendDeclarationError) as error:
        ab.validate_backends([first, second])
    assert "belongs to several backends ['FirstBackend', 'SecondBackend']" in str(error.value)
    assert "exactly one backend must serve new threads" in str(error.value)
    ab.validate_backends([first, _declared("Other", serves=False, location=_OtherLocation)])


def test_validation_requires_a_location_and_event_kinds():
    bare = type("BareBackend", (), {"serves_threads": False, "serves_new_threads": True})
    with pytest.raises(ab.BackendDeclarationError) as error:
        ab.validate_backends([bare])
    assert "BareBackend declares no RuntimeLocation type" in str(error.value)
    assert "BareBackend declares no event_kinds" in str(error.value)


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
    thread = thread_registration.new_thread('owner', frozenset(), '/tmp/owner')
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
            "from agent_comms.registration import Registration\n"
            "Registration(Path(sys.argv[1])/'registry.json').new_thread('a', frozenset(), '/tmp').backend\n"
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
    thread = comms.registry.new_thread('owner', frozenset(), '/tmp/owner', model=DeclaredModel('p/m'))
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
    request = ab.InputRequest(
        input_id=ab.InputId("q1"), content=ab.InputContent(text="t"), when_busy=ab.WhenBusy.STEER,
    )
    assert request.carried_inputs == (ab.InputId("q1"),)
    assert ab.SendNow((ab.InputId("q1"),)).carried_inputs == ()
    with pytest.raises(ValueError):
        ab.InputId("")
