"""Physical native parent -> normal fork -> first ACP send, without replay."""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from acp.schema import TextContentBlock

from agent_comms.acp import CommsClient
from agent_comms.acp_extension import (
    CompactionChangedUpdate,
    CompactionCommittedUpdate,
    CompactionPublishedUpdate,
    decode_updates,
)
from agent_comms.comms import Comms
from agent_comms.input_disposition import InputDispositions
from agent_comms.thread_management import ForkSpec
from agent_comms.threads import Thread
from agent_comms.child_process import DetachedProcess
from agent_comms.pi_commands import GetState, GetSessionStats
from agent_comms.pi_rpc import PiRpcChannel
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_underbudget_physical_native_fork_answers_first_input_without_compaction(
    native_backend, monkeypatch
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
    parent_text = "PHYSICAL_PARENT_CONTEXT " + "Retained architecture observation. " * 900
    result = await native.run(parent_text)
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
    assert stats.context_usage.tokens is not None
    assert 0.20 < stats.context_usage.tokens / state.model.context_window < 0.27
    print("PHYSICAL_PARENT_STATS", repr(stats), flush=True)
    assert state.model.context_window == 32768
    print("PHYSICAL_PARENT_STATE", repr(state), flush=True)
    original = native.session.read_bytes()
    await native.persistent.close()
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    monkeypatch.setenv(
        "AGENT_COMMS_AGENT_ARGS", "--offline --no-extensions --no-skills --no-context-files"
    )
    monkeypatch.setenv(
        "PATH", str(Path(os.sys.executable).parent) + os.pathsep + os.environ["PATH"]
    )
    comms = Comms(native.root)
    comms.threads.register(
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
    launch = DetachedProcess.launch
    worker_log = native.project / "worker.log"
    output = worker_log.open("wb")

    def logged_launch(*args, **kwargs):
        return launch(*args, output=output, **kwargs)

    monkeypatch.setattr(DetachedProcess, "launch", logged_launch)
    try:
        child = await asyncio.to_thread(
            comms.threads.fork,
            ForkSpec(
                name="physical-child",
                parent="physical-parent",
                task="Continue retained architecture",
            ),
        )
        async with asyncio.timeout(15):
            while True:
                try:
                    await attachment.load_session(cwd=str(native.project), session_id=child.name)
                    break
                except Exception:
                    if not comms.registry.require(child.name).process_alive:
                        raise
                    await asyncio.sleep(0.1)
        first_send_started = time.monotonic()
        try:
            async with asyncio.timeout(20):
                await attachment.prompt(
                    child.name, [TextContentBlock(type="text", text="hey Boss")]
                )
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
        print("FIRST_SEND_LATENCY_SECONDS", latency, flush=True)
        assert latency < 20
        assert not any(isinstance(fact, CompactionChangedUpdate) for fact in facts)
        assert not any(isinstance(fact, CompactionCommittedUpdate) for fact in facts)
        assert not any(isinstance(fact, CompactionPublishedUpdate) for fact in facts)
        thread = comms.registry.require(child.name)
        assert thread.session_file and thread.session_file != str(native.session)
        entries = [json.loads(line) for line in Path(thread.session_file).read_text().splitlines()]
        assert sum(entry["type"] == "compaction" for entry in entries) == 0
        users = [
            entry["message"]
            for entry in entries
            if entry["type"] == "message" and entry["message"].get("role") == "user"
        ]
        assert sum("hey Boss" in json.dumps(message["content"]) for message in users) == 1
        assert native.provider.posts == 2  # physical parent, first answer only
        assert native.session.read_bytes() == original
        await asyncio.sleep(1.2)
        assert native.provider.posts == 2  # No uncertain original replay or unsolicited retry.
    finally:
        await attachment.shutdown()
        output.flush()
        print("ACTUAL_WORKER_LOG", worker_log.read_text(), flush=True)
        output.close()
        if child is not None:
            await asyncio.to_thread(comms.owners.stop, child.name)
            assert not comms.registry.require(child.name).process_alive
