from agent_comms.native_arguments import NativeArguments
import pytest

from agent_comms import backend
from agent_comms.acp import CommsAgent

from agent_comms.config_options import ConfigChoice, InvalidConfigChoice, ModelConfigOption
from agent_comms.session_lifecycle import SessionConfigOptions
from agent_comms.login import run_login
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent
from agent_comms.comms import Comms
from agent_comms.active_route import LocalRoute, PinnedRoute, resolve_comms_route
from agent_comms.thread_identity import ThreadIncarnation
import dataclasses


async def test_terminal_auth_is_only_advertised_when_supported(tmp_path):
    selected_comms = Comms(resolve_comms_route(tmp_path), runtime=LaunchRuntime.current())
    selected_comms.settings = dataclasses.replace(selected_comms.settings)
    agent = CommsAgent(selected_comms)
    assert not (await agent.initialize(1, {})).auth_methods
    methods = (await agent.initialize(1, {"auth": {"terminal": True}})).auth_methods
    method, = methods
    assert method.id == "pi-login"
    assert method.model_dump(by_alias=True)["type"] == "terminal"
    assert method.args == ["--login"]


async def test_empty_native_catalogue_advertises_setup_without_accepting_unset_model(
    tmp_path, monkeypatch
):
    async def discover(self, thread):
        return []

    monkeypatch.setattr(ModelConfigOption, "discover", discover)
    comms = Comms(resolve_comms_route(tmp_path), runtime=LaunchRuntime.running())
    option = ModelConfigOption(
        comms, RestartEnvironment(native_config=tmp_path, agent_directory=tmp_path),
    )
    thread = Thread(name="setup", tags=frozenset(), worktree=str(tmp_path))
    description = await option.describe(thread)
    assert description.current_value is None
    assert description.choices == ()
    advertised = SessionConfigOptions.advertisement(description)
    assert advertised.current_value == ""
    assert [choice.value for choice in advertised.options] == [""]
    with pytest.raises(InvalidConfigChoice):
        await option.validate(thread, "")

    selected = dataclasses.replace(thread, model="saved/model")
    with pytest.raises(InvalidConfigChoice, match="saved/model"):
        await option.describe(selected)
    assert selected.model == "saved/model"


def test_login_uses_pi_native_ui_without_managed_thread_identity(tmp_path, monkeypatch):
    package = tmp_path / "native"
    wire = tmp_path / "wire"
    runtime = LaunchRuntime.running()
    root_id = Comms(LocalRoute(wire), runtime=runtime).messaging.initialize_private_initial_protocol()
    comms = Comms(PinnedRoute(wire, root_id, package), runtime=runtime)
    monkeypatch.setattr("agent_comms.native_pi._trusted_package",
                        lambda selected: selected / "dist/cli.js")
    captured = {}

    def call(args, cwd, env):
        captured.update(args=args, cwd=cwd, env=env)
        return 0

    monkeypatch.setattr("agent_comms.login.subprocess.call", call)
    assert run_login("openai-codex", comms=comms) == 0
    assert captured["args"][0] == "node"
    assert str(package / "dist/pi_login_control.mjs") in captured["args"]
    assert "--no-session" in captured["args"] and "--no-tools" in captured["args"]
    assert "--mode" not in captured["args"] and "--print" not in captured["args"]
    assert ThreadIncarnation.value_flag not in captured["args"]
    assert captured["env"]["AGENT_COMMS_LOGIN_PROVIDER"] == "openai-codex"
    with pytest.raises(ValueError):
        run_login("invalid\ncommand", comms=comms)


async def test_changed_auth_refreshes_catalogue_without_changing_selected_model(
    tmp_path, monkeypatch, fake_native_catalog
):
    revision = [0]
    monkeypatch.setattr("agent_comms.launch_runtime.RestartEnvironment.auth_revision", lambda self: (revision[0], 0))

    async def discover(*args):
        models = [ConfigChoice(value="test/base", name="Base")]
        if revision[0]:
            models.append(ConfigChoice(value="openai-codex/test", name="Subscription"))
        return models

    monkeypatch.setattr(ModelConfigOption, "discover", discover)
    selected_comms_2 = Comms(resolve_comms_route(tmp_path), runtime=LaunchRuntime.current())
    selected_comms_2.settings = dataclasses.replace(selected_comms_2.settings,
         arguments=NativeArguments.parse(("--model", "test/base")))
    agent = canonical_agent(selected_comms_2)
    # Exercise the explicit refresh path without the background drain racing it.
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _session_id: None)
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    agent.on_connect(Client())
    project = tmp_path / "project"
    project.mkdir()
    session = (await agent.new_session(str(project))).session_id
    try:
        await agent.sessions.config.publish_configuration(session, session)
        assert updates, "The private session response did not broadcast this catalogue"
        updates.clear()
        revision[0] = 1
        await agent.sessions.config.publish_configuration(session, session)
        # The background drain may publish this same catalogue; what matters is
        # that the refresh happens and never changes the selected model.
        assert updates
        for update in updates:
            options = update.config_options[0]
            assert options.current_value == "test/base"
            assert [option.value for option in options.options] == [
                "test/base",
                "openai-codex/test",
            ]
        assert agent._comms.registry.require(session).model == "test/base"
        published = len(updates)
        await agent.sessions.config.publish_configuration(session, session)
        assert len(updates) == published
    finally:
        await agent.shutdown()
