"""One completed native failure feeds notification and reported request failure.

The verified native CLI, HTTP provider, Core turn/application, input dispositions
and saved journal are real. Only HTTP response contents are controlled.
"""
import asyncio
import json
from pathlib import Path

import pytest

import agent_comms.native_pi as native_pi
from agent_comms.acp_extension import InputFailedUpdate, RequestFailedUpdate, decode_updates
from agent_comms.acp_failure import ACPFailure, NativeMessageFailureSource, NativeTurnError
from compaction_loopback import LoopbackProvider
from delivery_owner_fixture import canonical_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("notification_fails", [False, True])
@pytest.mark.parametrize("refuse_before_terminal", [False, True])
async def test_actual_native_outcome_classifies_once_and_next_input_is_independent(
    private_comms, tmp_path, monkeypatch, notification_fails, refuse_before_terminal,
):
    provider = LoopbackProvider(status=200, text="saved isolated history",
                                error_message="insufficient_quota")
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    (config / "models.json").write_text(json.dumps({"providers": {"outcome-offline": {
        "baseUrl": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",
        "api": "openai-completions", "models": [{"id": "fixture", "name": "Local",
            "contextWindow": 32768, "maxTokens": 2048}],
    }}}))
    (config / "auth.json").write_text(json.dumps({"outcome-offline": {
        "type": "api_key", "key": "offline-only-fixture",
    }}))
    (config / "settings.json").write_text(json.dumps({
        "compaction": {"enabled": False}, "retry": {"enabled": False, "maxRetries": 0,
            "provider": {"maxRetries": 0}},
    }))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    monkeypatch.setenv("PI_PROVIDER", "outcome-offline")
    monkeypatch.setenv("PI_MODEL", "fixture")
    monkeypatch.setenv("PI_REASONING_LEVEL", "off")
    launcher = str(Path(__file__).parents[1] / "stack/bin/pi-native")
    arguments = ["--provider", "outcome-offline", "--model", "fixture", "--thinking", "off",
                 "--no-extensions", "--no-skills", "--no-context-files"]
    agent = canonical_agent(private_comms, agent_bin=launcher, agent_args=arguments, runtime_enabled=True)
    updates = []
    attempted = []

    class Client:
        async def session_update(self, session_id, update):
            attempted.append(update)
            if notification_fails and any(isinstance(item, RequestFailedUpdate)
                                          for item in decode_updates(update.field_meta)):
                raise RuntimeError("isolated notification transport failed")
            updates.append(update)

    agent.sessions.client = Client()
    calls = []
    classify = ACPFailure.from_detail

    def once(cls, code, text, **kwargs):
        calls.append(text)
        return classify(code, text, **kwargs)

    monkeypatch.setattr(ACPFailure, "from_detail", classmethod(once))
    try:
        async with asyncio.timeout(25):
            await agent.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
            await agent.turns.run_agent_turn("project", "project", "Seed this new isolated session",
                                            original_owner_input=True)
            saved = Path(private_comms.registry.require("project").session_file)
            original = saved.read_bytes()
            # This existing HTTP fixture supports 400/503 errors. The shared
            # classifier's explicit quota exclusion overrides server-error text.
            provider.status = 400
            if refuse_before_terminal:
                class AfterRequestHeaders:
                    async def wait(self):
                        # Response selection follows the existing HTTP owner's
                        # parsed request count, not acceptance of a TCP socket.
                        provider.status = 503 if provider.posts == 2 else 400
                        provider.error_message = (
                            "Our servers are currently overloaded. Please try again later."
                            if provider.posts == 2 else "insufficient_quota"
                        )
                provider.response_gate = AfterRequestHeaders()
            with pytest.raises(NativeTurnError) as caught:
                await agent.turns.run_agent_turn("project", "project", "New input receiving a refusal",
                                                original_owner_input=True)
            failures = [item for update in attempted for item in decode_updates(update.field_meta)
                        if isinstance(item, RequestFailedUpdate)]
            assert len(failures) == 1
            failure = failures[0]
            assert isinstance(failure.source, NativeMessageFailureSource)
            assert failure.failure.input_state.has_started
            assert len(calls) == 1
            if refuse_before_terminal:
                assert failure.failure.retry_result.attempt == 1
                assert failure.failure.retry_result.success is False
                assert "insufficient_quota" in failure.failure.retry_result.final_error
            else:
                assert failure.failure.retry_result is None
            request_failure = caught.value.request
            assert request_failure.notification_published is (not notification_fails)
            assert request_failure.failure == failure.failure
            assert not any(isinstance(item, InputFailedUpdate) for update in updates
                           for item in decode_updates(update.field_meta))
            assert saved.read_bytes().startswith(original)
            provider.response_gate = None
            provider.status = 200
            provider.text = "answered a genuinely new independent input"
            await agent.turns.run_agent_turn("project", "project", "Distinct new work after settled failure",
                                            original_owner_input=True)
            assert len(calls) == 1  # Successful completion does not reclassify an old error.
            assert provider.posts == 3 + int(refuse_before_terminal)
            # Only a native re-request adds an HTTP post; user commits stay three.
            journal = [json.loads(line) for line in saved.read_text().splitlines()]
            users = [row["message"] for row in journal if row.get("type") == "message"
                     and row["message"]["role"] == "user"]
            assert len(users) == 3 and len({row["inputId"] for row in users}) == 3
    finally:
        await agent.shutdown()
        server.close()
        await server.wait_closed()
