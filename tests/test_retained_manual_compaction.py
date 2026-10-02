"""Opt-in actual retained-session ACP/manual/commit/reopen; loopback transport only."""

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from acp.agent.router import build_agent_router

from agent_comms.pi_summary_payloads import SelectedModel
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import (
    CompactionCommittedUpdate,
    CompactRequest,
    decode_updates,
    encode_request,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import Goal
from agent_comms.goal_states import PausedGoal
from agent_comms.native_compaction_request import NativeIntent
from agent_comms.retained_task_facts import RetainedTaskFacts
from agent_comms.comms import Comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_states import ManualCommittedSummary
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_session_reopen import validate_native_reopen
from agent_comms.owner_compaction_prepare import prepare_native_source
from agent_comms.owner_compaction_settings import PiCompactionSettings
from agent_comms.selected_pi_route import observe_selected_compaction_decision
from agent_comms.threads import Thread
from compaction_loopback import LoopbackProvider

pytestmark = pytest.mark.skipif(
    not os.environ.get("RETAINED_COMPACTION_SOURCE"), reason="Owned retained source opt-in"
)


@pytest.mark.parametrize("mode", ["manual", "adaptive"])
async def test_actual_cold_retained_commit_and_reopen(tmp_path, monkeypatch, mode):
    source = Path(os.environ["RETAINED_COMPACTION_SOURCE"])
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]).resolve()
    launcher = os.environ["AC_NATIVE_STACK_BIN"]
    context_window = int(os.environ.get("RETAINED_COMPACTION_CONTEXT_WINDOW", "272000"))
    settings = PiCompactionSettings(
        16384, int(os.environ.get("RETAINED_COMPACTION_KEEP_RECENT_TOKENS", "20000"))
    )
    before_source = source.stat()
    session = tmp_path / "retained.jsonl"
    shutil.copyfile(source, session)
    session.chmod(0o600)
    with session.open() as stream:
        user_entries_before = sum(
            row.get("type") == "message" and row.get("message", {}).get("role") == "user"
            for row in map(json.loads, stream)
        )
    config = tmp_path / "config"
    config.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    provider = LoopbackProvider(status=200, text="Condensed context. " * 800)
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    provider.port = server.sockets[0].getsockname()[1]
    model = "retained-local/fixture"
    (config / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "retained-local": {
                        "baseUrl": f"http://127.0.0.1:{provider.port}/v1",
                        "api": "openai-completions",
                        "models": [
                            {
                                "id": "fixture",
                                "name": "local fixture",
                                "contextWindow": context_window,
                                "maxTokens": 8192,
                            }
                        ],
                    }
                }
            }
        )
    )
    (config / "auth.json").write_text(
        json.dumps(
            {
                "retained-local": {
                    "type": "api_key",
                    "key": "local-only",
                }
            }
        )
    )
    (config / "settings.json").write_text(
        json.dumps(
            {
                "compaction": {
                    "enabled": True,
                    "reserveTokens": settings.reserve_tokens,
                    "keepRecentTokens": settings.keep_recent_tokens,
                }
            }
        )
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(config))
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path / "wire"))
    agent = None
    try:
        preparation = (await asyncio.to_thread(
            prepare_native_source,
            package,
            str(session),
            settings=settings,
            context_window=context_window,
        )).require_ready()

        comms = Comms(tmp_path / "wire")
        root_id = comms.messaging.initialize_private_initial_protocol()
        monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID", root_id)
        monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE", str(package))
        retained_text = os.environ.get("RETAINED_COMPACTION_EXACT_FACT", "")
        goal = Goal(retained_text, "original-retained-coordinate", revision=1,
                    state=PausedGoal()) if retained_text else None
        thread = Thread(
            "retained",
            frozenset(),
            str(project),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(session),
            model=model,
            goal=goal,
        )
        comms.registry.register(thread)
        agent = CommsAgent(
            comms,
            agent_bin=launcher,
            agent_args=[
                "--offline",
                "--no-extensions",
                "--no-skills",
                "--no-context-files",
                "--no-tools",
            ],
            auto_wake=False,
            private_nk_native_package=package,
            private_nk_wire_root_id=root_id,
        )
        updates = []

        class Client:
            async def session_update(self, **kwargs):
                updates.append(kwargs)

        agent.on_connect(Client())
        await agent.sessions.bind_owned(comms.registry.require("retained"), "retained")
        assert not agent.turns.persistent_backends
        assert comms.agents.agent_info_of("retained") is None
        response = await build_agent_router(agent)(
            "session/prompt",
            {
                "sessionId": "retained",
                "prompt": [
                    {"type": "text", "text": " " if mode == "manual" else "cold-start input"}
                ],
                **({"_meta": encode_request(CompactRequest())} if mode == "manual" else {}),
            },
            False,
        )
        (tmp_path / "updates.json").write_text(
            json.dumps([item["update"].model_dump(mode="json") for item in updates], indent=2)
        )
        if mode == "manual":
            (receipt,) = decode_updates(response.field_meta)
            assert isinstance(receipt, CompactionCommittedUpdate)
            assert receipt.summary
        journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
        (attempt,) = journal.summaries.history(str(session))
        if mode == "manual":
            assert isinstance(attempt.state, ManualCommittedSummary)
        assert attempt.state.commit_id
        operation = journal.operations.get(attempt.state.commit_id)
        assert operation.state.committed
        envelope = attempt.request
        if retained_text:
            assert envelope.retained.text.count(retained_text) == 1
            assert len(attempt.source_json.encode()) > RetainedTaskFacts.journal_control_bytes
            assert len(operation.intent_json.encode()) > RetainedTaskFacts.journal_control_bytes
        # A fresh reader must accept the same original complete payload and links.
        recovered = CompactionJournal(journal.path)
        assert recovered.summaries.get(attempt.operation_id).request == envelope
        assert NativeIntent.read(recovered.operations.get(operation.commit_id)).witness == preparation.witness
        recovered.operations.get(operation.commit_id).committed_outcome()
        operation.require_summary_link(attempt, admit_original=True)

        persistent = agent.turns.persistent_backends["retained"]
        if mode == "manual":
            assert not persistent.available and persistent.custody.identity.session_file == str(session)
            await agent.turns.prepare_selected_session(
                "retained", comms.registry.require("retained")
            )
        else:
            assert persistent.available and persistent.custody.child.proc.returncode is None
        decision = await observe_selected_compaction_decision(
            persistent,
            session_file=str(session),
            expected_package=package,
            selected=SelectedModel("retained-local", "fixture", context_window),
        )
        assert not decision.trigger, "Committed context must be usable on a fresh native reopen"
        identity = await asyncio.to_thread(
            validate_native_reopen,
            package,
            str(session),
            expected_session_id=preparation.witness.session_id,
        )
        latest = None
        user_entries_after = 0
        with session.open() as stream:
            for line in stream:
                row = json.loads(line)
                user_entries_after += (
                    row.get("type") == "message" and row.get("message", {}).get("role") == "user"
                )
                if row.get("type") == "compaction":
                    latest = row
        assert latest is not None
        envelope.retained.require_summary(latest["summary"])
        fresh_response = await build_agent_router(agent)(
            "session/prompt",
            {"sessionId": "retained", "prompt": [
                {"type": "text", "text": f"FRESH_AFTER_{mode.upper()}_COMPACTION"}
            ]}, False,
        )
        assert fresh_response.stop_reason == "end_turn"
        with session.open() as stream:
            user_entries_after = sum(
                row.get("type") == "message" and row.get("message", {}).get("role") == "user"
                for row in map(json.loads, stream)
            )
        receipt = {
            "source_bytes": before_source.st_size,
            "tokens_before": preparation.tokens_before,
            "selected_context_window": context_window,
            "provider_calls": provider.posts,
            "transport": "loopback only",
            "native_commit": True,
            "retained_payload_bytes": len(RetainedTaskFacts.canonical_journal_bytes(
                FieldCodec.encode(envelope.retained))),
            "reservation_bytes": len(attempt.source_json.encode()),
            "intent_bytes": len(operation.intent_json.encode()),
            "complete_retained_summary": True,
            "fresh_queued_input_answered": fresh_response.stop_reason == "end_turn",
            "strict_reopen": identity == preparation.witness.session_id,
            "read_files": len(latest.get("details", {}).get("readFiles", [])),
            "modified_files": len(latest.get("details", {}).get("modifiedFiles", [])),
            "owner_idle": comms.registry.require("retained").active_turn is None,
            "new_user_inputs": len(
                InputDispositions(comms.root / InputDispositions.filename).read().rows
            ),
        }
        assert receipt["new_user_inputs"] == (1 if mode == "manual" else 2)
        assert receipt["owner_idle"] and receipt["strict_reopen"]
        assert user_entries_after == user_entries_before + (1 if mode == "manual" else 2)
        assert (source.stat().st_size, source.stat().st_mtime_ns) == (
            before_source.st_size,
            before_source.st_mtime_ns,
        )
        (tmp_path / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))
    finally:
        if agent is not None:
            await agent.shutdown()
        server.close()
        await server.wait_closed()
