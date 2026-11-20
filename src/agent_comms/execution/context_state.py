"""A context language model's context, recorded in git; its cache, kept across threads.

Git holds the state. Each CLM conversation is one branch of a bare repository (Polystore
``GitBackend``): the system prompt, every turn of the live context as the harness last sent
it, the harness's budget state, and the identity of the cache that goes with it. A turn's
end is a commit, so the context has a history, a diff per edit, and a content id.

The cache is a local copy that is checked, never trusted. One llama-server slot serves every
CLM thread, so a thread that takes the slot from another used to re-read its whole context
(11 to 24 s for 7K to 13K tokens, 2026-11-20). When a thread takes the slot, the previous
holder's slot state is saved and named by what it was computed from: the server's model and
build, and the content id of that conversation's recorded context. A thread's saved state is
restored only when both still match exactly; otherwise it is discarded and the context is
read again. The server itself reuses a restored state only for the token prefix it shares
with the next request, so a wrong restore can cost time and never an answer.
"""

from __future__ import annotations

import fcntl
import json
import logging
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from polystore.exceptions import StorageResolutionError
from polystore.git import GitBackend

from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.launch_runtime import RestartEnvironment
from agent_comms.execution.native_arguments import ClmLocalProvider, TurnHost

logger = logging.getLogger(__name__)

# clm_reference.mjs publishes the live view it sent to this file in the session's directory.
LIVE_CONTEXT_ROOT = Path("/tmp/.live_ctx")
VIEW_NAME = "LIVE_VIEW.json"
CONTEXT_DIRECTORY = "ctx"
CACHE_MANIFEST = "cache/slot.json"


class SlotServerUnavailableError(Exception):
    """The llama-server did not answer a slot request."""


@dataclass(frozen=True)
class SlotServer:
    """The llama-server's one slot, saved to and restored from files in its own save path."""

    base: str
    seconds: float = 120.0

    def _call(self, path: str, payload: dict[str, str] | None = None) -> dict:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.seconds) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise SlotServerUnavailableError(f"{path}: {error}") from error

    def identity(self) -> str:
        """What a saved state is only valid for: this model file under this build."""
        properties = self._call("/props")
        return f"{properties['model_path']}@{properties['build_info']}"

    def save(self, filename: str) -> int:
        """Save the slot; the number of tokens whose state was written."""
        return int(self._call("/slots/0?action=save", {"filename": filename})["n_saved"])

    def restore(self, filename: str) -> int:
        return int(self._call("/slots/0?action=restore", {"filename": filename})["n_restored"])

    @classmethod
    def configured(cls, agent_directory: Path, provider: str) -> SlotServer:
        """The server Pi's own model configuration names for ``provider``."""
        models = json.loads((agent_directory.expanduser() / "models.json").read_text())
        return cls(models["providers"][provider]["baseUrl"].removesuffix("/v1"))


@dataclass(frozen=True)
class ContextHistory:
    """One conversation's branch: its recorded context and its cache's identity."""

    repository: Path
    session: str

    def branch(self) -> GitBackend:
        return GitBackend(self.repository, f"refs/clm/{self.session}")

    def record(self) -> str | None:
        """Commit the live view the harness last published; the context's content id, or
        ``None`` when this conversation has published nothing."""
        published = LIVE_CONTEXT_ROOT / self.session / VIEW_NAME
        if not published.is_file():
            return None
        view = json.loads(published.read_text(encoding="utf-8"))
        branch = self.branch()
        turns = {
            f"{CONTEXT_DIRECTORY}/{number:04d}-{turn['role']}.txt": turn["text"]
            for number, turn in enumerate(view["turns"], start=1)
        }
        if branch.exists(CONTEXT_DIRECTORY):
            for stale in set(branch.list_files(CONTEXT_DIRECTORY)) - set(turns):
                branch.delete(stale)
        branch.save_batch(
            [view["system"], json.dumps(view["budget"], sort_keys=True), *turns.values()],
            ["system.txt", "harness.json", *turns],
        )
        branch.checkpoint(f"context: {len(turns)} turns")
        return branch.tree_id(CONTEXT_DIRECTORY) if turns else None

    def context_id(self, branch: GitBackend) -> str | None:
        return branch.tree_id(CONTEXT_DIRECTORY) if branch.exists(CONTEXT_DIRECTORY) else None


class SlotHandoff:
    """What happened to the slot when a conversation took it; each member says it."""


@dataclass(frozen=True)
class KeptSlot(SlotHandoff):
    def __str__(self) -> str:
        return "this conversation already held the slot"


@dataclass(frozen=True)
class RestoredSlot(SlotHandoff):
    tokens: int

    def __str__(self) -> str:
        return f"restored {self.tokens} tokens of saved state"


@dataclass(frozen=True)
class ColdSlot(SlotHandoff):
    reason: str

    def __str__(self) -> str:
        return f"no state restored: {self.reason}"


@dataclass(frozen=True)
class UnavailableSlot(SlotHandoff):
    reason: str

    def __str__(self) -> str:
        return f"slot server unavailable, nothing saved or restored: {self.reason}"


@dataclass(frozen=True)
class ContextStateStore:
    """Hands the one slot from thread to thread, saving and restoring its state."""

    directory: Path
    server: SlotServer
    # Where the server writes slot files (its --slot-save-path), and how much of them to keep:
    # a state is about 65 KB per token (955 MB for 14,648 tokens, measured 2026-11-20).
    slots: Path
    budget_bytes: int = 16 * 1024**3

    @property
    def repository(self) -> Path:
        return self.directory / "context.git"

    def history(self, session: str) -> ContextHistory:
        return ContextHistory(self.repository, session)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """One handoff at a time: reading the holder, saving, restoring and recording the
        new holder are one step for every worker process sharing the slot."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "slot.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @property
    def _holder_file(self) -> Path:
        return self.directory / "holder"

    def holder(self) -> str | None:
        """The conversation whose state the slot holds, as far as this store handed it."""
        try:
            return self._holder_file.read_text().strip() or None
        except FileNotFoundError:
            return None

    def _hold(self, session: str | None) -> None:
        self._holder_file.write_text(session or "")

    def enter(self, session: str) -> SlotHandoff:
        with self._locked():
            holder = self.holder()
            if holder == session:
                return KeptSlot()
            try:
                identity = self.server.identity()
                if holder is not None:
                    self._save(holder, identity)
                outcome = self._restore(session, identity)
            except SlotServerUnavailableError as error:
                # Nothing is assumed about a slot that could not be asked.
                self._hold(None)
                return UnavailableSlot(str(error))
            self._hold(session)
            return outcome

    def _save(self, holder: str, identity: str) -> None:
        history = self.history(holder)
        branch = history.branch()
        context = history.context_id(branch)
        if context is None:
            return  # nothing recorded to name the state by, so nothing worth keeping
        tokens = self.server.save(f"{holder}.bin")
        if tokens == 0:
            # An empty slot (a restarted server) is not this conversation's state, and the
            # file it just overwrote no longer is either.
            if branch.exists(CACHE_MANIFEST):
                branch.delete(CACHE_MANIFEST)
                branch.checkpoint("slot state dropped: the slot was empty")
            return
        branch.save(
            json.dumps({"server": identity, "context": context, "tokens": tokens}, sort_keys=True),
            CACHE_MANIFEST,
        )
        branch.checkpoint(f"slot state saved: {tokens} tokens")
        self._prune()

    def _prune(self) -> None:
        """Delete the least recently saved states until the rest fit the budget. A deleted
        state costs its conversation one re-read of its context."""
        states = sorted(self.slots.glob("*.bin"), key=lambda state: state.stat().st_mtime)
        excess = sum(state.stat().st_size for state in states) - self.budget_bytes
        for state in states[:-1]:
            if excess <= 0:
                break
            excess -= state.stat().st_size
            state.unlink()

    def _restore(self, session: str, identity: str) -> SlotHandoff:
        history = self.history(session)
        branch = history.branch()
        try:
            manifest = json.loads(branch.load_text(CACHE_MANIFEST))
        except FileNotFoundError:
            return ColdSlot("this conversation has no saved state")
        if manifest["server"] != identity:
            return ColdSlot("its state was saved under another model or server build")
        if manifest["context"] != history.context_id(branch):
            return ColdSlot("its context changed after the state was saved")
        if not (self.slots / f"{session}.bin").is_file():
            return ColdSlot("its saved state was deleted to fit the disk budget")
        return RestoredSlot(self.server.restore(f"{session}.bin"))


class LlamaSlotHost(TurnHost):
    """This machine, running a turn against a local llama-server."""

    @asynccontextmanager
    async def slot_turn(
        self, provider: type[ClmLocalProvider], session_file: str | Path | None,
    ) -> AsyncIterator[None]:
        session = conversation(session_file)
        if session is None:
            # A thread that has saved nothing yet has no conversation to name its state by.
            yield
            return
        store = ContextStateStore(
            provider.state_directory.expanduser(),
            SlotServer.configured(RestartEnvironment.default_agent_directory, provider.declared_name),
            provider.slot_directory.expanduser(),
        )
        outcome = await Coordination.run_worker(partial(recoverable, store.enter, session))
        logger.info("CLM slot for %s: %s", session, outcome)
        try:
            yield
        finally:
            await Coordination.run_worker(partial(recoverable, store.history(session).record))


def conversation(session_file: str | Path | None) -> str | None:
    """The Pi session id a saved transcript names in its header, or ``None`` before one
    exists. The harness names its live-context directory by the same id."""
    if session_file is None or not Path(session_file).is_file():
        return None
    with Path(session_file).open(encoding="utf-8") as transcript:
        header = json.loads(transcript.readline() or "{}")
    return header.get("id") if header.get("type") == "session" else None


def recoverable(action, *arguments) -> object | None:
    """Run a recording step whose failure must not fail the turn it serves, and say so."""
    try:
        return action(*arguments)
    except (StorageResolutionError, OSError, json.JSONDecodeError, KeyError) as error:
        logger.warning("Context state step %s failed: %s", action.__qualname__, error)
        return None
