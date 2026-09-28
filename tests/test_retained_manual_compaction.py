"""Opt-in actual retained-session ACP/manual/commit/reopen; loopback transport only."""

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from acp.agent.router import build_agent_router

from agent_comms.acp import CommsAgent
from agent_comms.backend import PersistentPiSession, _session_revision
from agent_comms.comms import Comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_states import ManualCommittedSummary
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_session_reopen import validate_native_reopen
from agent_comms.owner_compaction_prepare import prepare_native_source
from agent_comms.pi_commands import GetState
from agent_comms.pi_events import Response
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.threads import Thread
from test_manual_compaction import LoopbackProvider

pytestmark = pytest.mark.skipif(
    not os.environ.get("RETAINED_COMPACTION_SOURCE"), reason="Owned retained source opt-in"
)


async def test_actual_retained_manual_commit_and_reopen(tmp_path, monkeypatch):
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
    provider = LoopbackProvider(status=200)
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
    guard = tmp_path / "local-only.cjs"
    guard.write_text(
        "const fetch=globalThis.fetch;globalThis.fetch=(url,...args)=>{"
        "const target=url instanceof Request?url.url:String(url);"
        f"if(!target.startsWith('http://127.0.0.1:{provider.port}/'))"
        "throw Error('NONLOCAL_NETWORK_REFUSED');return fetch(url,...args)};"
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(config))
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path / "wire"))
    env = dict(os.environ, NODE_OPTIONS=f"--require={guard}", PI_OFFLINE="1")
    stderr = (tmp_path / "native-stderr.log").open("wb")
    proc = await asyncio.create_subprocess_exec(
        "node",
        "--max-old-space-size=1536",
        str(package / "dist/cli.js"),
        "--mode",
        "rpc",
        "--offline",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-tools",
        "--provider",
        "retained-local",
        "--model",
        "fixture",
        "--session",
        str(session),
        env=env,
        cwd=project,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
    )
    persistent = PersistentPiSession()
    persistent.proc = proc
    persistent.reader = PiRpcChannel(proc.stdout)
    agent = None
    try:
        request = GetState(id="retained-state")
        proc.stdin.write(PiRpcChannel.command_bytes(request))
        await proc.stdin.drain()
        while True:
            async with asyncio.timeout(30):
                event = await persistent.reader.receive(strict=True)
            if isinstance(event, Response) and event.id == request.id:
                assert event.success
                break
        preparation = await asyncio.to_thread(
            prepare_native_source, package, str(session), keep_recent_tokens=20000
        )
        assert preparation is not None
        persistent.session_file = str(session)
        persistent.session_id = preparation.witness.session_id
        persistent.revision = _session_revision(str(session))
        persistent.launch_key = (launcher,)
        comms = Comms(tmp_path / "wire")
        root_id = comms.messaging.initialize_private_initial_protocol()
        thread = Thread(
            "retained",
            frozenset(),
            str(project),
            pid=os.getpid(),
            session_file=str(session),
            model=model,
        )
        comms.registry.register(thread)
        agent = CommsAgent(
            comms,
            agent_bin=launcher,
            auto_wake=False,
            private_nk_native_package=package,
            private_nk_wire_root_id=root_id,
        )
        updates = []

        class Client:
            async def session_update(self, **kwargs):
                updates.append(kwargs)

        agent.on_connect(Client())
        await agent.sessions.bind_owned(
            comms.registry.require("retained"), "retained", fresh=False, private=True
        )
        comms.agents.set_agent_info(
            "retained", model=model, context_used=preparation.tokens_before, context_size=272000
        )
        agent.turns.persistent_backends["retained"] = persistent
        await build_agent_router(agent)(
            "session/prompt",
            {
                "sessionId": "retained",
                "prompt": [{"type": "text", "text": "/compact"}],
            },
            False,
        )
        journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
        (attempt,) = journal.selected_summaries(str(session))
        assert isinstance(attempt.state, ManualCommittedSummary)
        assert journal.get(attempt.state.commit_id).state.committed
        assert persistent.proc is None and persistent.reopen_required == str(session)
        identity = await asyncio.to_thread(
            validate_native_reopen,
            launcher,
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
        assert receipt["new_user_inputs"] == 0
        assert receipt["owner_idle"] and receipt["strict_reopen"]
        assert user_entries_after == user_entries_before
        assert (source.stat().st_size, source.stat().st_mtime_ns) == (
            before_source.st_size,
            before_source.st_mtime_ns,
        )
        (tmp_path / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))
    finally:
        if agent is not None:
            await agent.shutdown()
        await persistent.close_idle()
        stderr.close()
        server.close()
        await server.wait_closed()
