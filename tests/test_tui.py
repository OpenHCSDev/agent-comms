"""Chat TUI tests: login, views, sending, fork."""

import pytest

from agent_comms import Thread
from agent_comms.tui import CommsApp


def widget_text(widget) -> str:
    return str(widget.visual)


@pytest.fixture
def app(wired):
    return CommsApp(wired, thread_name="fixer")


async def test_mount_shows_channels_and_presence(app, wired):
    async with app.run_test() as pilot:
        await pilot.pause()
        sidebar = widget_text(app.query_one("#sidebar"))
        assert "#all" in sidebar
        assert "PR111" in sidebar and "fixer" in sidebar
        assert "WHO'S HERE" in sidebar


async def test_default_view_is_global_and_shows_history(app, wired):
    async with app.run_test() as pilot:
        wired.broadcast("PR111", "main is green")
        await pilot.press("r")
        await pilot.pause()
        chat = widget_text(app.query_one("#inbox"))
        assert "PR111" in chat and "main is green" in chat


async def test_typing_sends_to_current_view(app, wired):
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt")
        prompt.focus()
        prompt.value = "anyone seen the ci flake?"
        await pilot.press("enter")
        await pilot.pause()
        # fixer typed into #all; every peer got it.
        assert wired.pending_count("PR111") == 1


async def test_cycle_to_dm_and_send(app, wired):
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")  # to a tag channel or DM view
        prompt = app.query_one("#prompt")
        prompt.focus()
        prompt.value = "ping"
        await pilot.press("enter")
        await pilot.pause()
        # The message went somewhere valid (current view target).
        assert wired.bus.total_messages() == 1


async def test_login_flow_registers_human(app, wired):
    app._me = None
    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt")
        prompt.focus()
        prompt.value = "tristan"
        await pilot.press("enter")
        await pilot.pause()
        assert "tristan" in wired.registry
        assert wired.registry.require("tristan").tags == frozenset({"human"})


async def test_fork_flow(app, wired, monkeypatch, tmp_path):
    session = tmp_path / "session.json"
    session.write_text("{}")
    wired.register(
        Thread(name="base", tags=frozenset(), worktree=str(tmp_path), session_file=str(session))
    )

    class FakePopen:
        def __init__(self, args, **kwargs):
            self.pid = 7

    monkeypatch.setattr("agent_comms.operations.subprocess.Popen", FakePopen)

    async with app.run_test() as pilot:
        await pilot.press("f")
        await pilot.pause()
        prompt = app.query_one("#prompt")
        assert prompt.has_focus
        prompt.focus()
        prompt.value = "kid base do stuff"
        await pilot.press("enter")
        await pilot.pause()
        assert "kid" in wired.registry
        assert wired.thread_detail("kid")["pid"] == 7


async def test_fork_error_is_surfaced(app, wired):
    async with app.run_test() as pilot:
        await pilot.press("f")
        await pilot.pause()
        prompt = app.query_one("#prompt")
        prompt.focus()
        prompt.value = "only-one-part"
        await pilot.press("enter")
        await pilot.pause()
        chat = widget_text(app.query_one("#inbox"))
        assert "fork expects" in chat


async def test_fork_escape_cancels(app, wired):
    async with app.run_test() as pilot:
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app._fork_mode is False


async def test_quit_binding(app):
    async with app.run_test() as pilot:
        await pilot.press("q")


async def test_refresh_updates_presence(app, wired):
    async with app.run_test() as pilot:
        await pilot.pause()
        wired.register(Thread(name="newcomer", tags=frozenset(), worktree="/wt"))
        await pilot.press("r")
        await pilot.pause()
        sidebar = widget_text(app.query_one("#sidebar"))
        assert "newcomer" in sidebar
