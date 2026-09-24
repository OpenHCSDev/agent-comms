"""Participant agent loop: a thread that answers its own inbox."""

import sys
from pathlib import Path

import pytest

from agent_comms import Thread
from agent_comms.agent_loop import Participant
from agent_comms.operations import wire

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="participant tests exec shell-script stubs; POSIX only"
)


def _echo_stub(tmp_path: Path, reply: str) -> Path:
    stub = tmp_path / "echo-agent"
    stub.write_text(f"#!/bin/sh\ncat >/dev/null\necho {reply!r}\n")
    stub.chmod(0o755)
    return stub


class TestParticipantDMs:
    async def test_dm_flow(self, tmp_path, monkeypatch):
        root = tmp_path / "wire"
        stub = _echo_stub(tmp_path, "on-it")
        monkeypatch.setenv("AGENT_COMMS_THREAD", "bot")
        monkeypatch.chdir(tmp_path)
        comms = wire(root)
        comms.register(Thread(name="human", tags=frozenset(), worktree=str(tmp_path)))
        comms.register(Thread(name="bot", tags=frozenset({"bot"}), worktree=str(tmp_path)))

        participant = Participant(root=root, agent_bin=str(stub), agent_args=[])
        participant.start()
        assert participant._thread_name == "bot"

        comms.send("human", "bot", "fix the flake")

        await participant._tick("bot")

        dm = [m.body for m in comms.dm_history("bot", "human")]
        assert dm == ["fix the flake", "on-it"]
        # Reply went back as a DM to the sender.
        assert comms.pending_count("human") == 1


class TestParticipantChannels:
    async def test_channel_message_gets_channel_reply(self, tmp_path, monkeypatch):
        root = tmp_path / "wire"
        stub = _echo_stub(tmp_path, "seen")
        monkeypatch.setenv("AGENT_COMMS_THREAD", "bot")
        monkeypatch.chdir(tmp_path)
        comms = wire(root)
        comms.register(Thread(name="human", tags=frozenset({"ci"}), worktree=str(tmp_path)))
        comms.register(Thread(name="bot", tags=frozenset({"ci", "bot"}), worktree=str(tmp_path)))

        participant = Participant(root=root, agent_bin=str(stub), agent_args=[])
        participant.start()

        comms.send("human", "#ci", "flake again")
        await participant._tick("bot")

        channel = [m.body for m in comms.channel_history("#ci")]
        assert channel == ["flake again", "seen"]


class TestParticipantLifecycle:
    async def test_renamed_participant_keeps_running_with_canonical_identity(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "wire"
        identity = tmp_path / "identity-agent"
        identity.write_text(
            '#!/bin/sh\ncat >/dev/null\nprintf \'%s|%s\' "$PI_AGENT_ID" "$AGENT_COMMS_ROOT"\n'
        )
        identity.chmod(0o755)
        monkeypatch.setenv("AGENT_COMMS_THREAD", "bot")
        monkeypatch.chdir(tmp_path)
        comms = wire(root)
        comms.register(Thread(name="human", tags=frozenset(), worktree=str(tmp_path)))

        participant = Participant(root=root, agent_bin=str(identity), agent_args=[])
        participant.start()
        comms.rename_self("auditor")
        comms.send("human", "bot", "identity check")

        await participant._tick("bot")

        assert participant._thread_name == "auditor"
        replies = [message for message in comms.dm_history("human", "auditor")]
        assert replies[-1].sender == "auditor"
        assert replies[-1].body == f"auditor|{root}"

    async def test_replies_run_in_sender_worktree(self, tmp_path, monkeypatch):
        root = tmp_path / "wire"
        sender_wt = tmp_path / "sender-wt"
        sender_wt.mkdir()
        capture = tmp_path / "pwd-agent"
        capture.write_text("#!/bin/sh\ncat >/dev/null\npwd\n")
        capture.chmod(0o755)
        monkeypatch.setenv("AGENT_COMMS_THREAD", "bot")
        monkeypatch.chdir(tmp_path)
        comms = wire(root)
        comms.register(Thread(name="human", tags=frozenset(), worktree=str(sender_wt)))
        comms.register(Thread(name="bot", tags=frozenset(), worktree=str(tmp_path)))

        participant = Participant(root=root, agent_bin=str(capture), agent_args=[])
        participant.start()
        comms.send("human", "bot", "where are you")
        await participant._tick("bot")

        dm = [m.body for m in comms.dm_history("bot", "human")]
        assert any(str(sender_wt) in body for body in dm)

    def test_start_without_env_uses_participant_name(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
        monkeypatch.delenv("PI_AGENT_ID", raising=False)
        monkeypatch.chdir(tmp_path)
        participant = Participant(root=tmp_path / "wire", agent_bin="/bin/true", agent_args=[])
        participant.start()
        assert participant._thread_name == "participant"

    async def test_backend_missing_drops_message(self, tmp_path, monkeypatch):
        root = tmp_path / "wire"
        monkeypatch.setenv("AGENT_COMMS_THREAD", "bot")
        monkeypatch.chdir(tmp_path)
        comms = wire(root)
        comms.register(Thread(name="human", tags=frozenset(), worktree=str(tmp_path)))
        comms.register(Thread(name="bot", tags=frozenset(), worktree=str(tmp_path)))
        participant = Participant(root=root, agent_bin="definitely-not-real-bin-xyz", agent_args=[])
        participant.start()
        comms.send("human", "bot", "hello")
        await participant._tick("bot")
        # No crash, no reply.
        assert comms.pending_count("human") == 0


class TestParticipantActivity:
    async def test_activity_trail_thinking_working_idle(self, tmp_path, monkeypatch):
        from agent_comms import ActivityState

        root = tmp_path / "wire"
        stub = _echo_stub(tmp_path, "on-it")
        monkeypatch.setenv("AGENT_COMMS_THREAD", "bot")
        monkeypatch.chdir(tmp_path)
        comms = wire(root)
        comms.register(Thread(name="human", tags=frozenset(), worktree=str(tmp_path)))
        comms.register(Thread(name="bot", tags=frozenset(), worktree=str(tmp_path)))

        participant = Participant(root=root, agent_bin=str(stub), agent_args=[])
        participant.start()
        comms.send("human", "bot", "fix the flake")
        await participant._tick("bot")

        states = [e.state.value for e in comms.activity._load() if e.thread == "bot"]
        assert states == ["thinking", "idle"]
        assert comms.activity_of("bot").state is ActivityState.IDLE

    async def test_working_activity_on_tool_use(self, tmp_path, monkeypatch):

        root = tmp_path / "wire"
        rpc_lines = "\n".join(
            [
                '{"type":"tool_execution_start","toolCallId":"t1","toolName":"bash","args":{"command":"pwd"}}',
                '{"type":"tool_execution_end","toolCallId":"t1","toolName":"bash","result":{"content":[]},"isError":false}',
                '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}',
                '{"type":"agent_settled"}',
            ]
        )
        stub = tmp_path / "pi-bot"
        stub.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            "state = json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'type': 'response', 'command': 'get_state', 'id': state['id'], "
            "'success': True, 'data': {'nativeInputProofCapability': "
            "'pi-native-input-v1-live-only'}}), flush=True)\n"
            "prompt = json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'type': 'response', 'command': 'prompt', 'id': prompt['id'], "
            "'success': True}), flush=True)\n"
            "print(json.dumps({'type': 'message_start', 'message': {'role': 'user', "
            "'content': prompt['message'], 'inputId': prompt['inputId']}}), flush=True)\n"
            f"for event in {rpc_lines.splitlines()!r}: print(event, flush=True)\n"
            "sys.stdin.readline()  # postturn get_state\n"
            "sys.stdin.readline()  # get_session_stats\n"
            "print(json.dumps({'type': 'response', 'command': 'get_session_stats', "
            "'success': True, 'data': {'contextUsage': {}}}), flush=True)\n"
        )
        stub.chmod(0o755)
        monkeypatch.setenv("AGENT_COMMS_THREAD", "bot")
        monkeypatch.chdir(tmp_path)
        comms = wire(root)
        comms.register(Thread(name="human", tags=frozenset(), worktree=str(tmp_path)))
        comms.register(Thread(name="bot", tags=frozenset(), worktree=str(tmp_path)))

        participant = Participant(root=root, agent_bin=str(stub), agent_args=[])
        participant.start()
        comms.send("human", "bot", "check cwd")
        await participant._tick("bot")

        states = [(e.state.value, e.detail) for e in comms.activity._load() if e.thread == "bot"]
        assert ("working", "bash: ") in [(s, d) for s, d in states] or any(
            s == "working" for s, d in states
        )
        assert states[-1] == ("idle", "")
