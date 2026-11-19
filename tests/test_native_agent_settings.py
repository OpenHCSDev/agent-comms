from __future__ import annotations

import json

from agent_comms.execution.native_arguments import SelectedModel
from agent_comms.execution.native_session_files import NativeAgentSettings


def test_gpt_56_sol_compaction_policy_merges_and_preserves_settings(tmp_path):
    session = tmp_path / "session"
    session.mkdir(mode=0o700)
    agent = session / NativeAgentSettings.directory_name
    agent.mkdir(mode=0o700)
    settings = agent / "settings.json"
    settings.write_text(json.dumps({
        "steeringMode": "all",
        "compaction": {"keepThis": "owned by Pi", "reserveTokens": 1},
    }))
    settings.chmod(0o644)

    assert NativeAgentSettings.reconcile(
        agent, SelectedModel("openai-codex", "gpt-5.6-sol")
    )
    assert json.loads(settings.read_text()) == {
        "steeringMode": "all",
        "compaction": {
            "enabled": True,
            "keepRecentTokens": 20_000,
            "keepThis": "owned by Pi",
            "reserveTokens": 650_000,
        },
    }
    first_revision = settings.stat().st_mtime_ns
    assert not NativeAgentSettings.reconcile(
        agent, SelectedModel("openai-codex", "gpt-5.6-sol")
    )
    assert settings.stat().st_mtime_ns == first_revision


def test_other_models_keep_their_private_settings(tmp_path):
    session = tmp_path / "session"
    session.mkdir(mode=0o700)
    agent = NativeAgentSettings.prepare_directory(
        session, SelectedModel("openai-codex", "gpt-5.5")
    )

    assert not (agent / "settings.json").exists()
