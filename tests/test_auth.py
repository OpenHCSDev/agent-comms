import pytest

from agent_comms import backend, wire
from agent_comms.acp import CommsAgent
from agent_comms.login import run_login


async def test_terminal_auth_is_only_advertised_when_supported(tmp_path):
    agent = CommsAgent(wire(tmp_path), agent_bin="pi")
    assert not (await agent.initialize(1, {})).auth_methods
    methods = (await agent.initialize(1, {"auth": {"terminal": True}})).auth_methods
    codex = next(method for method in methods if method.id == "pi-login-openai-codex")
    assert codex.model_dump(by_alias=True)["type"] == "terminal"
    assert codex.args == ["--login", "openai-codex"]


def test_login_uses_pi_native_ui_without_managed_thread_identity(monkeypatch):
    monkeypatch.setenv("PI_AGENT_ID", "must-not-be-adopted")
    monkeypatch.setenv("AGENT_COMMS_MANAGED", "1")
    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "pi-custom")
    captured = {}

    def call(args, env):
        captured.update(args=args, env=env)
        return 0

    monkeypatch.setattr("agent_comms.login.subprocess.call", call)
    assert run_login("openai-codex") == 0
    assert captured["args"][0] == "pi-custom"
    assert "--no-session" in captured["args"] and "--no-tools" in captured["args"]
    assert "--mode" not in captured["args"] and "--print" not in captured["args"]
    assert captured["env"]["AGENT_COMMS_LOGIN_PROVIDER"] == "openai-codex"
    assert "PI_AGENT_ID" not in captured["env"]
    assert "AGENT_COMMS_MANAGED" not in captured["env"]
    with pytest.raises(ValueError):
        run_login("invalid\ncommand")


async def test_changed_auth_refreshes_catalogue_without_changing_selected_model(
    tmp_path, monkeypatch
):
    revision = [0]
    monkeypatch.setattr(backend, "auth_revision", lambda: (revision[0], 0))

    async def discover(*args):
        models = [backend.Model("test/base", "Base")]
        if revision[0]:
            models.append(backend.Model("openai-codex/test", "Subscription"))
        return models

    monkeypatch.setattr(backend, "discover_models", discover)
    agent = CommsAgent(wire(tmp_path), agent_args=["--model", "test/base"])
    # Exercise the explicit refresh path without the background drain racing it.
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session_id: None)
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    agent.on_connect(Client())
    session = (await agent.new_session("/tmp/project")).session_id
    try:
        await agent._refresh_auth_models()
        assert not updates, "The session response already supplied this catalogue"
        revision[0] = 1
        await agent._refresh_auth_models()
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
        await agent._refresh_auth_models()
        assert len(updates) == published
    finally:
        await agent.shutdown()
