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
from agent_comms.declarations import (
    AgentRuntimeInfo,
    Goal,
    RelationViolationError,
    Thread,
    ThreadRegistry,
)
from agent_comms.input_disposition import InputDispositions
from agent_comms.operations import wire
from agent_comms.owner_compaction_adaptive import maybe_compact_owner_turn
from agent_comms.owner_compaction_prepare import prepare_native_source
from agent_comms.owner_compaction_provider import (
    NativeSummary,
    NativeSummaryError,
    summarize_native,
)
from agent_comms.owner_compaction_settings import PiCompactionDecision, PiSettingsEvidenceError

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
    registry = ThreadRegistry(tmp_path / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(tmp_path),
            pid=os.getpid(),
            session_file=str(session),
            goal=Goal("work", "goal"),
            model="openrouter/fixture",
        )
    )
    owner, epoch = registry.live_owner_with_epoch("owner")
    owner, epoch = registry.claim_live_turn_with_epoch(owner, "turn", expected_epoch=epoch)
    assert owner.active_turn is not None
    InputDispositions(tmp_path).record(
        "acp:original",
        seq=None,
        owner="owner",
        admission=owner.active_turn.admission_generation,
        target="owner",
        text="new input, not yet attempted",
    )
    monkeypatch.setattr(
        "agent_comms.owner_compaction_adaptive.package_for_launcher",
        lambda _: Path(PACKAGE),
    )
    monkeypatch.setattr(
        "agent_comms.owner_compaction_adaptive.read_compaction_decision",
        lambda *_args, **_kwargs: PiCompactionDecision(True, 100, 100, True),
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
    assert InputDispositions(session.parent.parent).get("acp:original")["native_id"] is None


async def test_adaptive_owner_one_original_input_native_commit_without_provider(admitted):
    registry, session, info = admitted
    seen = []

    async def summarize(prepared):
        seen.append(prepared)
        assert prepared.session_id
        assert prepared.preparation.witness["sessionFile"] == str(session)
        return "Provider-free synthetic owner summary"

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
    assert InputDispositions(session.parent.parent).get("acp:original")["native_id"] is None
    entries = [json.loads(line) for line in session.read_text().splitlines()]
    assert sum(row["type"] == "compaction" for row in entries) == 1


async def test_adaptive_owner_correction_during_summary_refuses_native_commit(admitted):
    registry, session, info = admitted
    before = session.read_bytes()

    async def summarize(_prepared):
        owner = registry.require("owner")
        assert owner.active_turn is not None
        InputDispositions(session.parent.parent).record(
            "acp:correction",
            seq=None,
            owner="owner",
            admission=owner.active_turn.admission_generation,
            target="owner",
            text="Retain correction",
        )
        return "Stale synthetic summary"

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
        return "Stale synthetic summary"

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
        return "forbidden"

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
    assert InputDispositions(session.parent.parent).status("acp:original") == "unknown"


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


async def test_native_summary_unsupported_model_refuses_before_provider(admitted):
    _registry, session, _info = admitted
    before = session.read_bytes()
    preparation = prepare_native_source(Path(PACKAGE), str(session), keep_recent_tokens=100)
    assert preparation is not None
    with pytest.raises(NativeSummaryError, match="Native summarization refused"):
        await summarize_native(
            Path(PACKAGE),
            preparation,
            provider="nonexistent-provider",
            model_id="nonexistent-model",
            context_window=1000,
            reserve_tokens=100,
            keep_recent_tokens=100,
        )
    assert session.read_bytes() == before


async def test_native_summary_transport_assembles_bounded_chunked_envelope(
    admitted, tmp_path, monkeypatch
):
    _registry, session, _info = admitted
    preparation = prepare_native_source(Path(PACKAGE), str(session), keep_recent_tokens=100)
    assert preparation is not None
    node = tmp_path / "fake-node"
    usage = {
        "input": 2,
        "output": 3,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 5,
        "cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0, "total": 0.0},
    }
    payload = json.dumps(
        {
            "summary": "one bounded result",
            "details": {"readFiles": ["/tmp/a"], "modifiedFiles": []},
            "usage": usage,
        }
    )
    half = len(payload) // 2
    node.write_text(
        f"#!{sys.executable}\n"
        "import sys,time\n"
        f"sys.stdout.write({payload[:half]!r});sys.stdout.flush();time.sleep(.02)\n"
        f"sys.stdout.write({payload[half:]!r});sys.stdout.flush()\n"
    )
    node.chmod(0o700)
    monkeypatch.setattr("agent_comms.owner_compaction_provider.shutil.which", lambda _: str(node))
    result = await summarize_native(
        Path(PACKAGE),
        preparation,
        provider="fake",
        model_id="model",
        context_window=1000,
        reserve_tokens=100,
        keep_recent_tokens=100,
    )
    assert result.text == "one bounded result"
    assert result.details == {"readFiles": ["/tmp/a"], "modifiedFiles": []}
    assert result.usage == usage


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
        assert prepared.preparation.witness["sessionFile"] == str(session)
        if correction:
            owner = comms.registry.require("proj")
            assert owner.active_turn is not None
            InputDispositions(root).record(
                "acp:correction",
                seq=None,
                owner="proj",
                admission=owner.active_turn.admission_generation,
                target="proj",
                text="Changed requirement before native write",
            )
        return NativeSummary(
            "Provider-free ACP-owner summary",
            {"readFiles": ["src/retained.py"], "modifiedFiles": ["src/changed.py"]},
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
    agent._drain_tasks["proj"].cancel()
    await asyncio.gather(agent._drain_tasks["proj"], return_exceptions=True)
    current = comms.registry.require("proj")
    comms.registry.register(
        replace(
            current,
            goal=Goal("retain history", "goal-acp"),
            session_file=str(session),
            model=info.model,
            pid=os.getpid(),
        )
    )
    comms.set_agent_info(
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
    agent._goal_store = store
    admission = comms.registry.snapshot().admission_generations["proj"]
    InputDispositions(root).record(
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
        assert agent._persistent_backends["proj"].reopen_required == str(session)
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
        turn = agent._run_agent_turn(
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
            assert InputDispositions(root).status("acp:original") == "unknown"
            assert comms.registry.require("proj").goal.status == "blocked"
        else:
            await turn
            assert dispatched == ["a" * 32]
            assert InputDispositions(root).status("acp:original") == "started"
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
        return "forbidden"

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
    assert InputDispositions(session.parent.parent).get("acp:original")["native_id"] is None
