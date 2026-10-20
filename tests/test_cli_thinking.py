"""Stopped-thread configuration uses the session's model-specific catalog."""

import pytest

from agent_comms.cli_commands import ConfigureCliCommand
from agent_comms.config_options import ThinkingLevelConfigOption
from agent_comms.native_arguments import NativeArguments
from agent_comms.pi_commands import GetAvailableModels, GetAvailableThinkingLevels
from agent_comms.pi_payloads import ModelsData, ReportedModel, ThinkingLevelsData
from agent_comms.pi_vocabulary import HighThinkingLevel, OffThinkingLevel, ServiceTier
from agent_comms.threads import Thread


def test_configure_refuses_model_outside_catalog_without_writing(comms, tmp_path, monkeypatch, configured_catalog):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "fixture/reasoning")
    comms.registry.declare(comms.registry.new_thread(name='worker', tags=frozenset(), worktree=str(tmp_path), model='fixture/reasoning', thinking_level=HighThinkingLevel))
    comms.registry.unregister("worker")
    original = comms.registry.require("worker")

    with pytest.raises(ValueError, match="Model 'fixture/plain' is unavailable"):
        ConfigureCliCommand(name="worker", model="fixture/plain").apply(comms)
    assert comms.registry.require("worker") == original


def test_native_arguments_configured_has_one_default(monkeypatch):
    monkeypatch.delenv(NativeArguments.arguments_key, raising=False)
    monkeypatch.delenv(NativeArguments.binary_key, raising=False)
    assert NativeArguments.configured_binary() == NativeArguments.default_binary
    assert NativeArguments.configured().argv == ()
    monkeypatch.setenv(NativeArguments.binary_key, "/fixture/pi")
    monkeypatch.setenv(NativeArguments.arguments_key, '--model "fixture/model" --thinking off')
    assert NativeArguments.configured_binary() == "/fixture/pi"
    assert NativeArguments.configured().model == "fixture/model"
    assert NativeArguments.configured().thinking is OffThinkingLevel
    assert NativeArguments.configured([]).argv == ()


