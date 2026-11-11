"""Which built runtimes this machine still needs, and the removal of the rest.

An installation builds an immutable runtime for each (Core commit, resolved lock) pair
and nothing ever retired one. On 2026-11-08 this machine held 129 of them, 16 GB, every
one built in the preceding six days, while a single one was running; the install that
found it refused to start its teammates for want of 10 GiB free, and 14 GB came back from
one hand-audited sweep. A sweep is not a fix: at two and a half gigabytes a day the pile
is back inside a week.

The builds are reproducible from the commit and lock their names carry. What is not
reproducible is a runtime something still points at, so this lists the holders and takes
what is left. It is a listing of holders rather than a rule about age on purpose: a
runtime built an hour ago that nothing holds is reclaimable, and one built last week that
a bus root is attached to is not.

A PATH entry is not a holder. codex-latency, tracing the same pile, found one process
carrying 47 historical runtime `bin` directories in its inherited PATH while exactly one
of them resolved its executables; counting those would have preserved the whole leak.
What counts is the interpreter a process was actually invoked with, which is what
`RuntimeProcesses` reads.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.root_runtime import RootRuntimeStore
from agent_comms.execution.runtime_processes import RuntimeProcesses
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.building_runtime import BuildingRuntime
from agent_comms.installer.runtime_resolution import RuntimeResolution
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import MalformedRecord


class RecordedPointer(DeclaredFamily, ABC, affix="Pointer"):
    """What a stored `.root-runtime.json` says about the runtime it would publish.

    Three answers, because two of them are not the same: a root that never published
    names no runtime, and a record that will not decode names one this cannot read. The
    first holds nothing; the second must stop the sweep, or a corrupt backup silently
    loses its only hold on the runtime it would restore.
    """

    @abstractmethod
    def held(self) -> frozenset[Path]:
        """The runtime this pointer keeps."""

    @abstractmethod
    def unread(self) -> tuple[str, ...]:
        """Why this pointer left a runtime undecided."""

    @staticmethod
    def read_at(directory: Path) -> RecordedPointer:
        try:
            return NamedPointer(RootRuntimeStore.at(directory).read().prefix)
        except FileNotFoundError:
            return AbsentPointer()
        except (OSError, MalformedRecord) as error:
            return UnreadablePointer(directory, str(error))


@dataclass(frozen=True)
class NamedPointer(RecordedPointer):
    prefix: Path

    def held(self) -> frozenset[Path]:
        return frozenset((self.prefix,))

    def unread(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class AbsentPointer(RecordedPointer):
    """No record at all, which is a complete answer: nothing was published here."""

    def held(self) -> frozenset[Path]:
        return frozenset()

    def unread(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class UnreadablePointer(RecordedPointer):
    """A record that is present and will not be read. Unknown is not absence."""

    path: Path
    reason: str

    def held(self) -> frozenset[Path]:
        return frozenset()

    def unread(self) -> tuple[str, ...]:
        return (f"Unreadable runtime pointer under {self.path}: {self.reason}",)


@dataclass(frozen=True)
class RuntimeRetention:
    """The built runtimes under one runtimes directory, and which of them are held."""

    runtimes: Path
    held: frozenset[Path]
    unread: tuple[str, ...]
    """Processes the scan could not read. A process table this cannot read is not one it
    can prune against, so any of these refuses the whole sweep rather than risking the
    runtime behind them."""

    @classmethod
    def after_install(cls, staged: LaunchRuntime, installed: LaunchRuntime,
                      processes: RuntimeProcesses, root: Path,
                      spill_dir: Path) -> RuntimeRetention:
        """What the install that just published ``staged`` still needs.

        The outgoing runtime is held by name rather than found by the scan: the installer
        is running on it, and the scan skips its own lineage, so nothing else would say so.
        """
        runtimes = staged.prefix.parent
        pointers = cls.pointers(root, spill_dir)
        named = (staged.prefix, installed.prefix, LaunchRuntime.running().prefix,
                 *processes.held_paths(),
                 *(prefix for pointer in pointers for prefix in pointer.held()))
        held = frozenset().union(*(cls.owning(runtimes, path) for path in named), frozenset())
        unread = (*processes.unread(),
                  *(line for pointer in pointers for line in pointer.unread()))
        return cls(runtimes, held, unread)

    @staticmethod
    def pointers(root: Path, spill_dir: Path) -> tuple[RecordedPointer, ...]:
        """Every stored record of a runtime this root has published: its own, and one per
        retained store backup.

        A backup carries the root's `.root-runtime.json`, because `RootRuntimeStore` is
        `RollbackRetained`; restoring one re-attaches the root to whichever runtime was
        published when it was taken. Three such backups were on this machine when the pile
        was found, reaching two runtimes older than the outgoing one, so current and
        outgoing alone are not the rollback holders.
        """
        return (RecordedPointer.read_at(root),
                *(RecordedPointer.read_at(backup.data_root)
                  for backup in StoreBackup.retained(root, spill_dir)))

    @staticmethod
    def owning(runtimes: Path, path: Path) -> frozenset[Path]:
        """The built runtime a path belongs to: the one directory under ``runtimes`` that
        contains it, and nothing when the path is somewhere else entirely.

        Not the executable's grandparent, which is what `LaunchRuntime.prefix` answers. A
        runtime bundles native children several directories down — codex-latency's audit
        named `share/toad/neovim/bin/nvim` — and their grandparent is a directory inside
        the build rather than the build. Held by that name, the enclosing runtime would
        have gone while a process was still running out of it.
        """
        relative = path.relative_to(runtimes) if path.is_relative_to(runtimes) else None
        return frozenset((runtimes / relative.parts[0],)) if relative and relative.parts \
            else frozenset()

    def built(self) -> tuple[Path, ...]:
        """Every prefix under the runtimes directory, newest name last."""
        if not self.runtimes.is_dir():
            return ()
        return tuple(sorted(entry for entry in self.runtimes.iterdir() if entry.is_dir()))

    def reclaimable(self) -> tuple[Path, ...]:
        """The completed builds no holder names."""
        return tuple(prefix for prefix in self.built()
                     if prefix not in self.held
                     and RuntimeResolution.recorded(prefix).reclaimable())

    def retire(self) -> RetirementOutcome:
        """Remove them, keeping the ones a read failure left undecided.

        A removal that fails is reported and not raised: an installation that published,
        verified and restarted its services has succeeded, and a directory it could not
        take back does not undo that.
        """
        if self.unread:
            return RefusedRetirement(self.unread)
        taken: list[Path] = []
        failures: list[str] = []
        for prefix in self.reclaimable():
            try:
                BuildingRuntime.discard(prefix)
            except OSError as error:
                failures.append(f"{prefix.name}: {error}")
                continue
            taken.append(prefix)
        return SweptRetirement(tuple(taken), tuple(failures), len(self.held))


class RetirementOutcome(DeclaredFamily, ABC, affix="Retirement"):
    """What one install's retirement did, for its report."""

    @abstractmethod
    def line(self) -> str: ...


@dataclass(frozen=True)
class SweptRetirement(RetirementOutcome):
    """The sweep ran: what it took, what it could not take, and how many it held."""

    retired: tuple[Path, ...]
    failures: tuple[str, ...]
    held: int

    def line(self) -> str:
        taken = ", ".join(prefix.name for prefix in self.retired) or "nothing"
        failed = f"; could not remove {'; '.join(self.failures)}" if self.failures else ""
        return f"Retired {len(self.retired)} built runtimes ({taken}); {self.held} held{failed}"


@dataclass(frozen=True)
class RefusedRetirement(RetirementOutcome):
    """The process scan left runtimes undecided, so none were taken."""

    unread: tuple[str, ...]

    def line(self) -> str:
        return ("Retired no built runtimes: the process scan could not read "
                f"{'; '.join(self.unread)}")
