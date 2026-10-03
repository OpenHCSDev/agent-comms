"""Real cold Pi CLI, localhost provider failure, and unchanged saved reopen."""

from __future__ import annotations
from agent_comms.owner_launch import RestartEnvironment
from agent_comms.selected_session import SavedSelectedSession

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

from agent_comms.retained_task_facts import RetainedTaskFacts
from agent_comms.field_codec import FieldCodec
from agent_comms.child_process import AttachedChild
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.native_custody import PiSessionChild
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.owner_compaction_prepare import prepare_native_source
from agent_comms.owner_compaction_settings import PiCompactionSettings
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.pi_summary_payloads import SelectedModel
from agent_comms.selected_pi_summary_rpc import (
    SelectedChildUnknown,
    SelectedSummaryFailed,
    SelectedSummarySlot,
)
from retained_native_fixture import retained_native_host
from selected_summary_cases import manual_source


@asynccontextmanager
async def native_summary_owner(tmp_path, status):
    pin = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
    if not pin:
        pytest.skip("Set PI_COMPACTION_TEST_PACKAGE to the matched native candidate")
    package = Path(pin).resolve(strict=True)
    requests = []
    started, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            started.set()
            if status is None:
                release.wait(15)
                self.close_connection = True
                return
            if status == 200:
                packets = [
                    {"id": "original-local-summary", "choices": [{"index": 0,
                     "delta": {"role": "assistant", "content": "Original generated narrative."},
                     "finish_reason": None}]},
                    {"id": "original-local-summary", "choices": [{"index": 0,
                     "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 100, "completion_tokens": 8, "total_tokens": 108}},
                ]
                body = ("".join("data: " + json.dumps(packet) + "\n\n" for packet in packets)
                        + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps(
                {"error": {"message": "Local selected summary refused", "type": "fixture"}}
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = tmp_path / "config"
    config.mkdir()
    settings = PiCompactionSettings(2048, 1024)
    selected = SelectedModel("fixture", "fixture", 32768)
    (config / "settings.json").write_text(
        json.dumps(
            {"compaction": {"enabled": True, "reserveTokens": 2048, "keepRecentTokens": 1024}}
        )
    )
    (config / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "fixture": {
                        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                        "api": "openai-completions",
                        "apiKey": "local-test",
                        "models": [
                            {
                                "id": "fixture",
                                "name": "Local failure test",
                                "contextWindow": 32768,
                                "maxTokens": 2048,
                            }
                        ],
                    }
                }
            }
        )
    )
    session = tmp_path / "saved.jsonl"
    session_id, timestamp = str(uuid4()), "2026-09-28T00:00:00.000Z"
    rows = [
        dict(type="session", version=3, id=session_id, timestamp=timestamp, cwd=str(tmp_path)),
        dict(
            type="model_change",
            id="model",
            parentId=None,
            timestamp=timestamp,
            provider="fixture",
            modelId="fixture",
        ),
        dict(
            type="thinking_level_change",
            id="thinking",
            parentId="model",
            timestamp=timestamp,
            thinkingLevel="off",
        ),
    ]
    parent = "thinking"
    for index in range(10):
        for entry_id, message in (
            (
                f"u{index}",
                dict(
                    role="user",
                    content=f"SAVED_HISTORY_{index} " + "retained text " * 200,
                    timestamp=index,
                ),
            ),
            (
                f"a{index}",
                dict(
                    role="assistant",
                    content=[dict(type="text", text="ack")],
                    api="openai-completions",
                    provider="fixture",
                    model="fixture",
                    stopReason="stop",
                    timestamp=index,
                    usage=dict(
                        input=100,
                        output=1,
                        cacheRead=0,
                        cacheWrite=0,
                        totalTokens=101,
                        cost=dict(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
                    ),
                ),
            ),
        ):
            rows.append(
                dict(
                    type="message",
                    id=entry_id,
                    parentId=parent,
                    timestamp=timestamp,
                    message=message,
                )
            )
            parent = entry_id
    session.write_text("".join(json.dumps(row) + "\n" for row in rows))
    session.chmod(0o600)
    original = session.read_bytes()
    preparation = (await asyncio.to_thread(
        prepare_native_source,
        package,
        str(session),
        settings=settings,
        context_window=selected.context_window,
    )).require_ready()

    env = {
        "PATH": "/usr/local/bin:/usr/bin",
        "HOME": str(tmp_path),
        "PI_CODING_AGENT_DIR": str(config),
        "AGENT_COMMS_NATIVE_CONFIG_DIR": str(config),
        "PI_OFFLINE": "1",
        "NODE_DISABLE_COMPILE_CACHE": "1",
        "NO_COLOR": "1",
    }
    arguments = (
        "--mode",
        "rpc",
        "--provider",
        "fixture",
        "--model",
        "fixture",
        "--offline",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-tools",
        "--session",
        str(session),
    )
    children = {}

    async def launch(*, observe_launch=None):
        environment = dict(env)
        if observe_launch is not None:
            observe_launch(environment)
        command, environment = NativePiRpcLaunch.bootstrap(
            package / "dist/cli.js", arguments, tmp_path, environment,
            RestartEnvironment.inherit(environment))
        child = await AttachedChild.start(command, cwd=str(tmp_path), env=environment)
        errors = asyncio.create_task(PiSessionChild.stderr_tail(child.stderr))
        children[child] = errors
        reader = PiRpcChannel(child.stdout)

        async def exchange(command):
            child.stdin.write((json.dumps(command) + "\n").encode())
            await child.stdin.drain()
            async with asyncio.timeout(20):
                while True:
                    raw = await reader.readline()
                    assert raw, await errors
                    event = json.loads(raw)
                    assert event.get("type") not in (
                        "input_committed",
                        "context_committed",
                        "agent_start",
                        "message_start",
                    )
                    if event.get("type") == "response" and event.get("id") == command["id"]:
                        return raw, event

        _, state = await exchange(dict(type="get_state", id="state"))
        assert state["success"] and state["data"]["sessionId"] == session_id
        assert not state["data"]["isStreaming"] and not state["data"]["isCompacting"]
        return child, reader, exchange, errors

    try:
        yield package, session, original, preparation, selected, settings, requests, started, launch
    finally:
        release.set()
        for child, errors in children.items():
            if child.returncode is None:
                await child.stop()
            await child.wait()
            await errors
        server.shutdown()
        server.server_close()


def selected_owner(child, reader, package, session, preparation, errors):
    persistent = retained_native_host(
        child,
        NativePiRpcLaunch(("node",), package, {}, SavedSelectedSession(session.parent, identity=NativeSessionIdentity(preparation.witness.session_id, str(session))), package, configuration=RestartEnvironment.inherit({})),
        NativeSessionIdentity(preparation.witness.session_id, str(session)),
        reader=reader,
        stderr_task=errors,
    )
    return (
        persistent,
        CompactionJournal(session.parent / "compaction-commits.sqlite3"),
        SelectedSummarySlot("owner", preparation.witness.session_id),
    )


async def test_original_summary_assembly_inspector(tmp_path):
    """Actual selected RPC generation with a local provider, never a live input.

    This qualifies the observation source. No canonical commit, configured
    provider study, submitted bounded control or recall credit is claimed.
    """
    from dataclasses import replace
    from agent_comms.pi_commands import AgentCommsSummarizeCompaction
    from agent_comms.selected_pi_summary_rpc import _summary_response
    from agent_comms.turn_context import FileProvenance
    from retained_native_fixture import RecordedNativeCheckpoint, RecordedSummaryAssembly
    from summary_prefix_configured_installed_journey import observe_native_requests
    import hashlib

    pin = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]).resolve(strict=True)
    summaries = tmp_path / "summary-assemblies"
    summaries.mkdir(mode=0o700)
    observation = tmp_path / "original-observations.jsonl"
    async with native_summary_owner(tmp_path, 200) as fixture:
        with observe_native_requests(pin, observation, summaries=summaries) as observe_launch:
            package, session, original, preparation, selected, settings, calls, _, launch = fixture
            child, _, exchange, errors = await launch(observe_launch=observe_launch)
            command = AgentCommsSummarizeCompaction(
                id="original-source-capture", version=1, operation_id=uuid4().hex,
                witness=preparation.witness, selected=selected, settings=settings,
                retained_text="Original exact injected task envelope.")
            raw, reply = await exchange(command.to_rpc())
            assert reply["success"], reply
            result = _summary_response(raw, command, preparation.tokens_before)
            path = summaries / f"summary-{command.operation_id}.json"
            capture = RecordedNativeCheckpoint.read_record(
                FileProvenance(str(path), hashlib.sha256(path.read_bytes()).hexdigest()),
                RecordedSummaryAssembly)
            assert capture.request == command
            assert capture.generated_parts and capture.inherited_summary is None
            assert capture.summary.startswith("Original generated narrative.")
            assert result.result.summary.startswith(command.retained_text + "\n\n")
            assert not capture.summary.startswith(command.retained_text)
            assert capture.observe()["evaluated"]
            assert not replace(capture, inherited_summary=result.result.summary).observe()["evaluated"]
            assert not replace(capture, generated_parts=()).observe()["evaluated"]
            assert session.read_bytes() == original
            records = [json.loads(row) for row in observation.read_text().splitlines()]
            assert not any(row.get("observerFailed") for row in records), records
            assert len([row for row in records if row.get("stage") == "summary-assembly"]) == 1
            assert calls and child.returncode is None
            child.stdin.close()
        # EOF exit waits for debugger disconnect. Release the original inspector
        # resource before joining the native child, then retain its actual stderr.
        await child.wait()
        (tmp_path / "native-stderr.log").write_text(await errors)


async def test_summary_observer_releases_before_native_eof(tmp_path):
    """The same observation resource closes before native EOF; no generation."""
    from summary_prefix_configured_installed_journey import observe_native_requests

    pin = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]).resolve(strict=True)
    summaries = tmp_path / "summary-assemblies"
    summaries.mkdir(mode=0o700)
    async with native_summary_owner(tmp_path, 200) as fixture:
        package, session, original, _, _, _, calls, _, launch = fixture
        with observe_native_requests(pin, tmp_path / "observations.jsonl",
                                     summaries=summaries) as observe_launch:
            child, _, _, errors = await launch(observe_launch=observe_launch)
            child.stdin.close()
        await child.wait()
        (tmp_path / "native-stderr.log").write_text(await errors)
        assert not calls
        assert session.read_bytes() == original


@pytest.mark.parametrize("status", [400, 429])
async def test_actual_native_provider_failure_attests_source_and_reopens(tmp_path, status):
    async with native_summary_owner(tmp_path, status) as fixture:
        package, session, original, preparation, selected, settings, calls, _, launch = fixture
        child, reader, exchange, errors = await launch()
        persistent, journal, slot = selected_owner(
            child, reader, package, session, preparation, errors
        )
        with pytest.raises(
            SelectedSummaryFailed, match="Local selected summary refused"
        ) as failure:
            await slot.run_selected_summary(
                persistent,
                journal,
                preparation.witness,
                dict(
                    source=manual_source(session),
                    selected=selected.to_wire(),
                    settings=dict(reserveTokens=2048, keepRecentTokens=1024),
                    retained=FieldCodec.encode(RetainedTaskFacts(())),
                ),
                expected_package=package,
                tokens_before=preparation.tokens_before,
                idle_timeout_seconds=20,
            )
        state = journal.summaries.get(failure.value.operation_id).state
        assert state.declared_name == "failed"
        assert state.terminal and state.settled_without_original
        assert not state.original_eligible
        assert not journal.summaries.blocking(str(session))
        assert persistent.custody.idle().current and child.returncode is None
        assert len(calls) == 2, "two map chunks, no retries"
        assert len({json.dumps(call["messages"]) for call in calls}) == 2
        assert "SAVED_HISTORY_0" in json.dumps(calls[0])
        assert session.read_bytes() == original
        _, state = await exchange(dict(type="get_state", id="idle-after-failure"))
        assert not state["data"]["isStreaming"] and not state["data"]["isCompacting"]
        child.stdin.close()
        async with asyncio.timeout(10):
            await child.wait()
        _, _, reopened, _ = await launch()
        _, history = await reopened(dict(type="get_messages", id="reopened-history"))
        assert len(history["data"]["messages"]) == 20
        assert session.read_bytes() == original
        assert len(calls) == 2, "reopen must not replay input or summary"


async def test_actual_native_child_disconnect_remains_unknown(tmp_path):
    async with native_summary_owner(tmp_path, None) as fixture:
        package, session, original, preparation, selected, settings, calls, started, launch = (
            fixture
        )
        child, reader, _, errors = await launch()
        persistent, journal, slot = selected_owner(
            child, reader, package, session, preparation, errors
        )
        task = asyncio.create_task(
            slot.run_selected_summary(
                persistent,
                journal,
                preparation.witness,
                dict(
                    source=manual_source(session),
                    selected=selected.to_wire(),
                    settings=dict(reserveTokens=2048, keepRecentTokens=1024),
                    retained=FieldCodec.encode(RetainedTaskFacts(())),
                ),
                expected_package=package,
                tokens_before=preparation.tokens_before,
                idle_timeout_seconds=20,
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 10)
            await child.stop()
            with pytest.raises(SelectedChildUnknown):
                await task
            assert (
                journal.summaries.unresolved(str(session))[0].state.declared_name
                == "unknown"
            )
            assert not native_input_admitted(tmp_path, str(session))
            assert session.read_bytes() == original
            total_calls = len(calls)
            await launch()
            assert len(calls) == total_calls
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await persistent.close_idle()


async def test_actual_summary_slot_denies_mutation_and_joins_cancellation(tmp_path):
    from agent_comms.pi_commands import AgentCommsSummarizeCompaction

    async with native_summary_owner(tmp_path, None) as fixture:
        package, session, original, preparation, selected, settings, calls, started, launch = fixture
        child, _, exchange, _ = await launch()
        operation = "d" * 32
        request = AgentCommsSummarizeCompaction(
            id="held-summary", version=1, operation_id=operation,
            witness=preparation.witness, selected=selected, settings=settings,
            retained_text=RetainedTaskFacts(()).text,
        )
        child.stdin.write((json.dumps(request.to_rpc()) + "\n").encode())
        await child.stdin.drain()
        assert await asyncio.to_thread(started.wait, 10)
        _, state = await exchange(dict(type="get_state", id="during-summary"))
        assert state["data"]["isCompacting"]
        for command in (
            dict(type="prompt", message="Must never start", inputId="c" * 32),
            dict(type="set_model", provider=selected.provider, modelId=selected.model_id),
            dict(type="compact"),
            dict(type="new_session"),
        ):
            _, response = await exchange(dict(id=command["type"], **command))
            assert not response["success"]
            assert response["error"] == "Selected summary in flight; mutation denied"
        _, second = await exchange(dict(request.to_rpc(), id="second", operationId="e" * 32))
        assert second["data"]["status"] == "declined"
        assert second["data"]["reason"] == "in_flight"
        _, cancelled = await exchange(dict(
            id="cancel", type="agent_comms_cancel_summary", version=1, operationId=operation,
        ))
        assert cancelled["success"] and cancelled["data"]["status"] == "unknown"
        _, idle = await exchange(dict(type="get_state", id="after-cancellation"))
        assert not idle["data"]["isCompacting"] and not idle["data"]["isStreaming"]
        _, duplicate = await exchange(dict(request.to_rpc(), id="duplicate"))
        assert duplicate["data"]["reason"] == "duplicate_operation"
        assert session.read_bytes() == original
        # This direct native request does not create a Python owner journal.
        # Native input proof, not the journal admission default, proves no start.
        assert not Path(str(session) + ".input-proof").exists()
        total_calls = len(calls)
        await child.stop()
        await launch()
        assert len(calls) == total_calls
        assert session.read_bytes() == original
