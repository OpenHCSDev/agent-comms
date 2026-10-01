"""Physical native parent -> normal fork -> first ACP send, without replay."""

import asyncio
import json
import os
import time
from abc import abstractmethod
from pathlib import Path

import pytest
from acp.schema import TextContentBlock

from agent_comms.pi_summary_payloads import SelectedModel
from agent_comms.relationships import RelationshipEdit
from agent_comms.acp import CommsClient
from agent_comms.acp_extension import (
    CompactionChangedUpdate,
    CompactionCommittedUpdate,
    CompactionPublishedUpdate,
    InputFailedUpdate,
    RequestFailedUpdate,
    decode_updates,
)
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.input_disposition import InputDispositions
from agent_comms.pi_commands import GetSessionStats, GetState
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.selected_pi_route import read_selected_compaction_decision
from agent_comms.thread_management import ForkSpec
from agent_comms.threads import Thread
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture

pytest_plugins = ("test_backend_native_lifecycle",)


class OwnerAdmissionCase(DeclaredFamily, affix="Case"):
    @abstractmethod
    async def create(self, comms): ...

    @abstractmethod
    async def first_input(self, attachment, child): ...

    @abstractmethod
    def verify_fraction(self, fraction): ...

    @abstractmethod
    def verify_history(self, native, thread, original): ...

    def verify_relationships(self, comms, thread):
        assert comms.registry.require(thread.name).incarnation == thread.incarnation


class OrdinaryOwnerCase(OwnerAdmissionCase):
    repetitions = 2400

    async def create(self, comms):
        await asyncio.to_thread(comms.owners.start, "physical-parent")
        return comms.registry.require("physical-parent")

    async def first_input(self, attachment, child):
        return await attachment.prompt(
            child.name, [TextContentBlock(type="text", text="hey Boss")]
        )

    def verify_fraction(self, fraction):
        assert 0.60 < fraction < 0.70

    def verify_history(self, native, thread, original):
        assert Path(thread.session_file) == native.session
        assert native.session.read_bytes().startswith(original)


class ForkOwnerCase(OwnerAdmissionCase):
    repetitions = 900

    async def create(self, comms):
        return await asyncio.to_thread(
            comms.threads.fork,
            ForkSpec(
                name="physical-child",
                parent="physical-parent",
                task="hey Boss",
            ),
        )

    async def first_input(self, attachment, child):
        # Normal fork already journaled and dispatched the first input.
        # A second ACP prompt would test queueing instead of fork first-send.
        return None

    def verify_fraction(self, fraction):
        assert 0.20 < fraction < 0.27

    def verify_history(self, native, thread, original):
        assert Path(thread.session_file) != native.session
        assert native.session.read_bytes() == original

    def verify_relationships(self, comms, thread):
        super().verify_relationships(comms, thread)
        parent = comms.registry.require(thread.parent)
        assert parent.name == "physical-parent"
        edge = comms.relationships.edit(parent.name, RelationshipEdit.decode('add'), thread.name, "Actual native fork")
        assert edge.pair_identity == frozenset((parent.incarnation, thread.incarnation))
        for owner, peer in ((parent, thread), (thread, parent)):
            saved = comms.relationships.collaborations(owner.name)
            assert len(saved) == 1
            assert saved[0].counterpart(owner) == peer.incarnation
        reopened = Comms(comms.root)
        assert reopened.relationships.collaborations(parent.name) == (edge,)
        assert reopened.registry.require(parent.name) == parent


@pytest.mark.parametrize(
    "case",
    [member() for member in OwnerAdmissionCase.members_with(OwnerAdmissionCase)],
    ids=lambda case: case.declared_name,
)
async def test_underbudget_physical_native_owner_answers_without_compaction(
    native_backend, monkeypatch, case
):
    native = native_backend
    # Select a normal bounded model before creating physical parent history.
    config_path = native.config / "models.json"
    config = json.loads(config_path.read_text())
    config["providers"]["response-local"]["models"][0]["contextWindow"] = 32768
    config_path.write_text(json.dumps(config))
    (native.config / "settings.json").write_text(
        json.dumps(
            {
                "compaction": {"enabled": True, "reserveTokens": 2048, "keepRecentTokens": 1024},
                "retry": {"enabled": False, "maxRetries": 0},
            }
        )
    )
    parent_text = (
        "PHYSICAL_PARENT_CONTEXT " + "Retained architecture observation. " * case.repetitions
    )
    result = await native.run(parent_text)
    print("PARENT_NATIVE_RESULT", repr(result[-1]), flush=True)
    assert result[-1].ok, result[-1]
    assert native.provider.posts == 1
    retained = native.persistent.custody.idle()

    async def probe(command):
        retained.child.proc.stdin.write(PiRpcChannel.command_bytes(command))
        await retained.child.proc.stdin.drain()
        raw = await retained.child.reader.readline()
        response = PiRpcChannel.decode_record(raw, strict=True)
        assert response.success
        return response.data

    state = await probe(GetState(id="parent-state"))
    stats = await probe(GetSessionStats(id="parent-stats"))
    fraction = stats.context_usage.tokens / state.model.context_window
    case.verify_fraction(fraction)
    print("PHYSICAL_PARENT_STATS", repr(stats), flush=True)
    assert state.model.context_window == 32768
    print("PHYSICAL_PARENT_STATE", repr(state), flush=True)
    decision = await read_selected_compaction_decision(
        native.persistent,
        session_file=str(native.session),
        expected_package=Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]),
        selected=SelectedModel(state.model.provider, state.model.id, state.model.context_window),
    )
    print("ACTUAL_SELECTED_COMPACTION_DECISION", repr(decision), flush=True)
    assert decision.enabled
    assert not decision.trigger
    original = native.session.read_bytes()
    source_rows = [json.loads(line) for line in original.splitlines()]
    selected_messages = [row["message"] for row in source_rows if row["type"] == "message"]
    serialized_bytes = len(
        json.dumps(selected_messages, ensure_ascii=False, separators=(",", ":")).encode()
    )
    effective_tokens = state.model.context_window - decision.reserve_tokens
    old_mixed_unit_budget = effective_tokens * 0.75
    assert stats.context_usage.tokens < effective_tokens
    assert serialized_bytes > old_mixed_unit_budget
    print(
        "TOKEN_BYTE_MISMATCH",
        "tokens",
        stats.context_usage.tokens,
        "effective_tokens",
        effective_tokens,
        "serialized_context_bytes",
        serialized_bytes,
        "old_mixed_unit_budget",
        old_mixed_unit_budget,
        flush=True,
    )

    native.provider.text = "FIRST_OWNER_ANSWER"
    await native.persistent.close()
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    monkeypatch.setenv(
        "AGENT_COMMS_AGENT_ARGS", "--offline --no-extensions --no-skills --no-context-files"
    )
    monkeypatch.setenv(
        "PATH", str(Path(os.sys.executable).parent) + os.pathsep + os.environ["PATH"]
    )
    debug_log = native.project / "runtime-debug.log"
    monkeypatch.setenv("AGENT_COMMS_DEBUG_LOG", str(debug_log))
    comms = Comms(native.root)
    comms.registry.declare(
        Thread(
            "physical-parent",
            frozenset(),
            str(native.project),
            session_file=str(native.session),
            model="response-local/fixture",
            thinking_level="off",
        )
    )
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    root_id = os.environ["AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"]
    comms.owners.pin_private_nk_launch(comms.root, root_id, package)
    attachment = CommsClient(
        comms,
        runtime_enabled=True,
        private_nk_native_package=package,
        private_nk_wire_root_id=root_id,
    )
    facts = []

    class Client:
        async def session_update(self, **kwargs):
            facts.extend(decode_updates(kwargs["update"].get("_meta")))

    attachment.on_connect(Client())
    child = None
    first_send_started = time.monotonic()
    try:
        child = await case.create(comms)
        async with asyncio.timeout(15):
            while True:
                try:
                    await attachment.load_session(cwd=str(native.project), session_id=child.name)
                    break
                except Exception:
                    if not comms.registry.require(child.name).process_alive:
                        raise
                    await asyncio.sleep(0.1)
        try:
            async with asyncio.timeout(20 - (time.monotonic() - first_send_started)):
                response = await case.first_input(attachment, child)
                # ACP end_turn can mean local queue acceptance; actual native
                # answer publication is the completion boundary for both cases.
                while True:
                    request_failures = [
                        fact.failure for fact in facts if isinstance(fact, RequestFailedUpdate)
                    ]
                    input_failures = [
                        fact.failure for fact in facts if isinstance(fact, InputFailedUpdate)
                    ]
                    assert not request_failures, "\n".join(
                        failure.feedback for failure in request_failures
                    )
                    assert not input_failures, "\n".join(
                        failure.description + "\n" + failure.input_disposition
                        for failure in input_failures
                    )
                    thread = comms.registry.require(child.name)
                    rows = [
                        json.loads(line)
                        for line in Path(thread.session_file).read_text().splitlines()
                    ]
                    answers = [
                        row["message"] for row in rows
                        if row["type"] == "message" and row["message"].get("role") == "assistant"
                    ]
                    if any("FIRST_OWNER_ANSWER" in json.dumps(answer) for answer in answers):
                        break
                    await asyncio.sleep(0.05)
        except Exception as error:
            rows = InputDispositions(comms.root / InputDispositions.filename).read().rows
            print(
                "FIRST_SEND_ERROR",
                repr(error),
                "facts",
                repr(facts),
                "dispositions",
                repr(rows),
                "provider_calls",
                native.provider.posts,
                flush=True,
            )
            raise
        latency = time.monotonic() - first_send_started
        print("FIRST_SEND_LATENCY_SECONDS", latency, "ACP_RESPONSE", repr(response), flush=True)
        print(
            "FIRST_SEND_PROVIDER_REQUEST_COUNT", native.provider.posts,
            "REQUEST_BYTES", [len(json.dumps(request)) for request in native.provider.requests],
            flush=True,
        )
        assert latency < 20
        assert not any(isinstance(fact, CompactionChangedUpdate) for fact in facts)
        assert not any(isinstance(fact, CompactionCommittedUpdate) for fact in facts)
        assert not any(isinstance(fact, CompactionPublishedUpdate) for fact in facts)
        thread = comms.registry.require(child.name)
        assert thread.session_file

        entries = [json.loads(line) for line in Path(thread.session_file).read_text().splitlines()]
        assert sum(entry["type"] == "compaction" for entry in entries) == 0
        users = [
            entry["message"]
            for entry in entries
            if entry["type"] == "message" and entry["message"].get("role") == "user"
        ]
        assert any("PHYSICAL_PARENT_CONTEXT" in json.dumps(message["content"]) for message in users)
        assert "FIRST_OWNER_ANSWER" in json.dumps(entries)
        assert sum("hey Boss" in json.dumps(message["content"]) for message in users) == 1
        assert native.provider.posts == 2  # physical parent, first answer only
        assert "PHYSICAL_PARENT_CONTEXT" in json.dumps(native.provider.requests[-1]["messages"])
        assert "hey Boss" in json.dumps(native.provider.requests[-1]["messages"])
        case.verify_history(native, thread, original)
        case.verify_relationships(comms, thread)
        await asyncio.sleep(1.2)
        assert native.provider.posts == 2  # No uncertain original replay or unsolicited retry.
    finally:
        await attachment.shutdown()
        for worker_log in sorted((comms.root / "diagnostics").glob("owner-*.log")):
            print("ACTUAL_WORKER_LOG", worker_log.name, worker_log.read_text(), flush=True)
        print(
            "ACTUAL_RUNTIME_DEBUG",
            debug_log.read_text() if debug_log.exists() else "",
            flush=True,
        )
        if child is not None:
            await asyncio.to_thread(comms.owners.stop, child.name)
            assert not comms.registry.require(child.name).process_alive
