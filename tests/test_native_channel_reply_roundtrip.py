"""A published native reply automatically reaches the original channel sender."""
import asyncio
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from acp.schema import SessionNotification, TextContentBlock

from agent_comms.execution.acp import CommsClient
from agent_comms.acp_extension import (
    CursorAdvancedUpdate,
    TurnChangedUpdate,
    VerifiedCursorObservation,
    decode_updates,
)
from agent_comms.comms import wire
from agent_comms.execution.controller import SessionUpdateSink
from agent_comms.execution.launch_runtime import RestoredRuntime
from agent_comms.execution.native_input_record import TriageNativeExecution
from agent_comms.execution.owner_cutover import PreserveOwnerRuntime
from agent_comms.execution.owner_restart import OwnerRestartRequest
from agent_comms.execution.pi_vocabulary import OffThinkingLevel
from agent_comms.execution.restart_scope import OwnersNamed
from agent_comms.execution.turn_lease import NoTurnRecord
from agent_comms.nominal.lifecycle_capability import Executing
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.threads import NativeThread
from bus_history import read_history
from bus_record_fixture import configure_local_native, prepare_native_comms, start_source_owner


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    ("restart_after_reply", "reject_triage", "broadcast"),
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
    ids=["reply", "saved-restart", "rejected-triage-continuity", "parallel-channel"],
)
async def test_native_channel_reply_automatically_reaches_original_sender(tmp_path, monkeypatch, restart_after_reply, reject_triage, broadcast, *, source_reads, native_runtime_package):
    runtime = RestoredRuntime(Path(sys.executable))
    requests = []
    failures = []
    request_times = []
    request_inputs = []
    committed = set()
    parallel_triage = threading.Event()
    seed_count = 2 if restart_after_reply else 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.headers["Authorization"] == "Bearer local-only"
                requests.append(request)
                request_times.append(time.monotonic())
                assert len(requests) <= (8 if broadcast else 2 + seed_count + restart_after_reply + 3 * reject_triage), "Reply observation caused replay or ping-pong"
                # The current original native reservation owns stage. Join its
                # exact ID to the saved input; prompt prose is not a stage codec.
                from agent_comms.coordination.coordinator import Coordination
                from agent_comms.coordination.tables.assignments import WakeAssignment
                from agent_comms.execution.native_runtime_input import NativeRuntimeInput
                from agent_comms.execution.transcript_sources import NativeTranscript

                # Pi commits each tracked input in a request's context (session
                # entry, generation, context digest) right before sending it
                # (native-input.patch), and Core records that commit from Pi's
                # emitted record. So the input this request carries is the one
                # recorded since the previous request; a seed or continuity
                # request carries none. The record can trail the request.
                deadline = time.monotonic() + 2
                while True:
                    with Coordination(comms.root / "coordination.sqlite3") as store:
                        with store.session.read():
                            pending = tuple(row for row in NativeRuntimeInput.select(
                                store.session._connection,
                                where=(
                                    "owner_lookup=? AND session_id IS NOT NULL AND session_entry_id IS NOT NULL"
                                    if broadcast else "session_id IS NOT NULL AND session_entry_id IS NOT NULL"
                                ),
                                parameters=((comms.registry.require(request["model"]).incarnation.lookup,) if broadcast else ()),
                            ) if row.input_id not in committed)
                    if pending or time.monotonic() > deadline:
                        break
                    time.sleep(0.02)
                committed.update(row.input_id for row in pending)
                with Coordination(comms.root / "coordination.sqlite3") as store:
                    with store.session.read():
                        # The commit's transcript entry must be the session's latest tracked user.
                        current_inputs = tuple((row, next(
                            entry for entry in NativeTranscript(Path(row.session_file)).tail()
                            if entry.input_boundary).require_tracked_user()) for row in pending)
                        pending = tuple((row, user) for row, user in current_inputs if row.input_id == user.require_input_id())
                        if not pending:
                            assert not broadcast
                            request_text = json.dumps(request.get("messages", ()))
                            assert (
                                "RETAINED_NATIVE_HISTORY_" in request_text
                                or "CONTINUITY_HUMAN_INPUT" in request_text
                            )
                            original = None
                            is_triage = requires_answer = False
                        else:
                            assert len(pending) == 1, tuple(
                                (row.input_id, row.stage.declared_name, row.session_file, str(row.session_id))
                                for row, _ in pending)
                            original, user = pending[0]
                            assert user.require_input_id() == original.input_id
                            is_triage = original.stage is TriageNativeExecution
                            assignments = tuple(WakeAssignment.one(
                                store.session._connection, assignment_id=identity
                            ) for identity in original.execution.source_assignment_ids(
                                store.session._connection, original.input_id
                            ))
                            requires_answer = any(
                                row.source == question.reference for row in assignments
                            ) if broadcast else False
                if broadcast:
                    assert original is not None
                    request_inputs.append({"input_id": original.input_id,
                                           "stage": original.stage.declared_name,
                                           "owner": comms.registry.snapshot().born(original.owner_lookup).name})
                if broadcast and len(requests) <= 2:
                    # Both actual owners must reach the endpoint while the
                    # first request is still open. No model turn may retain
                    # global bus/claim_scope ownership through its response.
                    if len(requests) == 2:
                        parallel_triage.set()
                    assert parallel_triage.wait(5), "Independent owner blocked behind another native request"
                text = (
                    ('{"decision":"FULL"}' if broadcast and requires_answer
                     else '{"decision":"IGNORE"}')
                    if is_triage
                    else "ROUNDTRIP_NATIVE_REPLY"
                )
                if reject_triage and len(requests) in (2, 3):
                    # Actual successful native output from the live failure:
                    # bare IGNORE violates the required JSON decision contract.
                    # An unreadable decision earns one re-ask (request 3); a second
                    # unreadable answer fails the claim.
                    text = "IGNORE"
                chunk = {
                    "id": f"roundtrip-{len(requests)}",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                }
                body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as error:
                failures.append(repr(error))
                self.send_error(400, "Local fixture assertion failed")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    configure_local_native(
        tmp_path / "config", monkeypatch, provider="roundtrip-local", port=server.server_port,
        models=("questioner", "answerer", "answerer2") if broadcast else ("fixture",),
    )
    comms = prepare_native_comms(wire(tmp_path / "wire"))
    projects = {}
    for name in (("questioner", "answerer", "answerer2") if broadcast else ("questioner", "answerer")):
        project = tmp_path / name
        project.mkdir()
        projects[name] = project
        comms.registry.declare(
            NativeThread(name, frozenset({'team'}), Checkout(project), model=DeclaredModel(f'roundtrip-local/{name}' if broadcast else 'roundtrip-local/fixture'), thinking_level=OffThinkingLevel)
        )
    root_id = comms.registry.store.prepare_native_root().namespace
    attachment = CommsClient(
        comms,
        auto_wake=True
    )
    facts = []

    class Client(SessionUpdateSink):
        async def publish_update(self, **kwargs):
            notification = SessionNotification.model_validate(kwargs)
            facts.extend(decode_updates(notification.update.field_meta))
            return True

    attachment.on_connect(Client())

    async def until(predicate, description):
        try:
            async with asyncio.timeout(25):
                while not predicate():
                    assert not failures, failures
                    await asyncio.sleep(0.05)
        except TimeoutError:
            print("TIMEOUT", description, "ACP facts", repr(facts), flush=True)
            print("wire history", read_history(comms), flush=True)
            for diagnostic in sorted((comms.root / "diagnostics").glob("*")):
                if diagnostic.is_file():
                    print(diagnostic.name, diagnostic.read_text(), flush=True)
            with sqlite3.connect(comms.root / "coordination.sqlite3") as db:
                print(
                    "native receipts",
                    db.execute("SELECT * FROM native_runtime_input").fetchall(),
                    flush=True,
                )
            raise

    try:
        for name in projects:
            await asyncio.to_thread(start_source_owner, comms, name, runtime)
        await attachment.load_session(cwd=str(projects["questioner"]), session_id="questioner")
        if restart_after_reply:
            for name, project in projects.items():
                await attachment.load_session(cwd=str(project), session_id=name)
                async with asyncio.timeout(35):
                    seed = await attachment.prompt(
                        session_id=name,
                        prompt=[
                            TextContentBlock(type="text", text=f"RETAINED_NATIVE_HISTORY_{name}")
                        ],
                    )
                assert seed.stop_reason == "end_turn"
                assert comms.registry.require(name).session_file
            assert len(requests) == seed_count
        facts.clear()
        question = comms.messaging.send(
            "questioner", "#team", "ROUNDTRIP_QUESTION" if broadcast else "@answerer ROUNDTRIP_QUESTION"
        ).message
        if broadcast:
            from agent_comms.coordination.coordinator import Coordination
            from agent_comms.coordination.tables.assignments import WakeAssignment

            await until(lambda: len(read_history(comms, "#team")) == 3,
                        "Both independent native replies published")
            replies = read_history(comms, "#team")[1:]
            assert {row.sender for row in replies} == {"answerer", "answerer2"}
            assert all(row.body == "ROUNDTRIP_NATIVE_REPLY" for row in replies)
            assert len({row.message_id for row in replies}) == 2
            assert parallel_triage.is_set() and request_times[1] - request_times[0] < 5

            def settled():
                with Coordination(str(comms.root / "coordination.sqlite3")) as store:
                    with store.session.read():
                        rows = WakeAssignment.select(store.session._connection)
                        return len(rows) == 6 and all(not row.lifecycle.successors() for row in rows)

            await until(settled, "Original and every peer-reply claim settled")
            with sqlite3.connect(comms.root / "coordination.sqlite3") as db:
                rows = db.execute("SELECT input_id, session_file, session_entry_id FROM native_runtime_input").fetchall()
            assert 7 <= len(rows) <= 8 and all(row[2] for row in rows)
            assert len({row[0] for row in rows}) == len(rows)
            assert len({row[1] for row in rows}) == 3
            await asyncio.sleep(1.2)
            assert 7 <= len(requests) <= 8 and not failures
            assert len(read_history(comms, "#team")) == 3
            # The real request reader owns every timing sample. They must
            # survive independently of registry phase changes and publication.
            observations = [json.loads(line)
                            for path in (comms.root / "diagnostics").glob("*.requests.jsonl")
                            for line in path.read_text().splitlines()]
            native = [row for row in observations if "native" in row]
            # Samples group by their own request. Which input a request carries
            # is owned by its committed context, never copied into its timing.
            requests_observed = {}
            for row in native:
                requests_observed.setdefault(row["native"]["requestId"], []).append(row)
            assert sorted(group[0]["turn"]["identity"]["incarnation"]["name"]
                          for group in requests_observed.values()) == sorted(
                row["owner"] for row in request_inputs)
            for samples in requests_observed.values():
                assert {"preparing", "dispatch", "first_event", "finished"} <= {
                    row["native"]["stage"] for row in samples
                }
                assert len({json.dumps(row["turn"], sort_keys=True) for row in samples}) == 1
                assert len({tuple(row["native_process"].items()) for row in samples}) == 1
            for original in request_inputs:
                acquisitions = [row["acquisition"] for row in observations
                                if row.get("input_id") == original["input_id"]
                                and "acquisition" in row]
                assert len(acquisitions) == 1
                selection = acquisitions[0]["operations"]["native_launch_selection"]
                # Each original input owns its acquisition counters
                # (PrivateSendClaim.reserve, diagnostics.py: "The original input
                # owns these counters, not the recipient's cursor lifetime"), so
                # each records exactly one launch selection of its own.
                assert selection["count"] == 1 and selection["total_ns"] > 0
            return
        await until(lambda: len(requests) >= seed_count + 1, "B actual native request")
        await until(
            lambda: len(read_history(comms, "#team")) >= 2, "B published actual reply"
        )
        reply = read_history(comms, "#team")[-1]
        assert reply.sender == "answerer" and reply.body == "ROUNDTRIP_NATIVE_REPLY"
        # No user prompt, explicit inbox drain, participant insertion, or selected claim seeding.
        await until(lambda: len(requests) == seed_count + 2 + reject_triage, "A automatically observed native reply")
        users = [item for item in requests[seed_count + 1]["messages"] if item["role"] == "user"]
        assert reply.body in json.dumps(users[-1]["content"])
        # The same response remains available through canonical natural-turn awareness.
        # Reading this projection proves neither model delivery nor ACK; native delivery
        # above must have happened automatically before inspecting pointers.
        owner = comms.registry.require("questioner")
        received = comms.bus.bus.received(owner.incarnation.lookup)
        assert reply.reference in [record.reference for record in received]

        def selected_turn_finished():
            states = [fact.state for fact in facts if isinstance(fact, TurnChangedUpdate)]
            return any(
                not bool(state.active.items_with(Executing, state.active))
                and state.finished != NoTurnRecord()
                for state in states
            )

        await until(selected_turn_finished, "Selected turn end published to the open session")
        await until(
            lambda: any(
                isinstance(fact, CursorAdvancedUpdate)
                and isinstance(fact.envelope.observation, VerifiedCursorObservation)
                and fact.envelope.observation.cursor.injected_seq == reply.seq
                for fact in facts
            ),
            "A ACP feedback proves exact reply injection",
        )
        with sqlite3.connect(comms.root / "coordination.sqlite3") as db:
            rows = db.execute(
                "SELECT input_id, session_file, session_entry_id FROM native_runtime_input"
            ).fetchall()
        delivered = [
            fact.envelope.observation.cursor
            for fact in facts
            if isinstance(fact, CursorAdvancedUpdate)
            if isinstance(fact.envelope.observation, VerifiedCursorObservation)
            if fact.envelope.observation.cursor.injected_seq == reply.seq
        ]
        # A rejected result adds a non-waking alert to the canonical source
        # prefix; injection still names the exact original reply.
        assert delivered[-1].covered_seq == reply.seq + reject_triage
        assert delivered[-1].reference_stage is TriageNativeExecution
        assert delivered[-1].reference.require_recorded().input_id in {row[0] for row in rows}
        # A rejected triage adds its one replacement input for the same reply.
        assert len(rows) == 2 + reject_triage and all(row[2] for row in rows)
        assert len({row[0] for row in rows}) == 2 + reject_triage
        assert len({row[1] for row in rows}) == 2  # Two actual native owners/children.
        await asyncio.sleep(1.2)  # Beyond the watcher fallback interval; no recursive wake/replay.
        assert len(requests) == seed_count + 2 + reject_triage and not failures
        await until(
            lambda: len(read_history(comms, "#team")) == 2 + reject_triage,
            "Canonical reply and any rejected-result alert published",
        )
        if reject_triage:
            from agent_comms.coordination.assignment_states import FailedAssignment
            from agent_comms.coordination.coordinator import Coordination
            from agent_comms.execution.historical_native_inputs import read_historical_native_inputs

            with Coordination(str(comms.root / "coordination.sqlite3")) as store:
                rejected = read_historical_native_inputs(
                    store,
                    wire_root_id=root_id,
                    recipient_lookup=owner.incarnation.lookup,
                    source_seq=reply.seq,
                    source_reads=source_reads,
                )
                assert len(rejected) == 1 and rejected[0].proves_triage_source(rejected)
                claim = store.assignments.get(rejected[0].assignment_id)
                assert type(claim.lifecycle) is FailedAssignment
                rejected_input = rejected[0].input_id
            body = read_history(comms, "#team")[-1].body
            assert "invalid triage decision" in body
            # The terminal names what actually arrived and what is accepted, so the reader
            # can send a valid decision instead of reading the session file to find out.
            assert "Received: 'IGNORE'" in body
            assert "IGNORE" in body and "FULL" in body
            fresh = comms.messaging.send("answerer", "#team", "CONTINUITY_FRESH_SOURCE").message
            await until(lambda: len(requests) == 4, "Fresh channel source authorized without retrying rejected input")
            await until(
                lambda: any(isinstance(fact, CursorAdvancedUpdate)
                            and isinstance(fact.envelope.observation, VerifiedCursorObservation)
                            and fact.envelope.observation.cursor.injected_seq == fresh.seq
                            for fact in facts),
                "ACP proves fresh source after rejected triage",
            )
            async with asyncio.timeout(35):
                answer = await attachment.prompt(
                    session_id="questioner",
                    prompt=[TextContentBlock(type="text", text="CONTINUITY_HUMAN_INPUT")],
                )
            assert answer.stop_reason == "end_turn" and len(requests) == 5
            assert "CONTINUITY_HUMAN_INPUT" in json.dumps(requests[-1])
            with sqlite3.connect(comms.root / "coordination.sqlite3") as db:
                assert db.execute("SELECT count(*) FROM native_runtime_input WHERE input_id=?",
                                  (rejected_input,)).fetchone() == (1,)
                assert db.execute("SELECT count(*) FROM native_runtime_input WHERE session_id IS NULL").fetchone() == (0,)
        if restart_after_reply:
            # Continue the same real owner/ACP/native journey from saved state.
            await until(
                lambda: all(not comms.registry.require(name).executing for name in projects),
                "Both native turns released before guarded restart",
            )
            before = comms.registry.snapshot()
            saved = {
                name: Path(before.threads[name].runtime.read_key).read_bytes() for name in projects
            }
            await attachment.shutdown()
            restart = OwnerRestartRequest(OwnersNamed(tuple(projects)), launch_runtime=runtime)
            await asyncio.to_thread(PreserveOwnerRuntime().restart, comms.owners, restart)
            after = comms.registry.snapshot()
            for name in projects:
                old, current = before.threads[name], after.threads[name]
                assert current.incarnation == old.incarnation
                assert current.process_identity != old.process_identity
                assert after.claim_generation(name) > before.claim_generation(name)
                assert current.session_file == old.session_file
                assert Path(current.runtime.read_key).read_bytes() == saved[name]
            assert len(requests) == seed_count + 2, "Restart replayed saved input"
            attachment = CommsClient(
                comms, auto_wake=True
            )
            attachment.on_connect(Client())
            await attachment.load_session(cwd=str(projects["questioner"]), session_id="questioner")
            async with asyncio.timeout(35):
                answer = await attachment.prompt(
                    session_id="questioner",
                    prompt=[TextContentBlock(type="text", text="EXPLICIT_POST_RESTART_INPUT")],
                )
            assert answer.stop_reason == "end_turn"
            assert len(requests) == seed_count + 3
            assert not failures, failures
            entries = [json.loads(line) for line in Path(after.threads["questioner"].runtime.read_key).read_text().splitlines()]
            inputs = [row['message'] for row in entries if row['type'] == 'message' and row['message']['role'] == 'user']
            assert sum('EXPLICIT_POST_RESTART_INPUT' in json.dumps(row['content']) for row in inputs) == 1
            assert Path(after.threads["questioner"].runtime.read_key).read_bytes().startswith(saved['questioner'])
    finally:
        await attachment.shutdown()
        try:
            await asyncio.to_thread(comms.owners.stop, *projects)
        finally:
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)
        assert all(not comms.registry.require(name).process_alive for name in projects)
