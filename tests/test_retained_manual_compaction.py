"""Opt-in actual retained-session ACP/manual/commit/reopen; loopback transport only."""

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from acp.agent.router import build_agent_router

from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import (
    CompactionCommittedUpdate,
    CompactRequest,
    decode_updates,
    encode_request,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_states import ManualCommittedSummary
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_session_reopen import validate_native_reopen
from agent_comms.owner_compaction_prepare import prepare_native_source
from agent_comms.owner_compaction_settings import PiCompactionSettings
from agent_comms.selected_pi_route import read_selected_compaction_decision
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
                                "contextWindow": 272000,
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
                    "reserveTokens": 16384,
                    "keepRecentTokens": 20000,
                }
            }
        )
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(config))
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path / "wire"))
    agent = None
    try:
        preparation = await asyncio.to_thread(
            prepare_native_source,
            package,
            str(session),
            settings=PiCompactionSettings(16384, 20000),
            context_window=272000,
        )
        assert preparation is not None
        comms = Comms(tmp_path / "wire")
        root_id = comms.messaging.initialize_private_initial_protocol()
        monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID", root_id)
        monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE", str(package))
        thread = Thread(
            "retained",
            frozenset(),
            str(project),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(session),
            model=model,
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
        (attempt,) = journal.selected_summaries(str(session))
        if mode == "manual":
            assert isinstance(attempt.state, ManualCommittedSummary)
        assert attempt.state.commit_id
        assert journal.get(attempt.state.commit_id).state.committed
        persistent = agent.turns.persistent_backends["retained"]
        if mode == "manual":
            assert not persistent.available and persistent.custody.session_file == str(session)
            await agent.turns.prepare_selected_session(
                "retained", comms.registry.require("retained")
            )
        else:
            assert persistent.available and persistent.custody.child.proc.returncode is None
        decision = await read_selected_compaction_decision(
            persistent,
            session_file=str(session),
            expected_package=package,
            provider="retained-local",
            model_id="fixture",
            context_window=272000,
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
        receipt = {
            "source_bytes": before_source.st_size,
            "tokens_before": preparation.tokens_before,
            "provider_calls": provider.posts,
            "transport": "loopback only",
            "native_commit": True,
            "strict_reopen": identity == preparation.witness.session_id,
            "read_files": len(latest.get("details", {}).get("readFiles", [])),
            "modified_files": len(latest.get("details", {}).get("modifiedFiles", [])),
            "owner_idle": comms.registry.require("retained").active_turn is None,
            "new_user_inputs": len(
                InputDispositions(comms.root / InputDispositions.filename).read().rows
            ),
        }
        assert receipt["new_user_inputs"] == (0 if mode == "manual" else 1)
        assert receipt["owner_idle"] and receipt["strict_reopen"]
        assert user_entries_after == user_entries_before + (0 if mode == "manual" else 1)
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
