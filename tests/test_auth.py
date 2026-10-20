import json
from pathlib import Path

import pytest
from acp.schema import SessionConfigSelectOption

import agent_comms.backend as backend
from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.config_options import InvalidConfigChoice, ModelConfigOption
from agent_comms.login import run_login
from agent_comms.native_arguments import NativeArguments
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent


async def test_terminal_auth_is_only_advertised_when_supported(tmp_path):
    agent = CommsAgent(wire(tmp_path), agent_bin="pi")
    assert not (await agent.initialize(1, {})).auth_methods
    methods = (await agent.initialize(1, {"auth": {"terminal": True}})).auth_methods
    method, = methods
    assert method.id == "pi-login"
    assert method.model_dump(by_alias=True)["type"] == "terminal"
    assert method.args == ["--login"]


async def test_empty_native_catalogue_advertises_setup_without_accepting_unset_model(tmp_path, monkeypatch, configured_catalog, *, thread_registration):
    async def discover(self, thread):
        return []

    monkeypatch.setattr(ModelConfigOption, "discover", discover)
    option = configured_catalog.catalog_for(ModelConfigOption)
    thread = thread_registration.new_thread(name='setup', tags=frozenset(), worktree=str(tmp_path))
    advertised = await option.describe(thread)
    assert advertised.current_value == ""
    assert [choice.value for choice in advertised.options] == [""]
    with pytest.raises(InvalidConfigChoice):
        await option.validate(thread, "")

    from dataclasses import replace

    selected = replace(thread, model="saved/model")
    with pytest.raises(NativePiUnavailable, match="saved/model"):
        await option.describe(selected)
    assert selected.model == "saved/model"


def test_login_runs_the_verified_native_package_without_managed_thread_identity(
    monkeypatch, configured_catalog
):
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("AGENT_COMMS_THREAD", "must-not-be-adopted")
    monkeypatch.setenv("AGENT_COMMS_MANAGED", "1")
    monkeypatch.setenv("NODE_OPTIONS", "--require=ambient.js")
    captured = {}

    def call(args, env):
        captured.update(args=args, env=env)
        return 0

    monkeypatch.setattr("agent_comms.login.subprocess.call", call)
    assert run_login("openai-codex") == 0
    package = configured_catalog.package
    arguments = captured["args"]
    assert tuple(arguments[:len(package.node())]) == package.node()
    extension = Path(arguments[arguments.index("-e") + 1])
    assert extension == package.cli.with_name("pi_login_control.mjs")
    manifest = json.loads((Path(__file__).parents[1] / "stack/native-import-manifest.json").read_text())
    assert {"entry": "dist/pi_login_control.mjs"} in manifest["extensionEntries"]
    assert "--no-session" in arguments and "--no-tools" in arguments
    assert "--mode" not in arguments and "--print" not in arguments
    assert captured["env"]["AGENT_COMMS_LOGIN_PROVIDER"] == "openai-codex"
    assert "AGENT_COMMS_THREAD" not in captured["env"]
    assert "AGENT_COMMS_MANAGED" not in captured["env"]
    assert "NODE_OPTIONS" not in captured["env"]
    with pytest.raises(ValueError):
        run_login("invalid\ncommand")


