"""The AgentBackend family: registry, declaration checks, lookup and Pi conversions."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

import agent_comms.agent_backend as ab
import agent_comms.agent_events as ae
from agent_comms.child_process import ProcessIdentity
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.native_arguments import NativeArguments
from agent_comms.pi_native_backend import PI_0_85_1_EVENT_KINDS, PiNativeBackend, PiSessionFile
from agent_comms.pi_durable_backend import PiDurableBackend, DurableDatabase
from agent_comms.threads import Thread
from agent_comms.unstarted_runtime import UnstartedRuntime


# -- registry ---------------------------------------------------------------


def test_registry_holds_the_pi_native_backend_and_is_frozen():
    assert ab.AgentBackend.names() == ("pi_native", "pi_durable")
    assert ab.AgentBackend.decode("pi_native") is PiNativeBackend
    assert ab.AgentBackend.for_new_threads() is PiNativeBackend
    assert isinstance(ab.AgentBackend.__registry__, MappingProxyType)
    assert ab.AgentBackend.decode("pi_durable") is PiDurableBackend


def test_a_backend_declared_after_sealing_fails_loud():
    with pytest.raises(ab.BackendDeclarationError, match="registry is sealed"):

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


@pytest.mark.parametrize("row", json.loads((Path(__file__).parent / "fixtures/p117/main-registry-rows.json").read_text()))
def test_main_written_registry_location_round_trip(row):
    restored = FieldCodec.decode(Thread, row)
    expected = (UnstartedRuntime(PiNativeBackend) if row["session_file"] is None
                else PiSessionFile(row["session_file"]))
    assert restored.runtime == expected
    assert restored.to_wire() == row
    assert json.dumps(restored.to_wire()) == json.dumps(row)


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
    class HalfBackend(ab.Steers, ab.Compacts):
        pass

    HalfBackend.location_type = _Location
    HalfBackend.serves_threads = True
    HalfBackend.serves_new_threads = True
    HalfBackend.event_kinds = frozenset()
    with pytest.raises(ab.BackendDeclarationError) as error:
        ab.validate_backends([HalfBackend])
    message = str(error.value)
    for role in ("ReportsContext", "ConfiguresModel"):
        assert role in message
    assert "leaves methods abstract" in message
    for method in ("compact", "run_turn", "abort", "close_idle"):
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
    thread = comms.registry.new_thread('owner', frozenset(), '/tmp/owner', model='p/m')
    backend = TurnRunner.backend_for(runner, "session", thread)
    assert type(backend) is PiNativeBackend
    assert TurnRunner.backend_for(runner, "session", thread) is backend
    assert backend.arguments(thread) == _Runner.agent_args.with_model("p/m").argv

    from agent_comms.pi_native_backend import PersistentPiSession

    runner.persistent_backends["other"] = PersistentPiSession()
    with pytest.raises(RelationViolationError, match="declares pi_native"):
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


def test_session_entries_convert_to_transcript_entries_newest_first(tmp_path):
    session = tmp_path / "s.jsonl"
    _session_lines(session)
    source = PiSessionFile(str(session)).history_source(tmp_path, None)
    records = tuple(source.reverse(session.stat().st_size))
    page = records[:4]
    assert [record.entry.source_coordinates.id for record in page] == ["l1", "c1", "t1", "a1"]
    assert page[1].entry.summary == "earlier work"
    assert page[2].entry.message.text == "file body"
    assert page[3].entry.message.text == "hi back"
    assert page[2].entry.recorded_at is None  # an invalid external time stays undated
    assert page[3].entry.recorded_at.isoformat() == "2026-10-10T10:00:02+00:00"
    older = tuple(source.reverse(page[-1].start))
    assert [record.entry.source_coordinates.id for record in older] == ["u1", "s"]
    assert older[0].entry.message.text == "hello there"


def _pi_stub(tmp_path: Path, session: Path) -> Path:
    stub = tmp_path / "pi-stub"
    stub.write_text(f"#!{sys.executable}\n" + f"""
import json, sys
def send(event):
    print(json.dumps(event), flush=True)
state = {{"nativeInputProofCapability": "pi-native-input-v1-live-only", "sessionId": "same",
         "sessionFile": {str(session)!r}, "isStreaming": False, "isCompacting": False,
         "pendingMessageCount": 0, "model": {{"provider": "p", "id": "m", "contextWindow": 1000}}}}
for line in sys.stdin:
    command = json.loads(line)
    kind = command["type"]
    if kind == "get_state":
        send({{"type": "response", "command": kind, "id": command.get("id"), "success": True,
              "data": state}})
    elif kind == "get_session_stats":
        send({{"type": "response", "command": kind, "id": command.get("id"), "success": True,
              "data": {{"contextUsage": {{"tokens": 321, "contextWindow": 1000}}}}}})
    elif kind == "compact":
        send({{"type": "response", "command": kind, "id": command.get("id"), "success": True,
              "data": {{"summary": "summary of " + command.get("customInstructions", ""),
                        "firstKeptEntryId": "k1", "tokensBefore": 900}}}})
    elif kind == "prompt":
        send({{"type": "response", "command": kind, "id": command["id"], "success": True}})
        send({{"type": "turn_start"}})
        send({{"type": "message_start", "message": {{"role": "user", "content": command["message"],
              "inputId": command["inputId"]}}}})
        send({{"type": "message_update", "assistantMessageEvent": {{"type": "text_delta",
              "delta": "pong"}}, "message": {{"role": "assistant", "content": []}}}})
        send({{"type": "message_end", "message": {{"role": "assistant", "stopReason": "stop",
              "content": [{{"type": "text", "text": "pong"}}], "usage": {{"totalTokens": 5}}}}}})
        send({{"type": "turn_end"}})
        send({{"type": "agent_settled"}})
""")
    stub.chmod(0o755)
    return stub


@pytest.mark.usefixtures("native_rpc_fixture")
@pytest.mark.skipif(sys.platform == "win32", reason="executes a POSIX script")
async def test_pi_rpc_stream_converts_to_agent_events_through_the_backend(tmp_path, comms, monkeypatch):
    from agent_comms.native_session_reopen import NativeSessionIdentity

    # The stub is not a verified Pi package; its saved header is the session's identity.
    from agent_comms.native_pi import NativePiRpcLaunch

    # The stub is not a verified Pi package: its saved header is the session's
    # identity, and its acquired launch is the one a retained child is reused under.
    monkeypatch.setattr(NativeSessionIdentity, "locate", staticmethod(
        lambda package, path: NativeSessionIdentity("same", path)
    ))
    monkeypatch.setattr(NativePiRpcLaunch, "retained_managed", lambda self, *args, **options: self)
    session = tmp_path / "session.jsonl"
    project = tmp_path / "project"
    project.mkdir()
    session.write_text(json.dumps({
        "type": "session", "version": 3, "id": "same", "timestamp": "2026-10-10T10:00:00.000Z",
        "cwd": str(project),
    }) + "\n")
    thread = comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(project), session_file=str(session), model='p/m', process_identity=ProcessIdentity.capture(os.getpid())))
    published = []

    async def observe(observed, state, info):
        published.append((observed.name, info.context_used, info.context_size))

    backend = PiNativeBackend(str(_pi_stub(tmp_path, session)), NativeArguments.parse([]), comms, observe)
    starts = []
    finish = asyncio.Event()
    delivery = ab.TurnDelivery(
        worktree=str(project), environment={}, inbox=ab.TurnInbox(), finish_event=finish,
        send_boundary=None, interrupt_boundary=None,
        input_started=lambda public_id, native_id, text: starts.append(text) or True,
        request_progress=lambda progress, process: None,
        ui_request=None,
    )
    events = []
    try:
        async with asyncio.timeout(10):
            async for event in backend.run_turn(thread, ab.InputContent(text="ping"), delivery):
                events.append(event)
                if isinstance(event, ae.StreamSettled):
                    finish.set()
        kinds = [type(event) for event in events]
        assert len(events) > 1, events
        assert ae.Chunk in kinds and ae.ProviderUsage in kinds and ae.StreamSettled in kinds
        assert "".join(event.text for event in events if isinstance(event, ae.Chunk)) == "pong"
        assert isinstance(events[-1], ae.Done) and events[-1].ok and events[-1].text == "pong"
        assert starts == ["ping"]
        assert backend.child_state.retained

        usage = await backend.context_usage(thread)
        assert usage == ab.ContextUsage(tokens=321, window=1000)
        outcome = await backend.compact(thread, "keep the plan")
        assert outcome == ab.PlacedCompaction(
            summary="summary of keep the plan", first_kept="k1", tokens_before=900,
        )
        assert published and published[0][0] == "owner"
    finally:
        await backend.close_idle()


# -- inbox records --------------------------------------------------------------


def test_inbox_records_say_which_inputs_they_carry():
    request = ab.InputRequest(
        input_id=ab.InputId("q1"), content=ab.InputContent(text="t"), when_busy=ab.WhenBusy.STEER,
    )
    assert request.carried_inputs == (ab.InputId("q1"),)
    assert ab.SendNow((ab.InputId("q1"),)).carried_inputs == ()
    with pytest.raises(ValueError):
        ab.InputId("")
