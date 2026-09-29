"""Opt-in current installed fork check using an actual retained source, localhost only."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from agent_comms.acp import CommsClient
from agent_comms.acp_extension import InputFailedUpdate, RequestFailedUpdate, decode_updates
from agent_comms.comms import Comms
from agent_comms.input_attempt import StartedInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.thread_management import ForkSpec
from agent_comms.threads import Thread

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_current_installed_retained_fork_first_input(native_backend, monkeypatch):
    source = os.environ.get("AC_RETAINED_FORK_SOURCE")
    if not source:
        pytest.skip("Requires explicit retained source; read only")
    source = Path(source)
    before = source.stat()
    fixture = native_backend
    model_file = fixture.config / "models.json"
    model = json.loads(model_file.read_text())
    model["providers"]["response-local"]["models"][0]["contextWindow"] = 272000
    model_file.write_text(json.dumps(model))
    (fixture.config / "settings.json").write_text(
        json.dumps(
            {
                "compaction": {"enabled": True, "reserveTokens": 16384, "keepRecentTokens": 20000},
                "retry": {"enabled": False, "maxRetries": 0},
            }
        )
    )
    marker = "RETAINED_FORK_LOOPBACK_" + uuid4().hex[:12]
    fixture.provider.text = marker
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    monkeypatch.setenv(
        "AGENT_COMMS_AGENT_ARGS",
        "--offline --no-extensions --no-skills --no-context-files --no-prompt-templates --no-tools",
    )
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    launcher = str(Path(sys.executable).with_name("pi-comms-native"))
    comms = Comms(fixture.root)
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    root_id = os.environ["AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"]
    comms.owners.pin_private_nk_launch(fixture.root, root_id, package)
    comms.registry.declare(
        Thread(
            "retained-parent",
            frozenset(),
            str(fixture.project),
            session_file=str(source),
            model="response-local/fixture",
            thinking_level="off",
        )
    )
    attachment = CommsClient(
        comms,
        agent_bin=launcher,
        runtime_enabled=True,
        private_nk_native_package=package,
        private_nk_wire_root_id=root_id,
    )
    failures = []
    answered = asyncio.Event()

    class View:
        async def session_update(self, session_id, update):
            failures.extend(
                fact
                for fact in decode_updates(update.get("_meta"))
                if isinstance(fact, (InputFailedUpdate, RequestFailedUpdate))
            )
            content = update.get("content", {})
            if isinstance(content, dict) and marker in content.get("text", ""):
                answered.set()

    attachment.on_connect(View())
    child = None
    started = time.monotonic()
    try:
        child = await asyncio.to_thread(
            comms.threads.fork,
            ForkSpec(
                "retained-child", "retained-parent", "One new isolated fork diagnostic " + marker
            ),
            launcher,
        )
        assert Path(child.session_file).is_relative_to(fixture.config)
        # The real fork's startup input races the first real owner attachment.
        # No attachment retry and no second prompt are issued by this harness.
        async with asyncio.timeout(90):
            await attachment.load_session(str(fixture.project), child.name)
            while not answered.is_set():
                assert not failures, failures
                await asyncio.sleep(0.05)
            while not comms.registry.require(child.name).last_finished_turn_id:
                await asyncio.sleep(0.05)
        rows = InputDispositions(fixture.root / InputDispositions.filename).read().rows
        original = [row for row in rows.values() if row.owner == child.name]
        assert len(original) == 1 and isinstance(original[0], StartedInput), original
        assert marker in original[0].source_text
        assert not failures
        print(
            "RETAINED_FORK_RESULT",
            json.dumps(
                {
                    "source_bytes": before.st_size,
                    "first_input_started": 1,
                    "reply_received": True,
                    "local_http_posts": fixture.provider.posts,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "provider": "loopback-controlled",
                    "context_window": 272000,
                    "original_replayed": False,
                    "python": sys.executable,
                }
            ),
            flush=True,
        )
    finally:
        await attachment.shutdown()
        if child is not None:
            await asyncio.to_thread(comms.owners.stop, child.name)
            assert not comms.registry.require(child.name).process_alive
        for log in (fixture.root / "diagnostics").glob("owner-*.log"):
            print("RETAINED_FORK_WORKER", log.read_text(), flush=True)
        after = source.stat()
        assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
