from __future__ import annotations

import json

from agent_comms.execution.native_arguments import SelectedModel
from agent_comms.execution.native_session_files import NativeAgentSettings, NativeModelConfiguration


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
            "reserveTokens": 550_000,
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


def test_model_context_override_preserves_other_route_configuration(tmp_path):
    models = tmp_path / "models.json"
    models.write_text(json.dumps({
        "providers": {
            "openai-codex": {
                "baseUrl": "https://example.invalid",
                "modelOverrides": {"gpt-5.6-sol": {"maxTokens": 128_000}},
            },
        },
    }))

    selected = SelectedModel("openai-codex", "gpt-5.6-sol")
    assert NativeModelConfiguration.set_context_window(tmp_path, selected, 1_050_000)
    assert json.loads(models.read_text()) == {
        "providers": {
            "openai-codex": {
                "baseUrl": "https://example.invalid",
                "modelOverrides": {
                    "gpt-5.6-sol": {
                        "contextWindow": 1_050_000,
                        "maxTokens": 128_000,
                    },
                },
            },
        },
    }
    assert not NativeModelConfiguration.set_context_window(tmp_path, selected, 1_050_000)


def test_every_clm_local_model_leaves_compaction_to_pi_clm(tmp_path):
    """pi-clm owns a context-language model's context and cancels Pi's compaction; Pi's own
    pre-response check must not decide to compact it, whichever clm-local model runs."""
    for model in ("clm-livectx-iter44-ffc4aab2-q4_k_m.gguf", "any-future-clm.gguf"):
        session = tmp_path / model
        session.mkdir(mode=0o700)
        agent = NativeAgentSettings.prepare_directory(session, SelectedModel("clm-local", model))
        assert json.loads((agent / "settings.json").read_text())["compaction"] == {
            "enabled": False, "keepRecentTokens": 0, "reserveTokens": 0,
        }
