"""Human read positions for native transcripts, independent of inbox delivery."""

from __future__ import annotations

import json
from bisect import bisect_right
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from weakref import WeakValueDictionary

from .declarations import _atomic_write_text, _store_lock, file_revision


@dataclass
class ReplyIndex:
    revision: tuple[int, int, int, int] | None = None
    through: int = 0
    ends: list[int] = field(default_factory=list)


class TranscriptReadState:
    """Index completed reply records incrementally; persist only human cursors.

    A record counts once even if it has several text blocks. Thinking, tool-only
    records and human prompts do not produce unread replies. Source identity,
    rather than thread spelling, keeps read positions stable across renames.
    """

    def __init__(self, path: Path):
        self.path = path
        self._indices: dict[str, ReplyIndex] = {}
        self._lock = RLock()

    def _index(self, source: str, is_reply: Callable[[Mapping], bool]) -> ReplyIndex:
        path = Path(source)
        revision = file_revision(path) if source else None
        cached = self._indices.get(source, ReplyIndex())
        if revision is None:
            self._indices.pop(source, None)
            return ReplyIndex()
        if revision == cached.revision:
            return cached
        if (
            cached.revision is None
            or revision[0] != cached.revision[0]
            or revision[1] <= cached.revision[1]
        ):
            cached = ReplyIndex()
        with path.open("rb") as stream:
            stream.seek(cached.through)
            while stream.tell() < revision[1]:
                raw = stream.readline()
                if not raw.endswith(b"\n"):
                    break  # Never consume a writer's incomplete trailing record.
                cached.through = stream.tell()
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(record, dict) and is_reply(record):
                    cached.ends.append(cached.through)
        cached.revision = revision
        self._indices[source] = cached
        return cached

    @staticmethod
    def _key(viewer: str, source: str, inode: int) -> str:
        return json.dumps([viewer, str(Path(source).resolve()), inode])

    def _markers(self) -> dict[str, int]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def counts(
        self, viewer: str, sources: Mapping[str, str], is_reply: Callable[[Mapping], bool]
    ) -> dict[str, int]:
        with self._lock, _store_lock(self.path):
            markers = self._markers()
            result = {}
            for name, source in sources.items():
                try:
                    index = self._index(source, is_reply)
                except FileNotFoundError:
                    result[name] = 0
                    continue
                if index.revision is None:
                    result[name] = 0
                    continue
                seen = markers.get(self._key(viewer, source, index.revision[0]), 0)
                if seen > index.through:
                    seen = 0  # A truncated/rebuilt source is not the old conversation tail.
                result[name] = len(index.ends) - bisect_right(index.ends, seen)
            return result

    def mark_read(self, viewer: str, source: str, through: int) -> None:
        revision = file_revision(Path(source)) if source else None
        if revision is None or not 0 <= through <= revision[1]:
            raise ValueError("Transcript changed; refresh before marking it read.")
        with _store_lock(self.path):
            markers = self._markers()
            key = self._key(viewer, source, revision[0])
            previous = markers.get(key, 0)
            if previous > revision[1] or previous < through:
                markers[key] = through
                _atomic_write_text(self.path, json.dumps(markers))


_shared_lock = RLock()
_shared_states: WeakValueDictionary[str, TranscriptReadState] = WeakValueDictionary()


def transcript_read_state(path: Path) -> TranscriptReadState:
    """Reuse one source index per live wire root, without retaining dead roots."""
    key = str(path.expanduser().resolve())
    with _shared_lock:
        state = _shared_states.get(key)
        if state is None:
            state = TranscriptReadState(Path(key))
            _shared_states[key] = state
        return state
