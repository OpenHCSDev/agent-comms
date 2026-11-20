"""CLM context history in git, and the slot's state handed between conversations."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_comms.execution import context_state
from agent_comms.execution.context_state import (
    ColdSlot,
    ContextStateStore,
    KeptSlot,
    RestoredSlot,
    SlotServer,
    UnavailableSlot,
)


class FakeLlama:
    """A llama-server with one slot: it remembers what was saved under each filename."""

    def __init__(self, slots) -> None:
        self.slot = 0  # tokens of state the slot holds
        self.slots = slots
        slots.mkdir()
        self.calls: list[str] = []
        self.build = "b1"
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *arguments) -> None:
                pass

            def answer(self, body: dict) -> None:
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                self.answer({"model_path": "/models/clm.gguf", "build_info": fake.build})

            def do_POST(self) -> None:
                name = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["filename"]
                action = self.path.split("action=")[1]
                fake.calls.append(f"{action} {name}")
                if action == "save":
                    (fake.slots / name).write_bytes(b"s" * fake.slot)  # one byte per token
                    self.answer({"n_saved": fake.slot})
                else:
                    fake.slot = (fake.slots / name).stat().st_size
                    self.answer({"n_restored": fake.slot})

        self.http = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.http.server_port}"


@pytest.fixture
def llama(tmp_path):
    fake = FakeLlama(tmp_path / "slots")
    yield fake
    fake.http.shutdown()


@pytest.fixture
def store(tmp_path, llama, monkeypatch):
    monkeypatch.setattr(context_state, "LIVE_CONTEXT_ROOT", tmp_path / "live")
    return ContextStateStore(tmp_path / "state", SlotServer(llama.url), llama.slots)


def publish(tmp_path, session: str, *turns: tuple[str, str]) -> None:
    directory = tmp_path / "live" / session
    directory.mkdir(parents=True, exist_ok=True)
    (directory / context_state.VIEW_NAME).write_text(json.dumps({
        "version": 1, "system": "SYSTEM",
        "turns": [{"role": role, "text": text} for role, text in turns],
        "budget": {"strict_target": 26624},
    }))


def turn(store, tmp_path, llama, session: str, *turns: tuple[str, str], tokens: int):
    """One turn of ``session``: take the slot, the model fills it, the context is recorded."""
    outcome = store.enter(session)
    llama.slot = tokens
    publish(tmp_path, session, *turns)
    store.history(session).record()
    return outcome


def test_a_turn_end_commits_the_context_as_files_with_a_history(store, tmp_path):
    publish(tmp_path, "a", ("user", "TASK"), ("assistant", "plan"), ("tool", "big output"))
    first = store.history("a").record()
    publish(tmp_path, "a", ("user", "TASK"), ("assistant", "plan"), ("user", "[compacted]"))
    second = store.history("a").record()

    branch = store.history("a").branch()
    assert first != second
    assert branch.list_files("ctx") == [
        "ctx/0001-user.txt", "ctx/0002-assistant.txt", "ctx/0003-user.txt"]
    assert branch.load_text("ctx/0003-user.txt") == "[compacted]"
    assert branch.load_text("system.txt") == "SYSTEM"
    assert [commit.message for commit in branch.history()] == [
        "context: 3 turns", "context: 3 turns"]
    before = branch.history()[-1].id
    assert branch.load_text("ctx/0003-tool.txt", ref=before) == "big output"
    assert store.history("never-published").record() is None


def test_returning_to_a_conversation_restores_the_state_saved_when_it_lost_the_slot(
        store, tmp_path, llama):
    assert isinstance(turn(store, tmp_path, llama, "a", ("user", "A"), tokens=100), ColdSlot)
    assert isinstance(turn(store, tmp_path, llama, "b", ("user", "B"), tokens=200), ColdSlot)
    back = store.enter("a")

    assert back == RestoredSlot(100)
    assert llama.slot == 100
    assert llama.calls == ["save a.bin", "save b.bin", "restore a.bin"]
    assert store.holder() == "a"


def test_a_conversation_that_keeps_the_slot_touches_nothing(store, tmp_path, llama):
    turn(store, tmp_path, llama, "a", ("user", "A"), tokens=100)
    llama.calls.clear()

    assert store.enter("a") == KeptSlot()
    assert llama.calls == []


def test_state_saved_under_another_server_build_is_not_restored(store, tmp_path, llama):
    turn(store, tmp_path, llama, "a", ("user", "A"), tokens=100)
    turn(store, tmp_path, llama, "b", ("user", "B"), tokens=200)
    llama.build = "b2"
    llama.calls.clear()

    assert isinstance(store.enter("a"), ColdSlot)
    assert llama.calls == ["save b.bin"]


def test_state_is_not_restored_once_its_context_changed(store, tmp_path, llama):
    turn(store, tmp_path, llama, "a", ("user", "A"), tokens=100)
    turn(store, tmp_path, llama, "b", ("user", "B"), tokens=200)
    publish(tmp_path, "a", ("user", "A, rewritten elsewhere"))
    store.history("a").record()
    llama.calls.clear()

    outcome = store.enter("a")
    assert outcome == ColdSlot("its context changed after the state was saved")
    assert llama.calls == ["save b.bin"]


def test_an_empty_slot_is_never_saved_as_a_conversations_state(store, tmp_path, llama):
    turn(store, tmp_path, llama, "a", ("user", "A"), tokens=100)
    turn(store, tmp_path, llama, "b", ("user", "B"), tokens=200)
    assert store.enter("a") == RestoredSlot(100)   # b's 200 tokens are saved
    llama.slot = 0  # the server restarted while a held the slot

    assert store.enter("b") == RestoredSlot(200)   # a's empty slot is not saved as a's state
    assert store.enter("a") == ColdSlot("this conversation has no saved state")


def test_saved_states_beyond_the_disk_budget_are_deleted_oldest_first(store, tmp_path, llama):
    import os
    from dataclasses import replace

    small = replace(store, budget_bytes=450)
    for age, (session, tokens) in enumerate((("a", 100), ("b", 200), ("c", 300), ("d", 50))):
        turn(small, tmp_path, llama, session, ("user", session), tokens=tokens)
        for state in llama.slots.glob("*.bin"):   # distinct save times, oldest first
            if state.stat().st_mtime > 1_000_000:
                os.utime(state, (age, age))

    assert sorted(state.name for state in llama.slots.glob("*.bin")) == ["c.bin"]
    assert small.enter("a") == ColdSlot("its saved state was deleted to fit the disk budget")
    assert small.enter("c") == RestoredSlot(300)


def test_an_unreachable_server_is_said_and_no_holder_is_assumed(tmp_path, monkeypatch):
    monkeypatch.setattr(context_state, "LIVE_CONTEXT_ROOT", tmp_path / "live")
    store = ContextStateStore(
        tmp_path / "state", SlotServer("http://127.0.0.1:9", seconds=2), tmp_path / "slots")

    assert isinstance(store.enter("a"), UnavailableSlot)
    assert store.holder() is None


def test_a_conversation_is_named_by_its_transcript_header(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"id":"abc123","type":"session","version":3}\n{"type":"message"}\n')

    assert context_state.conversation(transcript) == "abc123"
    assert context_state.conversation(tmp_path / "missing.jsonl") is None
    assert context_state.conversation(None) is None


def test_a_providers_turn_runs_its_start_then_its_end_even_when_the_turn_fails():
    import asyncio

    from agent_comms.execution.native_arguments import ModelProvider

    seen: list[str] = []

    class Recording(ModelProvider, declared_name="recording-test-provider"):
        @classmethod
        def turn_starting(cls, session_file):
            seen.append(f"start {session_file}")

        @classmethod
        def turn_ended(cls, session_file):
            seen.append(f"end {session_file}")

    async def failing_turn():
        async with Recording.turn("s.jsonl"):
            seen.append("turn")
            raise RuntimeError("the model failed")

    with pytest.raises(RuntimeError):
        asyncio.run(failing_turn())
    assert seen == ["start s.jsonl", "turn", "end s.jsonl"]


def test_a_provider_only_pi_configures_runs_as_the_base_provider():
    from agent_comms.execution.native_arguments import (
        ClmLocalProvider,
        ConfiguredProvider,
        SelectedModel,
    )

    assert SelectedModel("some-local-server", "m").provider_type is ConfiguredProvider
    assert SelectedModel("clm-local", "m").provider_type is ClmLocalProvider
