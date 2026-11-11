"""Report old-runtime applications without acquiring authority to stop them."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.execution.child_process import IdentityMismatchError, Platform, ProcessIdentity
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.nominal.declared_family import DeclaredFamily


class RuntimeReference(DeclaredFamily, ABC, affix="Reference"):
    """What one path a process names says about the build it is using.

    A command and an import root ask the same question of a path, and asking it in two
    places is how the same ambiguity gets forgotten once: the command learned that
    `current` names no particular build while the environment still resolved it to
    whatever the pointer happens to say now. codex-latency found that by pointing a live
    importer at `current/lib` after the command case was already closed. One relation,
    three answers, both callers.
    """

    @abstractmethod
    def held(self) -> frozenset[Path]:
        """What this reference keeps, for whoever retires the runtimes nothing uses.

        A path and not a runtime: a bundled native child lives several directories inside
        its build and an import root is a directory within one, so which build owns a path
        belongs to whoever holds the runtimes directory.
        """

    @abstractmethod
    def unread(self) -> tuple[str, ...]:
        """Why this reference leaves a build undecided."""

    @abstractmethod
    def describes(self) -> str:
        """This reference in one phrase, for the line its process reports."""

    def names_installation(self) -> bool:
        """Whether this is about the installation at all."""
        return True

    def hidden(self, identity: ProcessIdentity,
               error: Exception) -> tuple[RuntimeProcessOutcome, ...]:
        """What a process described by this reference contributes when its environment
        will not read.

        Inside the installation that is unknown and refuses the sweep: the process is
        ours and what else it imports could be any build. The policy belongs here rather
        than in the reader because the reference is what knows the difference.

        A live process that hides its environment cannot be read by anyone, its owner
        included — on this machine the user's own systemd, its sd-pam child and ssh-agent
        all do, permanently, by their own choice and not by ownership — so this cannot
        refuse for every one of them without being a collector that never runs.
        """
        try:
            identity.require_alive()
        except (ProcessLookupError, IdentityMismatchError):
            return ()
        return (UnreadableRuntimeProcess(identity.pid, str(error)),)

    @staticmethod
    def to(path: Path, runtimes: Path) -> RuntimeReference:
        """How ``path`` relates to the builds under ``runtimes``."""
        if path.is_relative_to(runtimes):
            return BuildReference(path)
        if path.is_relative_to(LaunchRuntime.pointer_in(runtimes.parent)):
            return PointerReference(path)
        return ForeignReference()


@dataclass(frozen=True)
class BuildReference(RuntimeReference):
    """A path inside one build, which therefore names it."""

    path: Path

    def held(self) -> frozenset[Path]:
        return frozenset((self.path,))

    def unread(self) -> tuple[str, ...]:
        return ()

    def describes(self) -> str:
        return str(self.path)


@dataclass(frozen=True)
class PointerReference(RuntimeReference):
    """A path through the installation's moving pointer, which names no build.

    `current` named whichever runtime was published when the process reached for it, and
    once it has moved nothing in the path says which that was. The process holds a build
    and will not say which, so none of them can be decided about.
    """

    path: Path

    def held(self) -> frozenset[Path]:
        return frozenset()

    def unread(self) -> tuple[str, ...]:
        return (f"{self.path} names the installation's pointer rather than a build",)

    def describes(self) -> str:
        return f"{self.path}, which names the installation's pointer rather than a build"


@dataclass(frozen=True)
class ForeignReference(RuntimeReference):
    """A path outside the installation, which holds none of its builds."""

    def held(self) -> frozenset[Path]:
        return frozenset()

    def unread(self) -> tuple[str, ...]:
        return ()

    def describes(self) -> str:
        return "a path outside the installation"

    def names_installation(self) -> bool:
        return False

    def hidden(self, identity: ProcessIdentity,
               error: Exception) -> tuple[RuntimeProcessOutcome, ...]:
        """Nothing. A process launched from outside the installation has already said
        what it runs, in a command every process publishes.

        The limit this leaves is exact: such a process, hiding its environment, cannot be
        shown to be importing from a build by this reader. It is uninspected, not proven
        to hold none.
        """
        return ()


class RuntimeProcessOutcome(DeclaredFamily, ABC, affix="RuntimeProcess"):
    @abstractmethod
    def line(self) -> str: ...

    @abstractmethod
    def held_paths(self) -> frozenset[Path]:
        """What this process uses from inside the installation, for whoever retires the
        runtimes nothing uses: what it was invoked with, or what it can import.

        Paths and not runtimes: a bundled native child lives several directories inside
        its runtime and an import root is a directory within one, so which build owns a
        path is a question for whoever holds the runtimes directory. Answering a prefix
        here would answer the wrong one.
        """

    @abstractmethod
    def unread(self) -> tuple[str, ...]:
        """What this outcome could not read, which leaves a runtime undecided."""


@dataclass(frozen=True)
class UnreadableRuntimeProcess(RuntimeProcessOutcome):
    pid: int
    reason: str

    def line(self) -> str:
        return f"Unknown runtime PID {self.pid}: {self.reason}"

    def held_paths(self) -> frozenset[Path]:
        """A process whose command could not be read names nothing; `unread` is how it
        keeps a sweep from deciding about the runtime it might have been running."""
        return frozenset()

    def unread(self) -> tuple[str, ...]:
        return (self.line(),)


@dataclass(frozen=True)
class ReferencingRuntimeProcess(RuntimeProcessOutcome, ABC, declared_name=None):
    """A process that named one path inside the installation.

    What that path holds and what it leaves undecided are the reference's answers, not
    each member's: the members differ in how a process came to name it, which is what
    their lines say, and in nothing else.
    """

    pid: int
    reference: RuntimeReference

    def held_paths(self) -> frozenset[Path]:
        return self.reference.held()

    def unread(self) -> tuple[str, ...]:
        return self.reference.unread()


@dataclass(frozen=True)
class UnattributedRuntimeProcess(ReferencingRuntimeProcess):
    """Invoked through something that names no build, so what it runs cannot be read back.

    Nothing on this machine was invoked this way when the retention was written; its own
    console scripts carry the built interpreter in their shebang, so a scan sees a build.
    It is a reachable invocation, not a hypothetical one: the installer's verification
    commands are written against `current/bin/python`.
    """

    def line(self) -> str:
        return f"Unattributed runtime PID {self.pid}: invoked as {self.reference.describes()}"


@dataclass(frozen=True)
class ImportingRuntimeProcess(ReferencingRuntimeProcess):
    """A process with an import root inside the installation, whatever it was invoked with.

    An import root is not a PATH entry. PATH is resolution: one entry wins, and the winner
    is already visible as the command, which is why 47 historical runtimes inherited on one
    process's PATH hold nothing. Every entry on an import path stays live for as long as the
    process does, so a build a running process can import from at any moment is in use —
    codex-latency ran a system interpreter with `PYTHONPATH` into a build and watched the
    collector call that build reclaimable.

    The root goes through the same `RuntimeReference` a command does, so an import path
    reaching the moving pointer is as undecidable here as an invocation through it.
    """

    def line(self) -> str:
        return f"Importing-runtime PID {self.pid}: {self.reference.describes()}"


@dataclass(frozen=True)
class RuntimeApplication:
    """The observed parent application's exact identity and display name."""

    process: ProcessIdentity
    name: str

    def describe(self) -> str:
        return f"{self.name} (application PID {self.process.pid})"


@dataclass(frozen=True, eq=False)
class RuntimeProcess(ProcessIdentity, LaunchRuntime, declared_name=None):
    application: RuntimeApplication


@dataclass(frozen=True)
class UnretiredRuntimeProcess(RuntimeProcessOutcome):
    process: RuntimeProcess

    def line(self) -> str:
        return (
            f"Unretired old-runtime PID {self.process.pid}: {self.process.prefix}; "
            f"owner {self.process.application.describe()}; not signalled"
        )

    def held_paths(self) -> frozenset[Path]:
        """What this process was invoked with — not what its inherited PATH would have
        found, which on one machine listed 47 runtimes for a single process."""
        return frozenset((self.process.interpreter,))

    def unread(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class RuntimeProcesses:
    """A read-only snapshot, not a process handle or permission to signal an app.

    Installer-launched children retain their own ChildProcess stop/reap authority.
    Discovering another application's interpreter grants no such authority.
    """

    outcomes: tuple[RuntimeProcessOutcome, ...]

    def lines(self) -> tuple[str, ...]:
        return tuple(outcome.line() for outcome in self.outcomes)

    def held_paths(self) -> frozenset[Path]:
        """Everything inside the installation this snapshot found a process using."""
        return frozenset().union(*(outcome.held_paths() for outcome in self.outcomes),
                                 frozenset())

    def unread(self) -> tuple[str, ...]:
        """Every process this snapshot could not read."""
        return tuple(line for outcome in self.outcomes for line in outcome.unread())

    @classmethod
    def imports(cls, platform: Platform, identity: ProcessIdentity, directory: Path,
                command: RuntimeReference) -> tuple[RuntimeProcessOutcome, ...]:
        """What this process can import from inside the installation.

        Read from its environment rather than guessed: `PYTHONPATH` entries and a
        `VIRTUAL_ENV` are the roots a live interpreter keeps on its import path, so a
        build named by one is in use however the process was invoked. Each goes through
        the same `RuntimeReference` a command does.
        """
        try:
            environment = platform.environment(identity)
        except ProcessLookupError:
            return ()
        except (OSError, IdentityMismatchError) as error:
            # Whether a hidden environment leaves anything undecided is the reference's
            # answer: it is what knows whether this process is inside the installation.
            return command.hidden(identity, error)
        named = (*environment.get("PYTHONPATH", "").split(os.pathsep),
                 environment.get("VIRTUAL_ENV", ""))
        references = (RuntimeReference.to(Path(value), directory) for value in named if value)
        return tuple(ImportingRuntimeProcess(identity.pid, reference)
                     for reference in references if reference.names_installation())

    @classmethod
    def observe(cls, target: LaunchRuntime, proc: Path = Path("/proc")) -> RuntimeProcesses:
        directory = target.prefix.parent
        outcomes: list[RuntimeProcessOutcome] = []
        platform = Platform.current()
        observer = ProcessIdentity.capture(os.getpid())
        lineage: frozenset[ProcessIdentity] = frozenset(observer.lineage())
        for entry in proc.iterdir():
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            try:
                identity = ProcessIdentity.capture(pid)
                invoked = platform.arguments(identity)[0]
                identity.require_alive()
            except ProcessLookupError:
                continue
            except (OSError, IdentityMismatchError) as error:
                outcomes.append(UnreadableRuntimeProcess(pid, str(error)))
                continue
            # A reference holds a path only when it names one build; the others are the
            # pointer, which names a build it will not say, and somewhere else entirely.
            interpreter = Path(invoked)
            command = RuntimeReference.to(interpreter, directory)
            # Independent of the command: a process importing from a build holds it even
            # when it was launched by a system interpreter from somewhere else entirely.
            outcomes.extend(cls.imports(platform, identity, directory, command))
            if not command.held():
                if command.names_installation():
                    outcomes.append(UnattributedRuntimeProcess(pid, command))
                continue
            runtime = LaunchRuntime.from_interpreter(interpreter)
            if runtime.prefix == target.prefix:
                continue
            try:
                if identity in lineage or observer in identity.lineage():
                    continue
                parent = platform.parent_identity(pid)
                application = RuntimeApplication(parent, platform.process_name(parent.pid))
                parent.require_alive()
                identity.require_alive()
            except (OSError, IdentityMismatchError) as error:
                outcomes.append(UnreadableRuntimeProcess(pid, str(error)))
                continue
            outcomes.append(
                UnretiredRuntimeProcess(
                    RuntimeProcess(
                        pid=identity.pid,
                        start_time=identity.start_time,
                        interpreter=runtime.interpreter,
                        application=application,
                    )
                )
            )
        return cls(tuple(outcomes))
