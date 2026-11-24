"""In-place Native Pi <-> CLI Inbox execution switch and model catalog invalidation."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_comms.cli_verbs.fork import ForkCliCommand
from agent_comms.config_options import (
    CatalogChoice,
    ConfigOptions,
    DiscoveredModels,
    ModelConfigOption,
)
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import NoProcess, ProcessIdentity
from agent_comms.execution.cli_inbox_backend import CliInboxBackend, CliInboxRuntime
from agent_comms.execution.pi_durable_backend import PiDurableBackend
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.execution.turn_item import NativeItemId, NativeTurnId
from agent_comms.pi_counts import ReportedCount
from agent_comms.pi_native_backend import PiNativeBackend
from agent_comms.pi_payloads import ModelsData, ReportedModel, ReportedText
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import PeerIdentity
from agent_comms.registry.thread_model import CliInboxThreadModel
from agent_comms.registry.thread_status import NoBridgeThreadStatus, StoppedThreadStatus
from agent_comms.registry.thread_text import ForkedParent
from agent_comms.registry.threads import (
    ExternalThread,
    ForkThread,
    NativeThread,
    ServiceThread,
)
from agent_comms.transcript.transcript_events import AssistantTranscript
from agent_comms.transport.connector_caller import DirectorCaller
from agent_comms.transport.owner_requests import RetainedOwnerLaunch
from agent_comms.transport.tools.served import (
    CommsCliInboxTool,
    CommsForkTool,
    CommsNativeThreadTool,
    invoke_tool,
)
from agent_comms_hooks import ToolRefusedError
from conftest import installed_comms


def test_native_to_cli_inbox_takeover_and_round_trip_preserves_identity(tmp_path: Path) -> None:
    comms = installed_comms(tmp_path / "wire")
    session_path = comms.root / "native-sessions" / "worker.jsonl"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(
        json.dumps({"type": "session", "id": "worker-1", "timestamp": "2026-11-23T08:00:00Z"})
        + "\n"
    )
    comms.registry.declare(
        NativeThread(
            "worker",
            frozenset({"model", "research"}),
            Checkout(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=RegistrySessionLocation.from_path(session_path, comms.root),
        ),
    )
    comms.registry.rename("worker", "researcher")
    comms.registry.unregister("researcher")
    original = comms.registry.require("researcher")
    assert original.process_identity == NoProcess()
    assert comms.registry.canonical_name("worker") == "researcher"

    # Context action availability before switch: Native -> cli-inbox available, native-thread not.
    status_before = comms.registry.status("researcher")
    assert CommsCliInboxTool.available_for_thread(original, status_before)
    assert not CommsNativeThreadTool.available_for_thread(original, status_before)

    # DirectorCaller refuses direct takeover while the thread still runs on Native Pi.
    director = DirectorCaller("claude-ai")
    acting, _ = director.acting({"fork": {"name": "researcher"}})
    with pytest.raises(ToolRefusedError, match="comms_cli_inbox"):
        director.prepare_call(comms, acting)

    # Switch Native Pi -> CLI inbox via MCP tool.
    switched_reply = invoke_tool(comms, "comms_cli_inbox", {"name": "researcher"}, caller=director)
    assert switched_reply == {
        "thread": "researcher",
        "execution": "external",
        "session_preserved": True,
    }
    after_cli = comms.registry.require("researcher")
    assert isinstance(after_cli, ExternalThread)
    assert after_cli.backend is CliInboxBackend
    assert isinstance(after_cli.runtime, CliInboxRuntime)
    assert after_cli.incarnation == original.incarnation
    assert after_cli.tags == original.tags
    assert after_cli.session_file == original.session_file
    assert comms.registry.canonical_name("worker") == "researcher"
    assert isinstance(comms.registry.status("researcher"), NoBridgeThreadStatus)

    # Context action availability after switch flips cleanly.
    status_cli = comms.registry.status("researcher")
    assert not CommsCliInboxTool.available_for_thread(after_cli, status_cli)
    assert CommsNativeThreadTool.available_for_thread(after_cli, status_cli)

    # Antigravity / remote MCP session takes over the thread with fork={"name": "researcher"}.
    director.prepare_call(comms, acting)
    assert comms.registry.require("researcher").incarnation == original.incarnation
    assert not comms.registry.require("researcher").is_fork
    assert acting.authority(comms).incarnation == original.incarnation
    assert acting.identity(comms) == original.incarnation.require_recorded().as_colleague()
    assert isinstance(acting.identity(comms), PeerIdentity)

    # External transcript items can be published even though the prior Pi session_file is retained.
    comms.transcripts.external("researcher").finish(
        turn_id=NativeTurnId("t1"),
        item_id=NativeItemId("i1"),
        events=(
            AssistantTranscript(
                "External takeover turn", position=NativeItemId("i1"), timestamp=100.0
            ),
        ),
    )
    page = comms.transcripts.capture_page_read("researcher").read()
    assert [event.text for event in page.events] == ["External takeover turn"]

    # Sending as the taken-over thread publishes from its exact name/identity.
    comms.registry.declare(ExternalThread("peer", frozenset({"model"}), Checkout(tmp_path)))
    sent = invoke_tool(
        comms,
        "comms_send",
        {"to": "peer", "body": "Taking over seamlessly"},
        caller=acting,
    )
    history = invoke_tool(comms, "comms_history", {"peer": "peer"}, caller=acting)
    assert history["dm"][0]["from"] == "researcher"
    assert history["dm"][0]["text"] == "Taking over seamlessly"

    # Switch back CLI inbox -> Native Pi and verify saved Pi session_file is preserved intact.
    back_reply = invoke_tool(comms, "comms_native_thread", {"name": "researcher"}, caller=director)
    assert back_reply == {
        "thread": "researcher",
        "execution": "native",
        "session_preserved": True,
    }
    restored = comms.registry.require("researcher")
    assert isinstance(restored, NativeThread)
    assert restored.backend is PiNativeBackend
    assert restored.incarnation == original.incarnation
    assert restored.tags == original.tags
    assert restored.session_file == original.session_file
    assert restored.runtime.read_key == str(session_path.resolve())
    assert isinstance(comms.registry.status("researcher"), StoppedThreadStatus)


def test_subagent_refusal_and_delete_cleans_preserved_session(tmp_path: Path) -> None:
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(ExternalThread("parent", frozenset(), Checkout(tmp_path)))
    comms.registry.register_child("child", "parent", ForkThread, comms.goals)
    child = comms.registry.require("child")
    assert not CommsNativeThreadTool.available_for_thread(child, comms.registry.status("child"))
    with pytest.raises(RelationViolationError, match="subagent"):
        comms.threads.switch_to_native("child")

    # Deleting a CLI-inbox thread with a preserved Pi session frees that session file.
    session_path = comms.root / "native-sessions" / "temp.jsonl"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text('{"type":"session","id":"temp"}\n')
    comms.registry.declare(
        NativeThread(
            "temp",
            frozenset(),
            Checkout(tmp_path),
            session_file=RegistrySessionLocation.from_path(session_path, comms.root),
        ),
        StoppedThreadStatus(),
    )
    comms.threads.switch_to_cli_inbox("temp")
    assert session_path.exists()
    deleted = comms.threads.delete("temp")
    assert deleted.deleted == "temp"
    assert deleted.session_bytes_freed > 0
    assert not session_path.exists()


def test_models_json_invalidates_catalog_and_surfaces_labels(tmp_path: Path, monkeypatch) -> None:
    comms = installed_comms(tmp_path / "wire")
    native_dir = tmp_path / "pi-agent"
    native_dir.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(native_dir))
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(native_dir))
    monkeypatch.delenv("AGENT_COMMS_AGENT_MODELS", raising=False)

    calls = 0

    async def fake_discover(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return ModelsData(
            models=(
                ReportedModel(
                    provider=ReportedText("personal-model"),
                    id=ReportedText("pm-2b-sft-v2"),
                    name=ReportedText("Personal Model 2B (Widened SFT v2)"),
                    context_window=ReportedCount(32768),
                ),
                ReportedModel(
                    provider=ReportedText("openai-codex"),
                    id=ReportedText("gpt-5.6-sol"),
                    name=ReportedText("gpt-5.6-sol"),
                ),
            )
        )

    from agent_comms.execution.pi_commands import GetAvailableModels

    monkeypatch.setattr(GetAvailableModels, "discover", fake_discover)
    options = ConfigOptions(RetainedOwnerLaunch.configured(), comms.route)
    assert isinstance(options.models, DiscoveredModels)
    catalog = options.catalog_for(ModelConfigOption)
    thread = comms.registry.declare(NativeThread("worker", frozenset(), Checkout(tmp_path)))

    first = asyncio.run(catalog.choices(thread))
    assert calls == 1
    assert first == [
        CatalogChoice(
            name="Personal Model 2B (Widened SFT v2) (personal-model/pm-2b-sft-v2)",
            value="personal-model/pm-2b-sft-v2",
            description="32,768 context",
        ),
        CatalogChoice(
            name="openai-codex/gpt-5.6-sol",
            value="openai-codex/gpt-5.6-sol",
            description=None,
        ),
        CatalogChoice(
            name="CLI inbox",
            value="cli-inbox",
            description="External CLI or MCP session on this thread's inbox",
        ),
    ]
    # Cached on repeat call when neither auth.json nor models.json changed.
    assert asyncio.run(catalog.choices(thread)) == first
    assert calls == 1

    # Editing models.json invalidates the catalog cache immediately.
    models_file = options.launch.configuration.models_file
    models_file.write_text('{"providers": {"personal-model": {}}}\n')
    assert asyncio.run(catalog.choices(thread)) == first
    assert calls == 2


def test_unified_fork_uses_backend_capability_and_forks_cli_inbox_threads(tmp_path: Path) -> None:
    comms = installed_comms(tmp_path / "wire")
    project = tmp_path / "repo"
    project.mkdir()
    subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
    parent = ExternalThread.stored(comms.registry.declare(
        ExternalThread("lead-cli", frozenset({"comms", "team"}), Checkout(project)),
        NoBridgeThreadStatus(),
    ))
    native = comms.registry.declare(
        NativeThread("lead-pi", frozenset({"comms"}), Checkout(project)),
        StoppedThreadStatus(),
    )
    durable = comms.registry.declare(
        NativeThread("lead-durable", frozenset(), Checkout(project), backend=PiDurableBackend),
        StoppedThreadStatus(),
    )
    service = comms.registry.declare(ServiceThread("installer", frozenset(), Checkout(project)))
    subagent = comms.registry.register_child("lead-sub", "lead-cli", ForkThread, comms.goals)

    # Fork availability follows AgentBackend capability (Forks) + independent thread, never NativeThread.
    for command in (ForkCliCommand, CommsForkTool):
        assert command.available_for_thread(parent, comms.registry.status("lead-cli"))
        assert command.available_for_thread(native, comms.registry.status("lead-pi"))
        assert not command.available_for_thread(durable, comms.registry.status("lead-durable"))
        assert not command.available_for_thread(service, comms.registry.status("installer"))
        assert not command.available_for_thread(subagent, comms.registry.status("lead-sub"))

    # ForkCliCommand pre-binds a CLI-inbox parent with CliInboxThreadModel.
    bound = ForkCliCommand.thread_binding(parent)
    assert bound.model == CliInboxThreadModel()

    # Parent publishes an external turn before forking.
    comms.transcripts.external("lead-cli").finish(
        turn_id=NativeTurnId("p1"),
        item_id=NativeItemId("pi1"),
        events=(
            AssistantTranscript("Parent CLI context", position=NativeItemId("pi1"), timestamp=10.0),
        ),
    )

    # Forking a CLI-inbox parent via comms_fork creates a child CLI-inbox thread with ForkedParent.
    director = DirectorCaller("claude-ai")
    reply = invoke_tool(
        comms,
        "comms_fork",
        {"name": "child-cli", "parent": "lead-cli", "task": "Follow up"},
        caller=director,
    )
    assert reply["created"] == "child-cli"
    child = comms.registry.require("child-cli")
    assert isinstance(child, ExternalThread)
    assert child.backend is CliInboxBackend
    assert child.runtime == CliInboxRuntime()
    assert child.parent == ForkedParent("lead-cli")
    assert child.tags == frozenset({"comms", "team"})
    assert isinstance(comms.registry.status("child-cli"), NoBridgeThreadStatus)

    # Child opens with its fork header and task on its own external transcript.
    page = comms.transcripts.capture_page_read("child-cli").read()
    assert [event.text for event in page.events] == [
        "Forked from @lead-cli. This thread started with:",
        "Follow up",
    ]

    # Subagents still refuse forking.
    with pytest.raises(RelationViolationError, match="subagent"):
        invoke_tool(
            comms,
            "comms_fork",
            {"name": "grandchild", "parent": "lead-sub"},
            caller=director,
        )
