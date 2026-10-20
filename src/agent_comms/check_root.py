"""A selected, disposable comms root; source readers never grant execution."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from typing import ClassVar

from .attempt_recovery import RecoveryMonitorCapability
from .candidate_maintenance import drain_candidate_catchup
from .child_process import NoProcess
from .cli_result import CliResult
from .comms import Comms
from .fresh_private_session import create_fresh_private_session
from .goals import NoGoal
from .launch_root import LaunchRoot
from .launch_runtime import ThreadLaunch
from .native_entries import ImportedSessionWriter, NativeEntry
from .read_basis import MemberActivityDisplayScope
from .registry_document import RosterVisibility
from .storage_rules import StorageRoots
from .threads import Thread
from .transcript_routes import TranscriptRoutes


@dataclass(frozen=True)
class CheckRootResult(CliResult):
    return_code: int

    def exit_code(self) -> int:
        return self.return_code


@dataclass(frozen=True)
class CheckRoot:
    """Own one check's selected history, workers and directory through its exit.

    The command receives the directory through an explicit ``{root}`` argument,
    never an environment setting. It must not exist before entry. Old turns,
    goals, deliveries and process identities do not travel with the history.
    """

    source: Comms
    threads: tuple[str, ...]
    into: Path
    max_bytes: ClassVar[int] = 1024 * 1024
    max_records: ClassVar[int] = 200

    @contextmanager
    def open(self) -> Iterator[Comms]:
        root = self.into.expanduser().absolute()
        if not self.threads:
            raise ValueError("A check root needs at least one named thread")
        scratch = self.source.registry.storage_rule.scratch_base.resolve()
        if (
            not root.resolve().is_relative_to(scratch)
            or len(root.resolve().relative_to(scratch).parts) < 2
        ):
            raise ValueError("A check root belongs under the caller's agent-scratch directory")
        if root.resolve().is_relative_to(self.source.root.resolve()):
            raise ValueError("A check root cannot be inside its source")
        root.mkdir(mode=0o700, parents=True)  # Refuse an existing directory, including a symlink.
        try:
            copied = Comms(root)
            self._populate(copied)
            try:
                yield copied
            finally:
                for thread in copied.registry.snapshot().visible_threads(
                    RosterVisibility(archived=True)
                ):
                    copied.owners.stop(thread.name)
        finally:
            drain_candidate_catchup(root)
            RecoveryMonitorCapability.retire_check_root(root)

    def _populate(self, copied: Comms) -> None:
        snapshot = self.source.registry.snapshot()
        names = tuple(dict.fromkeys(snapshot.require(name).name for name in self.threads))
        selected = frozenset(names)
        catalog = self.source.channels.catalog.read()
        aliases = frozenset(alias for alias, name in snapshot.aliases.items() if name in selected)
        targets = frozenset(
            target for name in names for target in catalog.targets_for(snapshot.require(name).tags)
        )
        scope = MemberActivityDisplayScope("#check", targets, participant_names=selected | aliases)
        with self.source.bus.log.opened_wire_snapshot() as source:
            messages = tuple(
                reversed(
                    tuple(
                        islice(
                            (
                                message
                                for message in source.newest_messages(self.max_bytes)
                                if scope.includes(message)
                            ),
                            self.max_records,
                        )
                    )
                )
            )
        copied.messaging.initialize_private_initial_protocol(history=messages)
        copied.channels.catalog.replace(catalog)
        declarations = {}
        for name in names:
            original = snapshot.require(name)
            slot = original.incarnation.require_recorded().lookup
            project = copied.root / "projects" / slot
            project.mkdir(parents=True, mode=0o700)
            session = self._session(original, copied.root / "native-sessions" / slot, project)
            declarations[name] = replace(
                original.without_turn(),
                worktree=str(project),
                parent=original.parent if original.parent in selected else None,
                task=None,
                goal=NoGoal(),
                process_identity=NoProcess(),
                session_file=session,
                earlier_projects=(),
                project_resume_due=False,
                storage_roots=StorageRoots(copied.root / "scratch" / slot),
                turn_generation=0,
                last_finished_turn_id=None,
            )
        with copied.registry.store.editing() as edit:
            edit.document.restore_stopped(snapshot, names)
            edit.document.threads.update(declarations)
            # Old messages can name people outside the selected workers. Keep
            # only their deleted-name record, never their sessions or authority.
            for name in snapshot.claimed_generations:
                if not edit.document.taken(name):
                    edit.document.advance_claim(name)
                    edit.document.owners.advance(name)
            edit.commit()
        if messages:
            for name in names:
                copied.messaging.acknowledge_through(name, messages[-1].seq)

    def _session(self, original: Thread, directory: Path, project: Path) -> str:
        session = create_fresh_private_session(directory, worktree=project)
        source = original.runtime.history_source(self.source.root, original.incarnation)
        with source.reading() as reader, session.path.open("r+") as output:
            writer = ImportedSessionWriter(output, NativeEntry.read(output.readline().encode()))
            output.seek(0, os.SEEK_END)
            cut = reader.cut()
            if reader.has_records(cut.offset):
                selected, used = [], 0
                with closing(reader.reverse(cut.offset)) as records:
                    for record in islice(records, self.max_records):
                        if used + record.size > self.max_bytes:
                            break
                        selected.append(record)
                        used += record.size
                with source.projection(
                    source, TranscriptRoutes(self.source.root)
                ) as project_records:
                    for _record, events in project_records(
                        iter(reversed(selected)),
                        max_records=self.max_records,
                        max_bytes=self.max_bytes,
                    ):
                        for event in events:
                            writer.append_recorded(event)
        return session.session_file

    def run(self, command: Sequence[str]) -> CheckRootResult:
        command = tuple(command)
        if command[:1] == ("--",):
            command = command[1:]
        if not command or "{root}" not in command:
            raise ValueError("check-root needs -- COMMAND with an explicit {root} argument")
        environment = ThreadLaunch.strip(dict(os.environ))
        environment = LaunchRoot.environment(environment)
        with self.open() as copied:
            arguments = tuple(argument.replace("{root}", str(copied.root)) for argument in command)
            return CheckRootResult(
                subprocess.run(arguments, cwd=copied.root, env=environment).returncode
            )
