"""A context language model's context, recorded in git.

Each conversation is one branch of a bare repository (Polystore ``GitBackend``): the system
prompt, every turn of the context as it was last sent, and the budget's state. A turn's end
is a commit, so the context has a history, a diff per edit, and a content id. Whoever holds
the context says what it is (``ContextView``); a served thread's is the view its harness
publishes, a practice attempt's is its own document.

What a turn showed before any cut is its original, and it is an array, not text: Polystore's
Zarr backend keeps it once under its content id beside the repository, and the commit's
``originals.json`` says which original each recorded turn had. A view that cut or dropped
the turn leaves the original recoverable from the commit that recorded it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.execution.context_sizes import ContextSizes

if TYPE_CHECKING:
    from polystore.git import GitBackend
    from polystore.zarr import ZarrStorageBackend

CONTEXT_DIRECTORY = "ctx"
ORIGINALS_INDEX = "originals.json"


@dataclass(frozen=True)
class ViewTurn:
    role: str
    text: str
    #: The bytes the turn arrived as, before any cut (a whole tool result, a screen, an image).
    #: ``text`` is what was sent; ``None`` when nothing was cut.
    original: bytes | None = None


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

    @property
    def originals_store(self) -> Path:
        """The Zarr store beside the repository that keeps this host's uncut originals."""
        return self.repository.with_suffix(".zarr")

    @staticmethod
    def content_id(original: bytes) -> str:
        """The id an original is kept under, here and wherever else it is named: the SHA-256
        of its bytes."""
        return hashlib.sha256(original).hexdigest()

    def retain(self, original: bytes) -> str:
        """Keep ``original`` as an array under its content id, once, and answer the id. The
        Zarr backend refuses a second write of a key, so an original already kept is not
        written again."""
        # Zarr and numpy come with Polystore, imported where an original is kept, like the branch.
        import numpy
        from polystore.zarr import ZarrStorageBackend

        digest = self.content_id(original)
        store, path = ZarrStorageBackend(), self.originals_store / f"{digest}.bin"
        if not store.exists(path):
            store.save(numpy.frombuffer(original, dtype=numpy.uint8), path)
        return digest

    def original(self, turn: str, ref: str | None = None) -> bytes | None:
        """What ``turn`` (its ``ctx/`` file) showed before any cut, as of the commit ``ref``
        (the newest by default). ``None`` for a turn that was recorded uncut."""
        try:
            digest = json.loads(self.branch().load_text(ORIGINALS_INDEX, ref=ref)).get(turn)
        except (FileNotFoundError, ValueError):
            return None
        if digest is None:
            return None
        from polystore.zarr import ZarrStorageBackend

        return bytes(ZarrStorageBackend().load(self.originals_store / f"{digest}.bin"))

    def commit(self, view: ContextView) -> str | None:
        """Commit ``view`` as this conversation's context. Answers the content id of its
        turns, or ``None`` for a context with none. That id is the turns alone; the commit is
        everything that was sent, the system text and the budget's state with them."""
        branch = self.branch()
        turns = {
            f"{CONTEXT_DIRECTORY}/{number:04d}-{turn.role}.txt": turn.text
            for number, turn in enumerate(view.turns, start=1)
        }
        originals = {
            name: self.retain(turn.original)
            for name, turn in zip(turns, view.turns, strict=True)
            if turn.original
        }
        # The turns are staged afresh as one batch, so a context that lost turns costs the same
        # few git processes as one that gained one; git stores each unchanged turn once.
        if branch.exists(CONTEXT_DIRECTORY):
            branch.delete_all(CONTEXT_DIRECTORY)
        branch.save_batch(
            [view.system, json.dumps(view.budget, sort_keys=True),
             json.dumps(originals, sort_keys=True), *turns.values()],
            ["system.txt", "harness.json", ORIGINALS_INDEX, *turns],
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


def recorded_tokens(branch: GitBackend, ref: str | None = None) -> int | None:
    """The context size the harness stated when one view was recorded. A view that predates
    the field, and a conversation that published none, state no size."""
    try:
        return json.loads(branch.load_text("harness.json", ref=ref)).get("tokens")
    except (FileNotFoundError, ValueError):
        return None
