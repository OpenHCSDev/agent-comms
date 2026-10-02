"""Normal prepared bundle: selected RPC -> native commit -> one original bind."""

import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agent_comms import agent_events as events
from agent_comms.child_process import AttachedChild, ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordinator import Coordination
from agent_comms.goals import Goal
from agent_comms.input_attempt import NotSentInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.owner_compaction_adaptive import maybe_compact_owner_turn
from agent_comms.registration import Registration
from agent_comms.runtime_info import AgentRuntimeInfo
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from retained_native_fixture import retained_native_host

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
pytestmark = pytest.mark.skipif(not PACKAGE, reason="Normal prepared native bundle required")


def record_fixture_history(inputs, owner, admission):
    for index in range(5):
        key = f"acp:seed-{owner}-{index}"
        text = f"Question {index}"
        native_id = str(index + 1).zfill(32)
        inputs.record(key, seq=None, owner=owner, admission=admission, target=owner, text=text)
        inputs.bind(
            key, admission=admission, turn_id=f"seed-{index}", native_id=native_id, text=text
        )
        inputs.started(key, turn_id=f"seed-{index}", native_id=native_id, text=text)


@asynccontextmanager
async def owner_fixture(
    tmp_path, monkeypatch, *, goal=True, response_gate: asyncio.Event | None = None
):
    package = Path(PACKAGE).resolve()
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path))
    root_id = Comms(tmp_path).messaging.initialize_private_initial_protocol()
    monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID", root_id)
    monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE", str(package))
    launcher = "pi"
    repo = Path(__file__).resolve().parents[1]
    requests = []

    async def serve(reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            length = next(
                int(line.split(b":", 1)[1])
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            request = json.loads(await reader.readexactly(length))
            requests.append(request)
            (tmp_path / "provider-requests.json").write_text(json.dumps(requests))
            if response_gate is not None:
                await asyncio.wait_for(response_gate.wait(), 10)
            # Use the real provider transport and its usage response. The selected
            # session (including a reopened session) owns the threshold decision.
            chunk = {
                "id": "local-owner",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": request["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Retained local context summary."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 9500, "completion_tokens": 5, "total_tokens": 9505},
            }
            body = b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    provider, model = (
        ("custom-local", "custom-model")
        if os.environ.get("PR95_CUSTOM_MODEL")
        else ("local-owner", "selected")
    )
    (tmp_path / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    provider: {
                        "baseUrl": f"http://127.0.0.1:{port}/v1",
                        "api": "openai-completions",
                        "apiKey": "offline-fixture",
                        "models": [
                            {
                                "id": model,
                                "name": "Local selected owner",
                                "contextWindow": 10000,
                                "maxTokens": 1000,
                            }
                        ],
                    }
                }
            }
        )
    )
    agent_dir = tmp_path / "pi-settings"
    agent_dir.mkdir()
    (agent_dir / "settings.json").write_text(json.dumps({
        "compaction": {
            "enabled": os.environ.get("PR95_EFFECTIVE_DISABLED") != "1",
            "reserveTokens": 1000, "keepRecentTokens": 10,
        },
        "retry": {"enabled": False},
    }))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", f"{provider}/{model}")
    if os.environ.get("PR95_EMPTY_SESSION") == "1":
        from agent_comms.fresh_private_session import create_fresh_private_session

        fresh = create_fresh_private_session(
            tmp_path / "native-sessions" / ("f" * 32), worktree=tmp_path
        )
        monkeypatch.setenv("PR95_OWNER_SAVED_SESSION", str(fresh.path))
    child = await AttachedChild.start(
        ("node", str(repo / "stack/test-native-selected-owner-host.mjs")),
        env=dict(os.environ, PR95_OWNER_FIXTURE_ROOT=str(tmp_path), TMPDIR=str(tmp_path)),
    )
    try:
        async with asyncio.timeout(5):
            line = await child.stderr.readline()
        assert line.startswith(b"{"), line.decode()
        fixture = json.loads(line)
        file = fixture["sessionFile"]
        persistent = retained_native_host(
            child,
            NativePiRpcLaunch(("node",), tmp_path, {}, Path(file).parent, Path(file), package),
            NativeSessionIdentity(fixture["sessionId"], file),
        )
        registry = Registration(tmp_path / "registry.json")
        registry.register(
            Thread(
                "owner",
                frozenset(),
                str(tmp_path),
                process_identity=ProcessIdentity.capture(os.getpid()),
                session_file=file,
                model=fixture["model"],
                goal=Goal("work", "goal") if goal else None,
            )
        )
        owner, owner_generation = registry.live_owner_with_generation("owner")
        owner, owner_generation = registry.lease_live_turn_with_generation(
            owner, "turn", expected_owner_generation=owner_generation
        )
        inputs = InputDispositions(tmp_path / InputDispositions.filename)
        inputs.record(
            "acp:original",
            seq=None,
            owner="owner",
            admission=owner.active_turn.admission_generation,
            target="owner",
            text="Continue",
        )
        if os.environ.get("PR95_PRIVATE_SESSION") == "1" and not os.environ.get(
            "PR95_EMPTY_SESSION"
        ):
            record_fixture_history(inputs, "owner", owner.active_turn.admission_generation)
        info = await NativeSessionPreparation.open(
            persistent, launcher,
            ["--provider", provider, "--model", model, "--thinking", "off",
             "--offline", "--no-extensions", "--no-skills", "--no-context-files",
             "--no-prompt-templates", "--no-tools"],
            worktree=str(tmp_path), environment=dict(os.environ), session_file=file,
        )
        async with asyncio.timeout(60):
            yield persistent, registry, inputs, file, launcher, info
    finally:
        server.close()
        await server.wait_closed()
        (tmp_path / "provider-requests.json").write_text(json.dumps(requests))
        await persistent.close_idle()
        assert child.returncode is not None and not child.identity.alive()


async def test_selected_native_summary_commits_and_admits_original_exactly_once(
    tmp_path, monkeypatch
):
    async with owner_fixture(tmp_path, monkeypatch) as (
        persistent,
        registry,
        inputs,
        file,
        launcher,
        info,
    ):
        from agent_comms.field_codec import FieldCodec
        from agent_comms.retained_task_facts import CurrentDecisionTaskFact, UserSourceTaskFact
        from agent_comms.tools import invoke_tool

        comms = Comms(tmp_path)
        user = comms.messaging.send_user_message(
            "owner", "Never replay UNKNOWN; preserve /artifacts/exact-root.",
            worktree=str(tmp_path),
        )
        monkeypatch.setenv("PI_AGENT_ID", "owner")
        declared = invoke_tool(comms, "comms_decision", {
            "chosen": "/artifacts/exact-root",
            "rejected": ["/scratch/guessed-root"],
            "to": "#retention-acceptance",
        })
        choice = comms.bus.log.full_history()[-1]
        assert FieldCodec.encode(choice.reference) == declared["reference"]
        admitted = []
        observed = []

        async def observe(event):
            observed.append(event)

        assert (
            await maybe_compact_owner_turn(
                registry,
                launcher,
                "owner",
                "turn",
                info,
                "acp:original",
                persistent,
                input_text="Continue",
                on_admission=admitted.append,
                on_event=observe,
            )
            is True
        )
        assert len(admitted) == 1
        assert isinstance(observed[0], events.CompactionStart)
        assert isinstance(observed[-1], events.CompactionEnd) and not observed[-1].aborted
        assert any(isinstance(event, events.CompactionSummaryProgress) for event in observed)

        journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
        rows = journal.summaries.blocking(file)
        assert len(rows) == 1 and rows[0].state.declared_name == "linked"
        captured = rows[0].request
        assert tuple(fact.source for fact in captured.retained.facts
                     if isinstance(fact, UserSourceTaskFact)) == (user,)
        assert tuple(fact.source for fact in captured.retained.facts
                     if isinstance(fact, CurrentDecisionTaskFact)) == (choice,)
        operation = journal.operations.get(rows[0].state.commit_id)
        assert operation.state.declared_name == "committed"
        intent = json.loads(operation.intent_json)
        assert intent["selectedSummaryOperationId"] == rows[0].operation_id
        assert (
            intent["selectedSummarySourceDigest"]
            == hashlib.sha256(rows[0].source_json.encode()).hexdigest()
        )
        entries = [json.loads(line) for line in Path(file).read_text().splitlines()]
        assert sum(row["type"] == "compaction" for row in entries) == 1
        committed = next(row for row in entries if row["type"] == "compaction")
        captured.retained.require_summary(committed["summary"])
        assert len(journal.publications.pending(file)) == 1
        assert not persistent.available and persistent.custody.session_file == file
        assert not native_input_admitted(tmp_path, file)
        token = admitted[0]
        assert inputs.read().lookup("acp:original").accepts_reservation
        with _store_lock(tmp_path / "wire"):
            assert token.consume_bound_original(
                wire_root=tmp_path,
                session_file=file,
                identity=token._identity,
                native_id="a" * 32,
                sent_text="Continue",
                dispositions=inputs,
            )
            assert not token.consume_bound_original(
                wire_root=tmp_path,
                session_file=file,
                identity=token._identity,
                native_id="b" * 32,
                sent_text="Continue",
                dispositions=inputs,
            )
        assert inputs.read().rows.get("acp:original").native_id == "a" * 32


@pytest.mark.parametrize("private_session", [False, True], ids=["ordinary", "private"])
@pytest.mark.parametrize(
    "correction,future_queued,queue_revoked",
    [(False, False, False), (True, False, False), (False, True, False), (False, True, True)],
    ids=["unchanged", "correction", "future-queued", "queue-revoked"],
)
@pytest.mark.parametrize("clean_decline", [False, True], ids=["summary", "decline"])
async def test_acp_selected_summary_handoff_uses_final_prompt_once(
    tmp_path, monkeypatch, correction, future_queued, queue_revoked, clean_decline, private_session
):
    await acp_selected_summary_journey(
        tmp_path,
        monkeypatch,
        correction=correction,
        future_queued=future_queued,
        queue_revoked=queue_revoked,
        clean_decline=clean_decline,
        private_session=private_session,
    )


def assert_request_cause(error, expected_type):
    """ACP preserves a typed backend cause while owning its public error."""
    assert isinstance(error.__cause__, expected_type)


async def acp_selected_summary_journey(
    tmp_path,
    monkeypatch,
    *,
    correction=False,
    future_queued=False,
    queue_revoked=False,
    clean_decline=False,
    private_session=False,
    after_summary=None,
    exchange=None,
    response_gate=None,
    expected_error=None,
    expected_state="reserved",
):
    original_key = "acp:original-proj" if private_session else "acp:original"
    from dataclasses import replace

    from acp import RequestError

    from agent_comms.acp import CommsAgent
    from agent_comms.acp_extension import (
        CompactionPublishedUpdate,
        InputDeliveryChangedUpdate,
        QueuePromptRequest,
        decode_updates,
        encode_request,
    )
    from agent_comms.comms import wire
    from agent_comms.compaction_identity import SelectedCommitReference
    from agent_comms.errors import RelationViolationError
    from agent_comms.field_codec import FieldCodec
    from agent_comms.goal_attempts import GoalAttemptStore
    from agent_comms.selected_pi_summary_rpc import SelectedSummarySlot

    if private_session:
        monkeypatch.setenv("PR95_PRIVATE_SESSION", "1")
    if clean_decline:
        monkeypatch.setenv("PR95_DECLINE_SUMMARY", "1")
    async with owner_fixture(tmp_path, monkeypatch, response_gate=response_gate) as (
        persistent,
        _registry,
        _inputs,
        file,
        launcher,
        info,
    ):
        root = tmp_path if private_session else tmp_path / "acp-wire"
        comms = wire(root)
        if private_session:
            # owner_fixture already created this root's certified protocol.
            with comms.bus.log.certified_read() as source:
                root_id = source.witness.root_id
        else:
            root_id = comms.messaging.initialize_private_initial_protocol()
        project = tmp_path / "proj"
        project.mkdir()

        updates = []
        publications = []

        class Client:
            async def session_update(self, **kwargs):
                updates.append(kwargs)
                for event in decode_updates(kwargs["update"].field_meta):
                    if isinstance(event, CompactionPublishedUpdate):
                        # Actual ACP delivery must precede binding/writing this exact
                        # selected original. Read its authority from the journal owner.
                        journal = CompactionJournal(root / "compaction-commits.sqlite3")
                        operation = journal.operations.get(event.publication.commit_id)
                        reference = SelectedCommitReference.from_intent(
                            json.loads(operation.intent_json)
                        )
                        attempt = journal.summaries.get(reference.operation_id)
                        source = attempt.request.source
                        assert source.pending_input_key is not None
                        original = dispositions.read().lookup(source.pending_input_key)
                        assert original.exists and not original.has_native_binding
                        publications.append(event.publication.commit_id)

        agent = CommsAgent(
            comms,
            agent_bin=launcher,
            agent_args=[],
            runtime_enabled=True,
            auto_wake=False,
            private_nk_native_package=Path(PACKAGE).resolve(),
            private_nk_wire_root_id=root_id,
        )
        agent.on_connect(Client())
        await agent.new_session(cwd=str(project), mcp_servers=[])
        agent.inputs.drain_tasks["proj"].cancel()
        await asyncio.gather(agent.inputs.drain_tasks["proj"], return_exceptions=True)
        comms.registry.register(
            replace(
                comms.registry.require("proj"),
                goal=Goal("retain history", "goal-acp"),
                session_file=file,
                model=info.model.display_name,
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
        )
        comms.agents.set_agent_info(
            "proj",
            model=info.model.display_name,
            context_used=info.model.context_window - 500,
            context_size=info.model.context_window,
        )
        private = root / "goal-private"
        private.mkdir(mode=0o700)
        store = GoalAttemptStore.initialize(private)
        store.create_goal("goal-acp")
        agent.turns.goals.goal_store = store
        agent.turns.persistent_backends["proj"] = persistent
        dispositions = InputDispositions(root / InputDispositions.filename)
        if private_session:
            with Coordination(str(root / "coordination.sqlite3")) as coordination:
                install_native_runtime_schema(coordination)
            record_fixture_history(
                dispositions, "proj", comms.registry.snapshot().admission_generations["proj"]
            )
        dispositions.record(
            original_key,
            seq=None,
            owner="proj",
            admission=comms.registry.snapshot().admission_generations["proj"],
            target="proj",
            text="Continue",
        )
        selected_exchange = SelectedSummarySlot.run_selected_summary
        summary_ids = []
        queued_keys = []

        async def selected_with_correction(self, *args, **kwargs):
            if future_queued:
                response = await agent.prompt(
                    "proj",
                    [{"type": "text", "text": "Fresh followup"}],
                    _meta=encode_request(QueuePromptRequest(defer_display=True)),
                )
                (receipt,) = decode_updates(response.field_meta)
                assert isinstance(receipt, InputDeliveryChangedUpdate)
                key = "acp:" + receipt.input_id
                queued_keys.append(key)
                row = dispositions.read().lookup(key)
                assert row.accepts_reservation and not row.has_native_binding
                if queue_revoked:
                    await agent.inputs.clear_queued_inputs("proj")
                    assert dispositions.read().lookup(key) == row
            result = await (
                selected_exchange(self, *args, **kwargs)
                if exchange is None
                else exchange(selected_exchange, self, persistent, *args, **kwargs)
            )
            summary_ids.append(result.operation_id)
            if after_summary is not None:
                await after_summary(persistent, Path(file))
            if correction:
                dispositions.record(
                    "acp:correction",
                    seq=None,
                    owner="proj",
                    admission=comms.registry.snapshot().admission_generations["proj"],
                    target="proj",
                    text="Changed requirement before native commit",
                )
            return result

        monkeypatch.setattr(SelectedSummarySlot, "run_selected_summary", selected_with_correction)
        managed = NativePiRpcLaunch.managed

        def offline_launch(command, arguments, **kwargs):
            launch = managed(command, arguments, **kwargs)
            environment = dict(launch.env, PR95_OWNER_FIXTURE_ROOT=str(tmp_path))
            return replace(
                launch,
                argv=(
                    "node",
                    str(
                        Path(__file__).resolve().parents[1]
                        / "stack/test-native-selected-owner-host.mjs"
                    ),
                    "--session",
                    launch.session_file,
                ),
                env=environment,
            )

        monkeypatch.setattr(NativePiRpcLaunch, "managed", offline_launch)
        try:
            before = Path(file).read_bytes()
            goal_before = comms.registry.require("proj").goal
            grant_before = store.snapshot("goal-acp")
            turn = agent.turns.run_agent_turn(
                "proj",
                "proj",
                "Continue",
                original_keys=(original_key,),
                original_owner_input=True,
                original_goal_id="goal-acp",
            )
            if expected_error is not None:
                with pytest.raises(RequestError) as refused:
                    await turn
                assert_request_cause(refused.value, expected_error)
                original = dispositions.read().lookup(original_key)
                assert isinstance(original, NotSentInput)
                assert not original.has_native_binding and not original.has_started
                assert Path(file).read_bytes() == before
                assert comms.registry.require("proj").goal == goal_before
                assert store.snapshot("goal-acp") == grant_before
                journal = CompactionJournal(root / "compaction-commits.sqlite3")
                (attempt,) = journal.summaries.history(file)
                assert attempt.state.declared_name == expected_state
                assert publications == []
                assert "proj" not in agent.inputs.selected_summary_admissions
                assert not native_input_admitted(root, file)
                requests = json.loads((tmp_path / "provider-requests.json").read_text())
                assert len(requests) == 1  # Actual summary request only.
                return
            if queue_revoked:
                with pytest.raises(RequestError) as refused:
                    await turn
                assert_request_cause(refused.value, RelationViolationError)
                assert summary_ids == []
                assert not CompactionJournal(root / "compaction-commits.sqlite3").summaries.history(
                    file
                )
                assert Path(file).read_bytes() == before
                assert not (tmp_path / "provider-requests.json").exists()
                for key in (original_key, *queued_keys):
                    row = dispositions.read().lookup(key)
                    assert row.exists and not row.has_native_binding and not row.has_started
                assert comms.registry.require("proj").goal == goal_before
                assert store.snapshot("goal-acp") == grant_before
                assert "proj" not in agent.inputs.selected_summary_admissions
                return
            if os.environ.get("PR95_COLD_DECLINE") == "1":
                from agent_comms.owner_compaction_settings import PiSettingsEvidenceError

                with pytest.raises(RequestError) as refused:
                    await turn
                assert_request_cause(refused.value, PiSettingsEvidenceError)
                original = dispositions.read().lookup(original_key)
                assert (
                    original.exists and not original.has_native_binding and not original.has_started
                )
                journal = CompactionJournal(root / "compaction-commits.sqlite3")
                (attempt,) = journal.summaries.history(file)
                assert attempt.state.declared_name == "refused"
                assert "proj" not in agent.inputs.selected_summary_admissions
                assert publications == []
                assert Path(file).read_bytes() == before
                return
            if correction:
                with pytest.raises(RequestError) as refused:
                    await turn
                assert_request_cause(refused.value, RelationViolationError)
                assert isinstance(dispositions.read().lookup(original_key), NotSentInput)
                assert not dispositions.read().lookup(original_key).has_native_binding
                assert Path(file).read_bytes() == before
                assert comms.registry.require("proj").goal == goal_before
                assert store.snapshot("goal-acp") == grant_before
                if not clean_decline:
                    requests = json.loads((tmp_path / "provider-requests.json").read_text())
                    assert len(requests) == 1  # Summary only; no original send/replay.
            else:
                await turn
                assert dispositions.read().rows[original_key].declared_name == "started", updates
            journal = CompactionJournal(root / "compaction-commits.sqlite3")
            attempt = journal.summaries.get(summary_ids[0])
            terminal_status = "declined-prestart" if clean_decline else "linked"
            assert attempt.state.declared_name == ("reserved" if correction else terminal_status)
            assert bool(journal.summaries.blocking(file)) is correction
            assert native_input_admitted(root, file) is not correction
            if not correction and not clean_decline:
                assert publications == [attempt.state.commit_id]
                assert journal.publications.pending(file) == ()
            entries = [json.loads(line) for line in Path(file).read_text().splitlines()]
            assert sum(row["type"] == "compaction" for row in entries) == (
                0 if correction or clean_decline else 1
            )
            assert "proj" not in agent.inputs.selected_summary_admissions
            if not correction:
                assert persistent.custody.idle().current
                assert persistent.available
                native_id = dispositions.read().rows.get(original_key).native_id
                assert native_id
                user_entries = [
                    row
                    for row in entries
                    if row["type"] == "message"
                    and row["message"]["role"] == "user"
                    and row["message"].get("inputId") == native_id
                ]
                assert len(user_entries) == 1
                requests = json.loads((tmp_path / "provider-requests.json").read_text())
                original = dispositions.read().lookup(original_key)
                original_posts = [
                    request
                    for request in requests
                    if any(
                        part.get("text") == original.sent_text
                        for part in request["messages"][-1]["content"]
                    )
                ]
                assert len(original_posts) == 1
                if not clean_decline:
                    summary = next(row["summary"] for row in entries if row["type"] == "compaction")
                    assert summary in json.dumps(original_posts[0]["messages"])
                if future_queued:
                    assert len(queued_keys) == 1
                    followup = dispositions.read().lookup(queued_keys[0])
                    assert followup.has_started, (followup, updates)
                    assert followup.native_id != native_id
                    positions = [
                        next(
                            i
                            for i, entry in enumerate(entries)
                            if entry["type"] == "message"
                            and entry["message"].get("inputId") == receipt.native_id
                        )
                        for receipt in (original, followup)
                    ]
                    assert positions[0] < positions[1]
                    assert (
                        sum(
                            entry["type"] == "message"
                            and entry["message"].get("inputId") == followup.native_id
                            for entry in entries
                        )
                        == 1
                    )
                    assert not agent.inputs.queued_inputs.get("proj")
                    return
                # Repeat the complete selected path on the continued session.
                # Historical linked rows must neither block new work nor permit
                # replay of their originals after this owner's turn finished.
                current = comms.registry.require("proj")
                comms.registry.register(replace(current, goal=Goal("next task", "goal-next")))
                store.create_goal("goal-next")
                dispositions.record(
                    "acp:next",
                    seq=None,
                    owner="proj",
                    admission=comms.registry.snapshot().admission_generations["proj"],
                    target="proj",
                    text="Continue again",
                )
                # The real local HTTP response reports a threshold crossing.
                # Runtime info remains a display projection, not decision authority.
                comms.agents.set_agent_info(
                    "proj",
                    model=info.model.display_name,
                    context_used=info.model.context_window - 500,
                    context_size=info.model.context_window,
                )
                await agent.turns.run_agent_turn(
                    "proj",
                    "proj",
                    "Continue again",
                    original_keys=("acp:next",),
                    original_owner_input=True,
                    original_goal_id="goal-next",
                )
                assert dispositions.read().rows["acp:next"].declared_name == "started"
                assert len(summary_ids) == 2
                assert all(
                    journal.summaries.get(key).state.declared_name == terminal_status
                    for key in summary_ids
                )
                assert journal.summaries.blocking(file) == ()
                assert native_input_admitted(root, file)
                assert persistent.custody.idle().current and persistent.available
                final_entries = [json.loads(line) for line in Path(file).read_text().splitlines()]
                assert sum(row["type"] == "compaction" for row in final_entries) == (
                    0 if clean_decline else 2
                )
                original_ids = {
                    dispositions.read().rows.get(key).native_id
                    for key in (original_key, "acp:next")
                }
                assert len(original_ids) == 2
                assert (
                    sum(
                        row["type"] == "message" and row["message"].get("inputId") in original_ids
                        for row in final_entries
                    )
                    == 2
                )
        finally:
            await agent.shutdown()


async def test_correction_after_native_commit_never_mints_original_admission(tmp_path, monkeypatch):
    from agent_comms.errors import RelationViolationError
    from agent_comms.owner_compaction_commit import OwnerCompactionCommit

    async with owner_fixture(tmp_path, monkeypatch) as (
        persistent,
        registry,
        inputs,
        file,
        launcher,
        info,
    ):
        admit = OwnerCompactionCommit.admit_selected_original

        def corrected(self, owner, owner_generation, operation, source, identity):
            assert operation.state.declared_name == "committed"
            inputs.record(
                "acp:correction",
                seq=None,
                owner=owner.name,
                admission=owner.active_turn.admission_generation,
                target=owner.name,
                text="Correction after native commit",
            )
            return admit(self, owner, owner_generation, operation, source, identity)

        monkeypatch.setattr(OwnerCompactionCommit, "admit_selected_original", corrected)
        admissions = []
        with pytest.raises(RelationViolationError, match="Unsettled"):
            await maybe_compact_owner_turn(
                registry,
                launcher,
                "owner",
                "turn",
                info,
                "acp:original",
                persistent,
                input_text="Continue",
                on_admission=admissions.append,
            )
        assert admissions == []
        assert inputs.read().lookup("acp:original").accepts_reservation
        entries = [json.loads(line) for line in Path(file).read_text().splitlines()]
        assert sum(row["type"] == "compaction" for row in entries) == 1
        assert not native_input_admitted(tmp_path, file)


async def test_selected_effective_disabled_skips_without_reserving_or_mutating(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PR95_EFFECTIVE_DISABLED", "1")
    async with owner_fixture(tmp_path, monkeypatch) as (
        persistent,
        registry,
        inputs,
        file,
        launcher,
        info,
    ):
        # Detached disk settings disagree: selected runtime owns the decision.
        (tmp_path / "pi-settings/settings.json").write_text('{"compaction":{"enabled":true}}')
        before = Path(file).read_bytes()
        assert not await maybe_compact_owner_turn(
            registry,
            launcher,
            "owner",
            "turn",
            info,
            "acp:original",
            persistent,
            input_text="Continue",
            on_admission=lambda _: pytest.fail("Disabled admission"),
        )
        assert Path(file).read_bytes() == before
        assert persistent.available
        assert not (tmp_path / "compaction-commits.sqlite3").exists()
        assert inputs.read().lookup("acp:original").accepts_reservation


async def test_selected_custom_model_and_project_settings_use_actual_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("PR95_CUSTOM_MODEL", "1")
    project = tmp_path / ".pi"
    project.mkdir()
    # These settings are untrusted by this host. A detached projectTrusted:true
    # reader would disagree; the live SettingsManager is the deciding authority.
    (project / "settings.json").write_text('{"compaction":{"enabled":false}}')
    async with owner_fixture(tmp_path, monkeypatch) as (
        persistent,
        registry,
        inputs,
        file,
        launcher,
        info,
    ):
        admitted = []
        assert info.model.display_name == "custom-local/custom-model"
        assert await maybe_compact_owner_turn(
            registry,
            launcher,
            "owner",
            "turn",
            info,
            "acp:original",
            persistent,
            input_text="Continue",
            on_admission=admitted.append,
        )
        assert len(admitted) == 1
        assert inputs.read().lookup("acp:original").accepts_reservation
        assert (
            sum(
                json.loads(line)["type"] == "compaction"
                for line in Path(file).read_text().splitlines()
            )
            == 1
        )


async def test_owner_without_goal_compacts_with_exact_turn_authority(tmp_path, monkeypatch):
    async with owner_fixture(tmp_path, monkeypatch, goal=False) as (
        persistent,
        registry,
        inputs,
        file,
        launcher,
        info,
    ):
        admitted = []
        assert await maybe_compact_owner_turn(
            registry,
            launcher,
            "owner",
            "turn",
            info,
            "acp:original",
            persistent,
            input_text="Continue",
            on_admission=admitted.append,
        )
        assert len(admitted) == 1
        assert inputs.read().lookup("acp:original").accepts_reservation


async def test_acp_selected_settings_change_refuses_original_without_history_write(
    tmp_path, monkeypatch
):
    from agent_comms.errors import RelationViolationError
    from agent_comms.pi_commands import UnknownCommand
    from agent_comms.pi_events import Response
    from agent_comms.pi_rpc import PiRpcChannel

    async def change_settings(persistent, file):
        command = UnknownCommand(
            wire={"type": "set_auto_compaction", "id": "fixture-disable", "enabled": False}
        )
        async with persistent.lock, asyncio.timeout(5):
            child = persistent.custody.child
            child.proc.stdin.write(PiRpcChannel.command_bytes(command))
            await child.proc.stdin.drain()
            response = PiRpcChannel.decode_record(await child.reader.readline(), strict=True)
            assert isinstance(response, Response) and response.success
            assert response.id == "fixture-disable"

    await acp_selected_summary_journey(
        tmp_path,
        monkeypatch,
        after_summary=change_settings,
        expected_error=RelationViolationError,
    )


async def test_acp_selected_saved_source_change_refuses_original_without_history_write(
    tmp_path, monkeypatch
):
    from agent_comms.native_pi import NativePiUnavailable

    async def change_revision(persistent, file):
        stat = file.stat()
        os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))

    await acp_selected_summary_journey(
        tmp_path,
        monkeypatch,
        after_summary=change_revision,
        expected_error=NativePiUnavailable,
    )


async def test_acp_disconnected_selected_summary_stays_unknown_without_original_replay(
    tmp_path, monkeypatch
):
    from agent_comms.selected_pi_summary_rpc import SelectedChildUnknown

    gate = asyncio.Event()

    async def disconnect(exchange, slot, persistent, *args, **kwargs):
        operation = asyncio.create_task(exchange(slot, *args, **kwargs))
        try:
            async with asyncio.timeout(15):
                while not (tmp_path / "provider-requests.json").exists():
                    await asyncio.sleep(0.01)
                await persistent.custody.child.proc.stop()
                return await operation
        finally:
            gate.set()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    await acp_selected_summary_journey(
        tmp_path,
        monkeypatch,
        exchange=disconnect,
        response_gate=gate,
        expected_error=SelectedChildUnknown,
        expected_state="unknown",
    )


@pytest.mark.parametrize(
    "compact_after_reset,damage",
    [(False, None), (True, None), (True, "missing-proof"), (True, "unsettled-input")],
    ids=["attach-input", "attach-compact-input", "missing-proof", "unsettled-input"],
)
async def test_private_retained_session_accepts_after_runtime_journal_reset(
    tmp_path, monkeypatch, compact_after_reset, damage
):
    from dataclasses import replace

    from agent_comms.acp import CommsAgent
    from agent_comms.comms import wire

    monkeypatch.setenv("PR95_PRIVATE_SESSION", "1")
    monkeypatch.setenv("PR95_EMPTY_SESSION", "1")
    async with owner_fixture(tmp_path, monkeypatch, goal=False) as (
        persistent,
        _registry,
        _inputs,
        file,
        launcher,
        info,
    ):
        comms = wire(tmp_path)
        with comms.bus.log.certified_read() as source:
            root_id = source.witness.root_id
        with Coordination(str(tmp_path / "coordination.sqlite3")) as coordination:
            install_native_runtime_schema(coordination)
        project = tmp_path / "proj"
        project.mkdir()
        updates = []

        class Client:
            async def session_update(self, **kwargs):
                updates.append(kwargs)

        def attached_agent():
            agent = CommsAgent(
                comms,
                agent_bin=launcher,
                agent_args=[],
                runtime_enabled=True,
                auto_wake=False,
                private_nk_native_package=Path(PACKAGE).resolve(),
                private_nk_wire_root_id=root_id,
            )
            agent.on_connect(Client())
            return agent

        managed = NativePiRpcLaunch.managed

        def native_launch(command, arguments, **kwargs):
            launch = managed(command, arguments, **kwargs)
            return replace(
                launch,
                argv=(
                    "node",
                    str(
                        Path(__file__).resolve().parents[1]
                        / "stack/test-native-selected-owner-host.mjs"
                    ),
                    "--session",
                    launch.session_file,
                ),
                env=dict(launch.env, PR95_OWNER_FIXTURE_ROOT=str(tmp_path)),
            )

        monkeypatch.setattr(NativePiRpcLaunch, "managed", native_launch)
        agent = attached_agent()
        try:
            await agent.new_session(cwd=str(project), mcp_servers=[])
            agent.inputs.drain_tasks["proj"].cancel()
            await asyncio.gather(agent.inputs.drain_tasks["proj"], return_exceptions=True)
            comms.registry.register(
                replace(
                    comms.registry.require("proj"),
                    session_file=file,
                    model=info.model.display_name,
                    process_identity=ProcessIdentity.capture(os.getpid()),
                )
            )
            agent.turns.persistent_backends["proj"] = persistent
            await agent.prompt("proj", [{"type": "text", "text": "Before quiet cutover"}])
            await agent.prompt("proj", [{"type": "text", "text": "Second retained turn"}])
            before_rows = [
                row for row in agent.inputs.dispositions.read().rows.values() if row.owner == "proj"
            ]
            assert len(before_rows) == 2 and all(row.has_started for row in before_rows), updates
            prior_native_ids = {row.native_id for row in before_rows}
            await agent.shutdown()
            before = Path(file).read_bytes()
            proofs = {
                p: p.read_bytes() for p in Path(file).parent.glob(Path(file).name + ".input-proof*")
            }
            assert proofs
            coordination_inode = (tmp_path / "coordination.sqlite3").stat().st_ino
            # Exact authorized reset, only after the real owner and child stop.
            for name in (
                "compaction-commits.sqlite3",
                "compaction-commits.sqlite3-wal",
                "compaction-commits.sqlite3-shm",
                InputDispositions.filename,
            ):
                (tmp_path / name).unlink(missing_ok=True)
            assert (tmp_path / "coordination.sqlite3").stat().st_ino == coordination_inode
            assert Path(file).read_bytes() == before
            assert all(p.read_bytes() == data for p, data in proofs.items())
            agent = attached_agent()
            await agent.load_session(cwd=str(project), session_id="proj", mcp_servers=[])
            agent.inputs.drain_tasks["proj"].cancel()
            await asyncio.gather(agent.inputs.drain_tasks["proj"], return_exceptions=True)
            (tmp_path / "cutover-attached.json").write_text(
                json.dumps({"attached": True, "session": file})
            )
            assert not agent.turns.persistent_backends
            if damage is not None:
                from acp import RequestError

                if damage == "missing-proof":
                    Path(file + ".input-proof").unlink()
                else:
                    dispositions = agent.inputs.dispositions
                    admission = comms.registry.snapshot().admission_generations["proj"]
                    dispositions.record(
                        "acp:uncertain",
                        seq=None,
                        owner="proj",
                        admission=admission,
                        target="proj",
                        text="Unresolved current input",
                    )
                    dispositions.bind(
                        "acp:uncertain",
                        admission=admission,
                        turn_id="uncertain",
                        native_id="e" * 32,
                        text="Unresolved current input",
                    )
                requests_before = json.loads((tmp_path / "provider-requests.json").read_text())
                with pytest.raises(RequestError):
                    await agent.prompt("proj", [{"type": "text", "text": "/compact"}])
                assert Path(file).read_bytes() == before
                assert (
                    json.loads((tmp_path / "provider-requests.json").read_text()) == requests_before
                )
                if damage == "unsettled-input":
                    row = agent.inputs.dispositions.read().lookup("acp:uncertain")
                    assert row.unresolved and row.has_native_binding and not row.has_started
                return
            if compact_after_reset:
                await agent.prompt("proj", [{"type": "text", "text": "/compact"}])
            await agent.prompt("proj", [{"type": "text", "text": "After quiet cutover"}])
            after_rows = [
                row for row in agent.inputs.dispositions.read().rows.values() if row.owner == "proj"
            ]
            assert len(after_rows) == 1 and after_rows[0].has_started, updates
            assert after_rows[0].native_id not in prior_native_ids
            events = [json.loads(line) for line in Path(file).read_text().splitlines()]
            for native_id in (*prior_native_ids, after_rows[0].native_id):
                assert (
                    sum(
                        row["type"] == "message" and row["message"].get("inputId") == native_id
                        for row in events
                    )
                    == 1
                )
            assert any(row["type"] == "compaction" for row in events)
            assert Path(file).read_bytes().startswith(before)
            assert not CompactionJournal(
                tmp_path / "compaction-commits.sqlite3"
            ).summaries.blocking(file)
        finally:
            await agent.shutdown()


@pytest.mark.parametrize("private_session", [False, True], ids=["ordinary", "private"])
async def test_cold_context_decline_refuses_before_original_binding(
    tmp_path, monkeypatch, private_session
):
    monkeypatch.setenv("PR95_COLD_DECLINE", "1")
    await acp_selected_summary_journey(
        tmp_path,
        monkeypatch,
        correction=False,
        future_queued=False,
        queue_revoked=False,
        clean_decline=True,
        private_session=private_session,
    )
    assert json.loads((tmp_path / "provider-requests.json").read_text()) == []
