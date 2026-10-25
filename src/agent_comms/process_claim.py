"""One running process per path, held by the kernel.

The standard library only: Claude Code hooks probe these claims on every tool call, and
the store machinery's imports cost a hook more than the probe it needs.
"""

from __future__ import annotations

import errno
import fcntl
import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


class FlockMode(ABC):
    """Whether a flock lets other holders in."""

    @abstractmethod
    def flock(self) -> int:
        """This mode's POSIX flock operation."""

    def attempt(self, descriptor: int) -> None:
        """Hold this mode on the open description now, or raise BlockingIOError."""
        fcntl.flock(descriptor, self.flock() | fcntl.LOCK_NB)

    def wait(self, descriptor: int) -> None:
        """Hold this mode on the open description, waiting in the kernel while it is held."""
        fcntl.flock(descriptor, self.flock())


class SharedFlock(FlockMode):
    def flock(self) -> int:
        return fcntl.LOCK_SH


class ExclusiveFlock(FlockMode):
    def flock(self) -> int:
        return fcntl.LOCK_EX


class ClaimHeldError(BlockingIOError):
    """Another open description holds the claim, even within the same process."""

    def __init__(self, path: Path) -> None:
        super().__init__(errno.EAGAIN, f"{path} is already held")
        self.path = path


@dataclass(frozen=True)
class ObservedClaim:
    """A claim file read without taking the claim, and whether a live process holds it."""

    file: TextIO
    held: bool


@dataclass(frozen=True)
class ProcessSingleton:
    """A claim file at most one process holds at a time. The holder's open description of the
    file is the claim: the kernel releases it when the holder closes the file or exits, and a
    descriptor passed through exec keeps it held. A claim's holder never opens, locks or
    unlocks the file itself; it claims, waits for, adopts or observes it here."""

    path: Path

    def descriptor(self) -> int:
        """A private open description of the claim file, created on first use."""
        return os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                       0o600)

    def claim(self, descriptor: int) -> None:
        try:
            ExclusiveFlock().attempt(descriptor)
        except BlockingIOError:
            raise ClaimHeldError(self.path) from None

    @staticmethod
    def wait(descriptor: int) -> None:
        """Hold the claim on an open description once its current holder lets it go; a caller
        that must not block its loop runs this on a worker of its own."""
        ExclusiveFlock().wait(descriptor)

    @contextmanager
    def claimed(self) -> Iterator[TextIO]:
        """Hold the claim, or raise ClaimHeldError at once."""
        with open(self.descriptor(), "a+") as held:
            self.claim(held.fileno())
            yield held

    @contextmanager
    def waited(self) -> Iterator[TextIO]:
        """Hold the claim once its current holder lets it go."""
        with open(self.descriptor(), "a+") as held:
            self.wait(held.fileno())
            yield held

    @contextmanager
    def adopted(self, descriptor: int) -> Iterator[TextIO]:
        """Take over the description a replaced holder kept through exec. The kernel already
        counts that description as holding the claim, so this cannot wait. The replaced holder
        may have opened it write-only, so it is adopted for appending only."""
        if not os.path.samestat(os.fstat(descriptor), os.stat(self.path)):
            raise ValueError(f"Inherited descriptor {descriptor} is not the claim {self.path}")
        os.set_inheritable(descriptor, False)
        with os.fdopen(descriptor, "a") as held:
            self.claim(held.fileno())
            yield held

    @contextmanager
    def observed(self) -> Iterator[ObservedClaim]:
        """Read an existing claim file; ``held`` is whether a live process holds the claim."""
        with self.path.open() as observed:
            try:
                SharedFlock().attempt(observed.fileno())
                held = False
            except BlockingIOError:
                held = True
            yield ObservedClaim(observed, held)

    @staticmethod
    def release(held: TextIO) -> None:
        """Let the claim go while its holder still has the file open."""
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)

    def held_elsewhere(self) -> bool:
        if not self.path.exists():
            return False
        with self.observed() as observed:
            return observed.held
