"""The processes constructing a runtime prefix, recorded beside it.

An install syncs its runtime, replaces its own image with that runtime's interpreter, and
completes the build from there. No scope in one process spans those halves, so between
them nothing owned the prefix: an interruption left a synced, unrecorded directory that no
later install would touch, because the retirement rightly refuses to sweep a build that
might be in flight and had no way to tell in flight from abandoned. Three of them were on
this machine. A catch inside `complete` cannot reach that window — `complete` is in the
other half.

What does span it is the install's own process. `os.execve` keeps the pid and the kernel's
start time, so an identity written before the exec still names the install afterwards, and
lasts exactly as long as the construction does.

Two details are load-bearing, and both were found by reading rather than by a failure:

Beside the prefix, not in it. The claim must exist before `uv sync` writes the first
durable byte, and uv owns everything in the environment directory while it creates one, so
a claim inside it is a file whose lifetime this does not control.

One file per builder. A single claim file would be overwritten by a second install
building the same commit and lock, and then either one dying would condemn the other's
live build. Each builder writes its own name and removes only what it wrote, so there is
no read-modify-write to lose and no claim to steal: a prefix is abandoned when every
builder that claimed it is gone.

A prefix with no claim at all is left alone. It predates this and is unknown, which is not
the same as abandoned.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.execution.child_process import IdentityMismatchError, ProcessIdentity
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord


@dataclass(frozen=True)
class BuildingRuntime:
    """One builder's claim on one prefix."""

    builder: ProcessIdentity

    infix: ClassVar[str] = ".building-"
    suffix: ClassVar[str] = ".json"

    @classmethod
    def path_in(cls, prefix: Path, builder: ProcessIdentity) -> Path:
        return prefix.parent / f"{prefix.name}{cls.infix}{builder.pid}-{builder.start_time}{cls.suffix}"

    @classmethod
    def claims_on(cls, prefix: Path) -> Iterator[Path]:
        """Every claim file written for ``prefix``, by any builder."""
        return prefix.parent.glob(f"{prefix.name}{cls.infix}*{cls.suffix}")

    @classmethod
    def claim(cls, prefix: Path) -> None:
        """Record this process as a builder of ``prefix``, before it has any output."""
        record = cls(ProcessIdentity.capture(os.getpid()))
        path = cls.path_in(prefix, record.builder)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(FieldCodec.compact(FieldCodec.encode(record)) + "\n")
        path.chmod(0o600)

    @classmethod
    def release(cls, prefix: Path) -> None:
        """Forget every builder: the prefix is finished or gone, and nobody is building it."""
        for path in cls.claims_on(prefix):
            path.unlink(missing_ok=True)

    @classmethod
    def discard(cls, prefix: Path) -> None:
        """Remove a build and the claims on it, which go together.

        Three places take a prefix back — a sync that failed, a completion that failed, and
        the retirement of one nobody is building — and each of them would otherwise have
        had to remember the claims separately. A claim outliving what it describes is a
        file naming a process about a directory that is not there.
        """
        shutil.rmtree(prefix)
        cls.release(prefix)

    @classmethod
    def abandoned(cls, prefix: Path) -> bool:
        """Whether every install that claimed this prefix has gone without completing it.

        A claim that will not read leaves the prefix alone: an unreadable claim is not a
        dead one, and this decides a removal.
        """
        claimed = False
        for path in cls.claims_on(prefix):
            claimed = True
            try:
                record = FieldCodec.decode(cls, FieldCodec.parse(path.read_text()))
                record.builder.require_alive()
            except (ProcessLookupError, IdentityMismatchError):
                continue
            except (OSError, MalformedRecord):
                return False
            return False
        return claimed
