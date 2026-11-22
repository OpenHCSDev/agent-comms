"""A context language model's context, recorded in git.

Each conversation is one branch of a bare repository (Polystore ``GitBackend``): the system
prompt, every turn of the context as it was last sent, and the budget's state. A turn's end
is a commit, so the context has a history, a diff per edit, and a content id. Whoever holds
the context says what it is (``ContextView``); a served thread's is the view its harness
publishes, a practice attempt's is its own document.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.execution.context_sizes import ContextSizes

if TYPE_CHECKING:
    from polystore.git import GitBackend
    from polystore.git_stream import GitStreamBackend

CONTEXT_DIRECTORY = "ctx"


@dataclass(frozen=True)
class ViewTurn:
    role: str
    text: str


@dataclass(frozen=True)
class ContextView:
    """A context as it was last sent: what is protected, each turn, and the budget's state."""

    system: str
    budget: dict
    turns: tuple[ViewTurn, ...]

    @classmethod
    def published(cls, text: str) -> ContextView:
        """The view a harness wrote as JSON."""
        view = json.loads(text)
        return cls(view["system"], view["budget"],
                   tuple(ViewTurn(turn["role"], turn["text"]) for turn in view["turns"]))


@dataclass(frozen=True)
class ContextHistory:
    """One conversation's branch: its recorded context."""

    repository: Path
    session: str

    def branch(self) -> GitBackend:
        # Polystore is imported where a branch is opened, which only the host of a turn does:
        # every client imports this module to discover declarations (RootFile.load_declarations)
        # and the phone app does not ship Polystore.
        from polystore.git import GitBackend

        return GitBackend(self.repository, f"refs/clm/{self.session}")

    def commit(self, view: ContextView) -> str | None:
        """Commit ``view`` as this conversation's context. Answers the content id of its
        turns, or ``None`` for a context with none. That id is the turns alone; the commit is
        everything that was sent, the system text and the budget's state with them."""
        branch = self.branch()
        turns = {
            f"{CONTEXT_DIRECTORY}/{number:04d}-{turn.role}.txt": turn.text
            for number, turn in enumerate(view.turns, start=1)
        }
        # The turns are staged afresh as one batch, so a context that lost turns costs the same
        # few git processes as one that gained one; git stores each unchanged turn once.
        if branch.exists(CONTEXT_DIRECTORY):
            branch.delete_all(CONTEXT_DIRECTORY)
        branch.save_batch(
            [view.system, json.dumps(view.budget, sort_keys=True), *turns.values()],
            ["system.txt", "harness.json", *turns],
        )
        branch.checkpoint(f"context: {len(turns)} turns")
        return branch.tree_id(CONTEXT_DIRECTORY) if turns else None

    def sizes(self) -> ContextSizes:
        """The size the context had at the record before this turn and the size this turn
        recorded: its last two records are the whole comparison, so nothing is carried from
        the turn's end to the caller that reports it, and a turn that recorded nothing
        compares a commit with itself."""
        branch = self.branch()
        recorded = branch.history(limit=2)
        if len(recorded) < 2:
            return ContextSizes.unrecorded()
        after, before = (recorded_tokens(branch, ref=commit.id) for commit in recorded)
        return ContextSizes(before, after)

    def context_id(self, branch: GitBackend) -> str | None:
        return branch.tree_id(CONTEXT_DIRECTORY) if branch.exists(CONTEXT_DIRECTORY) else None


@dataclass(frozen=True)
class StreamedHistory(ContextHistory):
    """A conversation recorded through the repository's one open git, for a holder that
    records often and is read later: a record starts no process, and is on disk, where
    another process reads it, at ``flush``. The branch has this one writer."""

    @cached_property
    def stream(self) -> GitStreamBackend:
        from polystore.git_stream import GitStreamBackend

        return GitStreamBackend(self.repository, f"refs/clm/{self.session}")

    def branch(self) -> GitStreamBackend:
        return self.stream

    def flush(self) -> None:
        self.stream.flush()


def recorded_tokens(branch: GitBackend, ref: str | None = None) -> int | None:
    """The context size the harness stated when one view was recorded. A view that predates
    the field, and a conversation that published none, state no size."""
    try:
        return json.loads(branch.load_text("harness.json", ref=ref)).get("tokens")
    except (FileNotFoundError, ValueError):
        return None
