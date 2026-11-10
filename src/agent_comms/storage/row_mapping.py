"""Editable row mappings whose revisions share immutable roots.

A copy starts a new edit over the same root. A frozen view retains that root and
its changed-key evidence; neither operation walks unchanged rows. Iteration keeps
insertion order, while comparing related revisions examines their writes alone.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import NoReturn

from immutables import Map


@dataclass(frozen=True, slots=True)
class RowSlot[Value]:
    value: Value
    position: int

    def setting(self, value: Value) -> RowSlot[Value]:
        return replace(self, value=value)


class RowMapping[Key: Hashable, Value](MutableMapping[Key, Value]):
    """One editable revision, with changes recorded where the writes happen."""

    def __init__(self, values: Mapping[Key, Value] = MappingProxyType({})) -> None:
        self._root: Map[Key, RowSlot[Value]] = Map(
            (key, RowSlot(value, position))
            for position, (key, value) in enumerate(values.items())
        )
        self._base = self._root
        self._changed: Map[Key, None] = Map()
        self._next_position = len(values)

    @classmethod
    def owning(cls, values: Mapping[Key, Value]) -> RowMapping[Key, Value]:
        """Admit a plain mapping once; an owned revision already carries its evidence."""
        if isinstance(values, RowMapping):
            return values
        return cls(values)

    def __getitem__(self, key: Key) -> Value:
        return self._root[key].value

    def __contains__(self, key: object) -> bool:
        return key in self._root

    def __len__(self) -> int:
        return len(self._root)

    def __iter__(self) -> Iterator[Key]:
        return (key for key, slot in sorted(
            self._root.items(), key=lambda entry: entry[1].position,
        ))

    def __setitem__(self, key: Key, value: Value) -> None:
        if key in self._root:
            slot = self._root[key].setting(value)
        else:
            slot = RowSlot(value, self._next_position)
            self._next_position += 1
        self._root = self._root.set(key, slot)
        self._changed = self._changed.set(key, None)

    def __delitem__(self, key: Key) -> None:
        self._root = self._root.delete(key)
        self._changed = self._changed.set(key, None)

    def clear(self) -> None:
        self._changed = self._changed.update((key, None) for key in self._root)
        self._root = Map()
        self._next_position = 0

    def any_key(self) -> Key:
        """Select one key without sorting or enumerating the collection."""
        try:
            return next(iter(self._root))
        except StopIteration:
            raise KeyError("mapping is empty") from None

    def popitem(self) -> tuple[Key, Value]:
        key = self.any_key()
        value = self[key]
        del self[key]
        return key, value

    def _copy_into(self, following: RowMapping[Key, Value]) -> RowMapping[Key, Value]:
        following._root = self._root
        following._base = self._base
        following._changed = self._changed
        following._next_position = self._next_position
        return following

    def copy(self) -> RowMapping[Key, Value]:
        following = self._copy_into(RowMapping())
        following._base = self._root
        following._changed = Map()
        return following

    def freeze(self) -> RowMapping[Key, Value]:
        return self._copy_into(FrozenRowMapping())

    def only(self, names: Iterable[Key]) -> RowMapping[Key, Value]:
        """Select keys directly, retaining their relative insertion order."""
        selected = sorted(
            (name for name in names if name in self._root),
            key=lambda name: self._root[name].position,
        )
        return RowMapping({name: self[name] for name in selected})

    def differing(self, earlier: RowMapping[Key, Value]) -> frozenset[Key]:
        """Direct or sibling edits compare their changed keys, never the complete roots."""
        if self._root is earlier._root:
            return frozenset()
        candidates: Iterable[Key]
        if self._base is earlier._root:
            candidates = self._changed
        elif earlier._base is self._root:
            candidates = earlier._changed
        elif self._base is earlier._base:
            candidates = self._changed.update(earlier._changed)
        else:
            # Independently supplied mappings have no shared mutation history.
            candidates = set(self._root).union(earlier._root)
        return frozenset(
            key for key in candidates
            if (key in self) != (key in earlier)
            or (key in self and self[key] != earlier[key])
        )

    def changed_in_order(self, earlier: RowMapping[Key, Value]) -> tuple[Key, ...]:
        """Order only changed keys, preserving insertion order for surviving writes."""
        return tuple(sorted(
            self.differing(earlier),
            key=lambda key: (self._root[key] if key in self else earlier._root[key]).position,
        ))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RowMapping):
            return not self.differing(other)
        if not isinstance(other, Mapping):
            return NotImplemented
        return len(self) == len(other) and all(
            key in other and slot.value == other[key]
            for key, slot in self._root.items()
        )


class ReadOnlyRows[Key, Value]:
    """Read-only is one capability, shared by retained row and thread mappings."""

    @staticmethod
    def refuse() -> NoReturn:
        raise TypeError("snapshot rows are read-only")

    def __setitem__(self, key: Key, value: Value) -> None:
        self.refuse()

    def __delitem__(self, key: Key) -> None:
        self.refuse()

    def clear(self) -> None:
        self.refuse()

    def popitem(self) -> tuple[Key, Value]:
        self.refuse()


class FrozenRowMapping[Key: Hashable, Value](ReadOnlyRows[Key, Value], RowMapping[Key, Value]):
    """A retained root cannot be changed through its mapping interface."""
