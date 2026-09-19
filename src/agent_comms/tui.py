"""OpenHCS agent communications — Textual TUI.

The chat. IRC feel: log in, see who's in the room, switch between the global
channel, tag channels, and DMs, and type into the always-on input. Humans and
agents share one wire; every action delegates to Comms operations.

Keys:
    n / p  next / previous view     r  refresh
    f  fork                         q  quit
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Static

from .declarations import GLOBAL_CHANNEL
from .operations import Comms, wire


def _fmt_age(ts: float, now: float | None = None) -> str:
    if ts <= 0:
        return "never"
    age = (now if now is not None else time.time()) - ts
    if age < 60:
        return "now" if age < 10 else f"{int(age)}s"
    if age < 3600:
        return f"{int(age // 60)}m"
    if age < 86400:
        return f"{int(age // 3600)}h"
    return f"{int(age // 86400)}d"


class Sidebar(Static):
    """Who's here + channel list with pending counts."""

    def show(self, channels: Sequence[str], who: Sequence[Mapping], current: str) -> None:
        lines = ["CHANNELS"]
        for channel in channels:
            marker = "›" if channel == current else " "
            lines.append(f" {marker} {channel}")
        lines.append("")
        lines.append("WHO'S HERE")
        for row in who:
            marker = "›" if row["name"] == current else " "
            pending = f" ({row['pending']})" if row["pending"] else ""
            task = f" — {row['task']}" if row["task"] else ""
            lines.append(
                f" {marker} {row['name']}{pending} [{row['status']},"
                f" {_fmt_age(row['last_seen'])}]{task}"
            )
        self.update("\n".join(lines))


class ChatView(Static):
    """History of the current view (channel or DM)."""

    @staticmethod
    def _field(message: object, key: str) -> str:
        if isinstance(message, Mapping):
            return str(message.get(key if key != "sender" else "from", ""))
        return str(getattr(message, key, ""))

    def show_messages(self, title: str, messages: Sequence[Mapping]) -> None:
        lines = [f"── {title} " + "─" * max(0, 60 - len(title))]
        if not messages:
            lines.append("(no messages yet — say hi)")
        for message in messages:
            sender = self._field(message, "sender")
            target = self._field(message, "target")
            text = self._field(message, "text")
            lines.append(f"[{sender} → {target}] {text}")
        self.update("\n".join(lines))


class CommsApp(App[None]):
    """One wire, one chat. Views: #all, tag channels, DMs."""

    CSS = """
    Screen { layout: vertical; }
    #panes { height: 1fr; }
    #left { width: 40%; border: solid $accent; padding: 0 1; }
    #right { width: 60%; border: solid $accent; padding: 0 1; }
    #prompt { height: 3; border: none; }
    """
    BINDINGS = [
        Binding("n", "next_view", "Next view"),
        Binding("p", "prev_view", "Prev view"),
        Binding("r", "refresh", "Refresh"),
        Binding("f", "fork", "Fork"),
        Binding("q", "quit", "Quit"),
    ]
    AUTO_FOCUS = None

    def __init__(self, comms: Comms, thread_name: str | None = None):
        super().__init__()
        self._comms = comms
        self._me = thread_name
        self._current = GLOBAL_CHANNEL
        self._fork_mode = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="panes"):
            with Vertical(id="left"):
                yield Sidebar(id="sidebar")
            with Vertical(id="right"):
                yield ChatView(id="inbox")
        yield Input(placeholder="", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        if self._me is None:
            prompt = self.query_one("#prompt", Input)
            prompt.placeholder = "log in: type your name and press enter"
        self.refresh_data()

    # ─── Views ────────────────────────────────────────────────────────────────

    def _views(self) -> list[str]:
        channels = list(self._comms.channels())
        dms = sorted(self._comms.registry.active_threads())
        if self._me and self._me in dms:
            dms.remove(self._me)
        return channels + [f"@{name}" for name in dms]

    def _current_target(self) -> str:
        """Resolve the current view to a send target."""
        if self._current.startswith("@"):
            return self._current[1:]
        return self._current

    def refresh_data(self) -> None:
        if self._me:
            with contextlib.suppress(Exception):
                self._comms.heartbeat(self._me)
        self.query_one("#sidebar", Sidebar).show(
            self._comms.channels(), self._comms.who(), self._current
        )
        title = self._current
        if self._current.startswith("@"):
            peer = self._current[1:]
            messages = []
            if self._me:
                messages = [m.to_wire() for m in self._comms.dm_history(self._me, peer)]
            else:
                messages = []
        else:
            messages = [m.to_wire() for m in self._comms.channel_history(self._current)]
        self.query_one("#inbox", ChatView).show_messages(title, messages)
        placeholder = self._current
        if self._me:
            placeholder = f"→ {self._current}"
        self.query_one("#prompt", Input).placeholder = placeholder

    def _cycle(self, direction: int) -> None:
        views = self._views()
        if not views:
            return
        try:
            index = views.index(self._current)
        except ValueError:
            index = 0
        self._current = views[(index + direction) % len(views)]
        self.refresh_data()

    def action_next_view(self) -> None:
        self._cycle(1)

    def action_prev_view(self) -> None:
        self._cycle(-1)

    def action_refresh(self) -> None:
        self.refresh_data()

    def action_fork(self) -> None:
        self._fork_mode = True
        prompt = self.query_one("#prompt", Input)
        prompt.placeholder = "fork: name parent task  (esc to cancel)"
        prompt.focus()

    # ─── Input ────────────────────────────────────────────────────────────────

    def on_key(self, event: events.Key) -> None:  # noqa: N802 - Textual API
        if event.key == "escape" and self._fork_mode:
            self._fork_mode = False
            self.refresh_data()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = self.query_one("#prompt", Input)
        value = event.value.strip()
        prompt.value = ""
        if not value:
            return

        if self._me is None:
            # Login: become a thread with the human tag.
            from .declarations import Thread

            name = value.replace(" ", "-")
            thread = Thread(name=name, tags=frozenset({"human"}), worktree=str(Path.cwd()))
            self._comms.register(thread)
            self._me = name
            self._current = GLOBAL_CHANNEL
            self.refresh_data()
            return

        if self._fork_mode:
            self._fork_mode = False
            parts = value.split(maxsplit=2)
            if len(parts) != 3:
                self.query_one("#inbox", ChatView).show_messages(
                    "error",
                    [{"sender": "system", "target": "-", "text": "fork expects: name parent task"}],
                )
                return
            from .operations import ForkSpec

            name, parent, task = parts
            try:
                self._comms.fork(ForkSpec(name=name, parent=parent, task=task))
            except Exception as exc:
                self.query_one("#inbox", ChatView).show_messages(
                    "error", [{"sender": "system", "target": "-", "text": str(exc)}]
                )
            self.refresh_data()
            return

        target = self._current_target()
        try:
            self._comms.send(self._me, target, value)
        except Exception as exc:
            self.query_one("#inbox", ChatView).show_messages(
                "error", [{"sender": "system", "target": "-", "text": f"send failed: {exc}"}]
            )
        self.refresh_data()


def run(root: Path | str | None = None, thread_name: str | None = None) -> None:
    comms = wire(root)
    CommsApp(comms, thread_name=thread_name).run()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="agent-comms-tui", description="IRC-style chat for agents plus humans."
    )
    parser.add_argument("--root", default=None, help="Comms root directory")
    parser.add_argument(
        "--thread",
        default=None,
        help="Join as this thread (omit to log in interactively)",
    )
    args = parser.parse_args()
    run(args.root, thread_name=args.thread)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
