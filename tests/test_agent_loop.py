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
