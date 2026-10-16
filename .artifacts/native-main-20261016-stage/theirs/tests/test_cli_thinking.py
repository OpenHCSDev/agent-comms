"""Stopped-thread configuration uses the session's model-specific catalog."""

import pytest

from agent_comms.cli_commands import ConfigureCliCommand
from agent_comms.config_options import ThinkingLevelConfigOption
from agent_comms.native_arguments import NativeArguments
from agent_comms.launch_runtime import RestartEnvironment
from agent_comms.pi_commands import GetAvailableModels, GetAvailableThinkingLevels
from agent_comms.pi_payloads import ModelsData, ReportedModel, ThinkingLevelsData
from agent_comms.pi_vocabulary import HighThinkingLevel, OffThinkingLevel, ServiceTier
from agent_comms.threads import Thread


@pytest.mark.parametrize("change", ["thinking", "model"])
@pytest.mark.parametrize("overridden", [False, True])
def test_configure_refuses_unsupported_result_without_writing(comms, tmp_path, monkeypatch, change, overridden):
    if overridden:
        monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "fixture/reasoning,fixture/plain")
    else:
        monkeypatch.delenv("AGENT_COMMS_AGENT_MODELS", raising=False)
    comms.registry.declare(Thread(
        name="worker", tags=frozenset(), worktree=str(tmp_path),
        model="fixture/reasoning", thinking_level="high",
    ))
    comms.registry.unregister("worker")
    original = comms.registry.require("worker")
    queried = []

    async def discover_models(self, binary, arguments):
        return ModelsData(models=(
            ReportedModel(provider="fixture", id="reasoning"),
            ReportedModel(provider="fixture", id="plain"),
        ))

    async def discover_levels(self, binary, arguments):
        queried.append(NativeArguments.parse(arguments).model)
        return ThinkingLevelsData(levels=(OffThinkingLevel,))

    monkeypatch.setattr(GetAvailableModels, "discover", discover_models)
    monkeypatch.setattr(GetAvailableThinkingLevels, "discover", discover_levels)
    command = ConfigureCliCommand(
        name="worker", **({"thinking": "high"} if change == "thinking" else {"model": "fixture/plain"})
    )
    with pytest.raises(ValueError, match="unavailable; choose one of: off"):
        command.apply(comms)
    assert comms.registry.require("worker") == original
    assert queried == ["fixture/reasoning" if change == "thinking" else "fixture/plain"]


def test_configure_refuses_model_outside_catalog_without_writing(comms, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "fixture/reasoning")
    comms.registry.declare(Thread(
        name="worker", tags=frozenset(), worktree=str(tmp_path),
        model="fixture/reasoning", thinking_level="high",
    ))
    comms.registry.unregister("worker")
    original = comms.registry.require("worker")

    with pytest.raises(ValueError, match="Model 'fixture/plain' is unavailable"):
        ConfigureCliCommand(name="worker", model="fixture/plain").apply(comms)
    assert comms.registry.require("worker") == original


def test_configure_accepts_model_supported_level(comms, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_COMMS_AGENT_MODELS", raising=False)
    comms.registry.declare(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path), model="fixture/reasoning"))
    comms.registry.unregister("worker")

    async def discover_models(self, binary, arguments):
        return ModelsData(models=(ReportedModel(provider="fixture", id="reasoning"),))

    async def discover_levels(self, binary, arguments):
        return ThinkingLevelsData(levels=(OffThinkingLevel, HighThinkingLevel))

    monkeypatch.setattr(GetAvailableModels, "discover", discover_models)
    monkeypatch.setattr(GetAvailableThinkingLevels, "discover", discover_levels)
    result = ConfigureCliCommand(
        name="worker", thinking="high", service_tier=ServiceTier.STANDARD
    ).apply(comms)
    assert result["thinking_level"] == "high"
    assert result["service_tier"] == "standard"
    configured = comms.registry.require("worker")
    assert configured.thinking_level is HighThinkingLevel
    assert configured.service_tier.value == "standard"


def test_native_arguments_configured_has_one_default(monkeypatch):
    monkeypatch.delenv(NativeArguments.arguments_key, raising=False)
    monkeypatch.delenv(NativeArguments.binary_key, raising=False)
    assert NativeArguments.configured_binary() == NativeArguments.default_binary
    assert NativeArguments.configured().argv == NativeArguments.default
    monkeypatch.setenv(NativeArguments.binary_key, "/fixture/pi")
    monkeypatch.setenv(NativeArguments.arguments_key, '--model "fixture/model" --thinking off')
    assert NativeArguments.configured_binary() == "/fixture/pi"
    assert NativeArguments.configured().model == "fixture/model"
    assert NativeArguments.configured().thinking == "off"
    assert NativeArguments.configured([]).argv == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("overridden", [False, True])
async def test_session_description_and_change_share_refusal(tmp_path, monkeypatch, overridden):
    import os
    from agent_comms.native_pi import NativePiUnavailable
    from acp import RequestError

    if overridden:
        monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "fixture/plain")
    else:
        monkeypatch.delenv("AGENT_COMMS_AGENT_MODELS", raising=False)
    thread = Thread(name="worker", tags=frozenset(), worktree=str(tmp_path), model="fixture/plain", thinking_level="high")
    option = ThinkingLevelConfigOption("unused", NativeArguments.parse([]), RestartEnvironment.inherit(os.environ))

    async def discover(self, binary, arguments):
        return ThinkingLevelsData(levels=(OffThinkingLevel,))

    monkeypatch.setattr(GetAvailableThinkingLevels, "discover", discover)
    with pytest.raises(NativePiUnavailable, match="choose one of: off"):
        await option.describe(thread)
    with pytest.raises(RequestError) as refused:
        await option.change(None, thread, "high")
    assert "choose one of: off" in refused.value.data["reason"]
