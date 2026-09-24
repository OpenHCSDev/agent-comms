"""No-provider negative gates for the saved-session Pi compact runner."""

from __future__ import annotations

import asyncio
import signal

from agent_comms import manual_compaction as compact


async def test_unsupported_platform_refuses_before_policy_or_child(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No private profile or subprocess on unsupported platform")

    monkeypatch.setattr(compact, "_supported_platform", lambda: False)
    monkeypatch.setattr(compact, "_private_policy", forbidden)
    monkeypatch.setattr(compact.asyncio, "create_subprocess_exec", forbidden)
    result = await compact.compact_session(
        "pi", ["--provider", "openrouter", "--model", "fake"], "/missing", "/missing"
    )
    assert result == {"ok": False, "error": "Saved-session compaction requires POSIX teardown."}


async def test_broken_stdin_close_does_not_skip_signal_and_reap(monkeypatch):
    calls = []

    class BrokenStdin:
        def is_closing(self):
            return False

        def close(self):
            calls.append("close")
            raise BrokenPipeError("the child closed its read end")

    class Child:
        stdin = BrokenStdin()
        pid = 1_000_000_000
        returncode = 0

        async def wait(self):
            calls.append("wait")
            return 0

    monkeypatch.setattr(compact, "_signal_group", lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(compact, "_group_alive", lambda pid: False)
    # The caller already considers this attempt uncertain and requests force.
    # BrokenPipe must not prevent termination and a completed wait.
    clean = await asyncio.wait_for(compact._shutdown(Child(), force=True), 1)
    assert clean is False
    assert calls == ["close", (Child.pid, signal.SIGTERM), "wait"]
