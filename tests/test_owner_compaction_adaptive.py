"""Opt-in owner adaptive handoff; fake summary, real native CAS, no provider."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import agent_events as ae
from agent_comms import backend
from agent_comms.acp import CommsAgent
from agent_comms.backend import PersistentPiSession
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import Goal
from agent_comms.input_disposition import InputDispositions
from agent_comms.owner_compaction_adaptive import maybe_compact_owner_turn
from agent_comms.owner_compaction_provider import (
    NativeSummary,
)
from agent_comms.owner_compaction_settings import PiCompactionDecision, PiSettingsEvidenceError
from agent_comms.pi_summary_payloads import SummaryFiles, SummaryUsage
from agent_comms.registration import Registration
from agent_comms.runtime_info import AgentRuntimeInfo
from agent_comms.threads import Thread

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
pytestmark = pytest.mark.skipif(
    not PACKAGE or sys.platform != "linux", reason="Disposable Linux native opt-in"
)


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    script = """
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
const {SessionManager} = await import(pathToFileURL(join(process.argv[1],
  'dist/core/session-manager.js')));
const manager = SessionManager.create(process.argv[2], join(process.argv[2], 'sessions'));
for (let i=0;i<3;i++) {
  manager.appendMessage({role:'user',content:'Task '+i+' history '.repeat(500),timestamp:i*2+1});
  manager.appendMessage({role:'assistant',content:[{type:'text',text:'Answer '+i}],
    provider:'fixture',model:'fixture',api:'fixture',stopReason:'stop',timestamp:i*2+2});
}
console.log(manager.getSessionFile());
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, PACKAGE, str(tmp_path)],
        capture_output=True,
        check=True,
        timeout=20,
        text=True,
    )
    session = Path(result.stdout.strip())
    agent_dir = tmp_path / "private-pi-agent"
    agent_dir.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    registry = Registration(tmp_path / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
            session_file=str(session),
            goal=Goal("work", "goal"),
            model="openrouter/fixture",
        )
    )
    owner, owner_generation = registry.live_owner_with_generation("owner")
    owner, owner_generation = registry.lease_live_turn_with_generation(
        owner, "turn", expected_owner_generation=owner_generation
    )
    assert owner.active_turn is not None
    InputDispositions(tmp_path / InputDispositions.filename).record(
        "acp:original",
        seq=None,
        owner="owner",
        admission=owner.active_turn.admission_generation,
        target="owner",
        text="new input, not yet attempted",
    )
    monkeypatch.setattr(
        "agent_comms.native_pi.NativePiRpcLaunch.package_for_command",
        lambda _: Path(PACKAGE),
    )
    monkeypatch.setattr(
        "agent_comms.owner_compaction_adaptive.read_compaction_decision",
        lambda *_args, **_kwargs: PiCompactionDecision(100, 100, enabled=True, trigger=True),
    )
    info = AgentRuntimeInfo(
        thread="owner",
        model="openrouter/fixture",
        context_used=950,
        context_size=1000,
    )
    return registry, session, info


async def test_selected_default_strategy_refuses_detached_provider_before_commit(admitted):
    registry, session, info = admitted
    before = session.read_bytes()
    with pytest.raises(PiSettingsEvidenceError, match="selected live Pi|Selected live Pi"):
        await maybe_compact_owner_turn(
            registry,
            "pi-native",
            "owner",
            "turn",
            info,
            "acp:original",
            PersistentPiSession(),
        )
    assert session.read_bytes() == before
    assert (
        InputDispositions(session.parent.parent / InputDispositions.filename)
        .read()
        .rows.get("acp:original")
        .native_id
        is None
    )


async def test_adaptive_owner_one_original_input_native_commit_without_provider(admitted):
    registry, session, info = admitted
    seen = []

    async def summarize(prepared):
        seen.append(prepared)
        assert prepared.witness.session_id
        assert prepared.witness.session_file == str(session)
        return NativeSummary("Provider-free synthetic owner summary", None, None)

    result = await maybe_compact_owner_turn(
        registry,
        "unused-launcher",
        "owner",
        "turn",
        info,
        "acp:original",
        PersistentPiSession(),
        summary_strategy=summarize,
    )
    assert result is True
    assert len(seen) == 1
    assert (
        InputDispositions(session.parent.parent / InputDispositions.filename)
        .read()
        .rows.get("acp:original")
        .native_id
        is None
    )
    entries = [json.loads(line) for line in session.read_text().splitlines()]
    assert sum(row["type"] == "compaction" for row in entries) == 1


async def test_adaptive_owner_correction_during_summary_refuses_native_commit(admitted):
    registry, session, info = admitted
    before = session.read_bytes()

    async def summarize(_prepared):
        owner = registry.require("owner")
        assert owner.active_turn is not None
        InputDispositions(session.parent.parent / InputDispositions.filename).record(
            "acp:correction",
            seq=None,
            owner="owner",
            admission=owner.active_turn.admission_generation,
            target="owner",
            text="Retain correction",
        )
        return NativeSummary("Stale synthetic summary", None, None)

    with pytest.raises(RelationViolationError, match="Unsettled"):
        await maybe_compact_owner_turn(
            registry,
            "unused-launcher",
            "owner",
            "turn",
            info,
            "acp:original",
            PersistentPiSession(),
            summary_strategy=summarize,
        )
    assert session.read_bytes() == before


async def test_adaptive_settings_change_after_summary_refuses_native_commit(admitted, tmp_path):
    registry, session, info = admitted
    before = session.read_bytes()

    async def summarize(_prepared):
        (tmp_path / "private-pi-agent" / "settings.json").write_text(
            '{"compaction":{"keepRecentTokens":500}}'
        )
        return NativeSummary("Stale synthetic summary", None, None)

    with pytest.raises(RelationViolationError, match="source changed"):
        await maybe_compact_owner_turn(
            registry,
            "unused-launcher",
            "owner",
            "turn",
            info,
            "acp:original",
            PersistentPiSession(),
            summary_strategy=summarize,
        )
    assert session.read_bytes() == before


async def test_adaptive_unproven_project_settings_skips_without_provider(admitted, tmp_path):
    registry, session, info = admitted
    project_settings = tmp_path / ".pi" / "settings.json"
    project_settings.parent.mkdir()
    project_settings.write_text('{"compaction":{"enabled":true}}')
    called = False

    async def summarize(_prepared):
        nonlocal called
        called = True
        return NativeSummary("forbidden", None, None)

    assert not await maybe_compact_owner_turn(
        registry,
        "unused-launcher",
        "owner",
        "turn",
        info,
        "acp:original",
        PersistentPiSession(),
        summary_strategy=summarize,
    )
    assert not called
    assert (
        InputDispositions(session.parent.parent / InputDispositions.filename)
        .read()
        .rows["acp:original"]
        .declared_name
        == "unknown"
    )


async def test_adaptive_unbound_custom_model_skips_without_provider(admitted, tmp_path):
    registry, _session, info = admitted
    (tmp_path / "private-pi-agent" / "models.json").write_text('{"providers":[]}')

    async def forbidden(_prepared):
        raise AssertionError("Unbound model must not receive a provider request")

    assert not await maybe_compact_owner_turn(
        registry,
        "unused-launcher",
        "owner",
        "turn",
        info,
        "acp:original",
        PersistentPiSession(),
        summary_strategy=forbidden,
    )


@pytest.mark.parametrize("correction", [False, True])
async def test_acp_owner_turn_compacts_then_sends_original_once(
    admitted, tmp_path, monkeypatch, correction
):
    _registry, session, info = admitted
    root = tmp_path / "wire"
    comms = wire(root)
    project = tmp_path / "proj"
    project.mkdir()
    received = []

    class Client:
        async def session_update(self, **kwargs):
            received.append(kwargs)

    async def synthetic_summary(prepared):
        assert prepared.witness.session_file == str(session)
        if correction:
            owner = comms.registry.require("proj")
            assert owner.active_turn is not None
            InputDispositions(root / InputDispositions.filename).record(
                "acp:correction",
                seq=None,
                owner="proj",
                admission=owner.active_turn.admission_generation,
                target="proj",
                text="Changed requirement before native write",
            )
        return NativeSummary(
            "Provider-free ACP-owner summary",
            FieldCodec.decode(
                SummaryFiles,
                {"readFiles": ["src/retained.py"], "modifiedFiles": ["src/changed.py"]},
            ),
            FieldCodec.decode(
                SummaryUsage,
                {
                    "input": 7,
                    "output": 4,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 11,
                    "cost": {
                        "input": 0.0,
                        "output": 0.0,
                        "cacheRead": 0.0,
                        "cacheWrite": 0.0,
                        "total": 0.0,
                    },
                },
            ),
        )

    agent = CommsAgent(
        comms,
        agent_bin="pi",
        agent_args=[],
        runtime_enabled=True,
        adaptive_compaction_enabled=True,
        adaptive_summary_strategy=synthetic_summary,
        auto_wake=False,
    )
    agent.on_connect(Client())
    await agent.new_session(cwd=str(project), mcp_servers=[])
    agent.inputs.drain_tasks["proj"].cancel()
    await asyncio.gather(agent.inputs.drain_tasks["proj"], return_exceptions=True)
    current = comms.registry.require("proj")
    comms.registry.register(
        replace(
            current,
            goal=Goal("retain history", "goal-acp"),
            session_file=str(session),
            model=info.model,
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    comms.agents.set_agent_info(
        "proj",
        model=info.model,
        context_used=info.context_used,
        context_size=info.context_size,
    )
    from agent_comms.goal_attempts import GoalAttemptStore

    private = root / "goal-private"
    private.mkdir(mode=0o700)
    store = GoalAttemptStore.initialize(private)
    store.create_goal("goal-acp")
    agent.turns.goal_store = store
    admission = comms.registry.snapshot().admission_generations["proj"]
    InputDispositions(root / InputDispositions.filename).record(
        "acp:original",
        seq=None,
        owner="proj",
        admission=admission,
        target="proj",
        text="Original new task",
    )
    dispatched = []

    async def fake_native_stream(*args, **kwargs):
        task = args[2]
        native_id = "a" * 32
        assert agent.turns.persistent_backends["proj"].reopen_required == str(session)
        with kwargs["send_boundary"](None, native_id, task) as allowed:
            assert allowed is True
            dispatched.append(native_id)
        assert kwargs["native_start"](None, native_id, task)
        yield ae.InputStarted(id=None)
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="processed")

    monkeypatch.setattr(backend, "stream_agent_events", fake_native_stream)
    assert comms.registry.require("proj").role.executable
    try:
        turn = agent.turns.run_agent_turn(
            "proj",
            "proj",
            "Original new task",
            original_keys=("acp:original",),
            original_owner_input=True,
            original_goal_id="goal-acp",
        )
        if correction:
            with pytest.raises(RelationViolationError, match="Unsettled"):
                await turn
            assert dispatched == []
            assert (
                InputDispositions(root / InputDispositions.filename)
                .read()
                .rows["acp:original"]
                .declared_name
                == "unknown"
            )
            assert comms.registry.require("proj").goal.state.declared_name == "blocked"
        else:
            await turn
            assert dispatched == ["a" * 32]
            assert (
                InputDispositions(root / InputDispositions.filename)
                .read()
                .rows["acp:original"]
                .declared_name
                == "started"
            )
        compactions = [
            row
            for line in session.read_text().splitlines()
            if (row := json.loads(line))["type"] == "compaction"
        ]
        assert len(compactions) == (0 if correction else 1)
        if not correction:
            assert compactions[0]["details"]["readFiles"] == ["src/retained.py"]
            assert compactions[0]["details"]["modifiedFiles"] == ["src/changed.py"]
            assert compactions[0]["usage"]["totalTokens"] == 11
    finally:
        await agent.shutdown()


async def test_adaptive_owner_selected_model_mismatch_skips_without_provider(admitted):
    registry, session, info = admitted
    called = False

    async def summarize(_prepared):
        nonlocal called
        called = True
        return NativeSummary("forbidden", None, None)

    different = AgentRuntimeInfo(
        thread="owner",
        model="other/model",
        context_used=950,
        context_size=1000,
    )
    assert not await maybe_compact_owner_turn(
        registry,
        "unused-launcher",
        "owner",
        "turn",
        different,
        "acp:original",
        PersistentPiSession(),
        summary_strategy=summarize,
    )
    assert not called
    assert (
        InputDispositions(session.parent.parent / InputDispositions.filename)
        .read()
        .rows.get("acp:original")
        .native_id
        is None
    )
