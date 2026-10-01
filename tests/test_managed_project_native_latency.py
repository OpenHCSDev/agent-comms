"""Installed owner IPC/native over original retained history; provider only is controlled."""

import asyncio
import functools
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from dataclasses import replace

from acp.schema import TextContentBlock
import pytest

from agent_comms.acp import CommsClient
from agent_comms.acp_extension import InputFailedUpdate, RequestFailedUpdate, decode_updates
from agent_comms.comms import Comms
from agent_comms.input_drain import InputDrain
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.owned_turn import OwnedTurn
from agent_comms.runtime_requests import ProjectRuntimeRequest
from agent_comms.turn_runner import TurnRunner
import agent_comms.native_package as native_package
from delivery_owner_fixture import canonical_agent

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_installed_retained_greeting_then_managed_read(native_backend, monkeypatch):
    fixture = native_backend
    source_name = os.environ.get("NATIVE_TOOL_LATENCY_SOURCE")
    if not source_name:
        pytest.skip("An original retained source is required")
    source = Path(source_name)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    assert source.stat().st_size > 40_000_000
    # The installed candidate must not be an editable/source overlay.
    assert "site-packages" in native_package.__file__
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    extension = package / "agent-comms-extensions/global-project-sync/index.mjs"
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    monkeypatch.setenv("PATH", str(Path(os.sys.executable).parent) + os.pathsep + os.environ["PATH"])
    spans, updates, failures = [], [], []

    def timed_async(target, name):
        original = getattr(target, name)

        @functools.wraps(original)
        async def timed(*args, **kwargs):
            begin = time.perf_counter_ns()
            try:
                return await original(*args, **kwargs)
            finally:
                spans.append({"boundary": target.__name__ + "." + name,
                              "begin_ns": begin, "end_ns": time.perf_counter_ns()})
        monkeypatch.setattr(target, name, timed)

    for target, name in ((InputDrain, "drain_owned_inbox"),
                         (OwnedTurn, "prepare_native"),
                         (TurnRunner, "prepare_selected_session"),
                         (ProjectRuntimeRequest, "result")):
        timed_async(target, name)
    verify = native_package.verify_native_package

    def timed_verify(*args, **kwargs):
        begin = time.perf_counter_ns()
        try:
            return verify(*args, **kwargs)
        finally:
            spans.append({"boundary": "verify_native_package", "begin_ns": begin,
                          "end_ns": time.perf_counter_ns()})
    monkeypatch.setattr(native_package, "verify_native_package", timed_verify)
    managed = NativePiRpcLaunch.managed

    def timed_managed(*args, **kwargs):
        begin = time.perf_counter_ns()
        try:
            return managed(*args, **kwargs)
        finally:
            spans.append({"boundary": "NativePiRpcLaunch.managed", "begin_ns": begin,
                          "end_ns": time.perf_counter_ns()})
    monkeypatch.setattr(NativePiRpcLaunch, "managed", timed_managed)

    class Observer:
        async def session_update(self, session_id, update):
            now = time.perf_counter_ns()
            facts = decode_updates(update.get("_meta"))
            failures.extend(fact for fact in facts if isinstance(fact, (InputFailedUpdate, RequestFailedUpdate)))
            content = update.get("content", {})
            text = content.get("text", "") if isinstance(content, dict) else ""
            # Keep only original live event classifications and clock brackets;
            # saved snapshot text and user history are not copied into evidence.
            updates.append({"received_ns": now, "update": update.get("sessionUpdate"),
                            "facts": [type(fact).__name__ for fact in facts],
                            "marker": text if "LATENCY_" in text else ""})

    comms = Comms(fixture.root)
    owner = canonical_agent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False,
        agent_args=["--offline", "--no-extensions", "--extension", str(extension),
                    "--no-skills", "--no-context-files", "--no-prompt-templates", "--tools", "read"])
    attachment = CommsClient(comms, agent_bin="pi", auto_wake=False,
        private_nk_native_package=package,
        private_nk_wire_root_id=os.environ["AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"])
    attachment.on_connect(Observer())
    report = {"complete": False, "spans": spans, "updates": updates,
              "python": os.sys.executable, "core_module": native_package.__file__,
              "native_package": str(package), "source_bytes": source.stat().st_size,
              "source_sha256": source_hash, "public_inputs": 0, "paid_calls": 0,
              "clock": "Same Python process perf_counter_ns; nested spans are not additive"}
    receipt = Path(os.environ["NATIVE_TOOL_LATENCY_RECEIPT"])
    try:
        session = (await owner.new_session(str(fixture.project))).session_id
        thread = comms.registry.require(session)
        saved_dir = fixture.root / "native-sessions" / "retained"
        saved_dir.mkdir(parents=True, mode=0o700)
        saved = saved_dir / source.name
        shutil.copyfile(source, saved); saved.chmod(0o600)
        with sqlite3.connect(Path(str(source) + ".input-proof").as_uri() + "?mode=ro", uri=True) as original:
            with sqlite3.connect(str(saved) + ".input-proof") as target:
                original.backup(target)
        Path(str(saved) + ".input-proof").chmod(0o600)
        comms.registry.register(replace(thread, session_file=str(saved),
                                       model="response-local/fixture", thinking_level="off"))
        await attachment.load_session(str(fixture.project), session)
        fixture.provider.text = "LATENCY_GREETING_OK"
        begin = time.perf_counter_ns()
        async with asyncio.timeout(90):
            answer = await attachment.prompt(session, [TextContentBlock(type="text", text="A new isolated greeting; answer once.")])
        report["greeting"] = {"begin_ns": begin, "end_ns": time.perf_counter_ns(),
                              "stop_reason": answer.stop_reason}
        assert fixture.provider.posts == 1
        assert any(update["marker"] == fixture.provider.text for update in updates)
        assert not comms.registry.require(session).executing
        (fixture.project / "isolated-read.txt").write_text("LATENCY_READ_FILE_OK\n")
        fixture.provider.tool_call = ("read", {"path": "isolated-read.txt"})
        fixture.provider.text = "LATENCY_READ_OK"
        begin = time.perf_counter_ns()
        async with asyncio.timeout(90):
            answer = await attachment.prompt(session, [TextContentBlock(type="text", text="Read isolated-read.txt, then answer once.")])
        report["read"] = {"begin_ns": begin, "end_ns": time.perf_counter_ns(),
                          "stop_reason": answer.stop_reason}
        assert fixture.provider.posts == 3, "One greeting, one read request and one final; no replay"
        assert any(span["boundary"] == "ProjectRuntimeRequest.result" for span in spans), "Actual native extension must query its original owner"
        assert any(update["marker"] == fixture.provider.text for update in updates)
        assert not failures, failures
        assert not comms.registry.require(session).executing
        report["complete"] = True
    finally:
        children = [persistent.custody.idle().child.proc
                    for persistent in owner.turns.persistent_backends.values()
                    if persistent.available]
        await attachment.shutdown()
        await owner.shutdown()
        report["provider_posts"] = fixture.provider.posts
        report["source_unchanged"] = hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
        report["native_children_retired"] = all(not child.alive() for child in children)
        report["native_child_count"] = len(children)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(report, indent=2) + "\n")
    assert report["source_unchanged"]
    assert report["native_children_retired"] and report["native_child_count"] == 1
