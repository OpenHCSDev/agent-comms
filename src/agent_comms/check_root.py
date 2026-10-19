"""A selected, disposable comms root; source readers never grant execution."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from typing import ClassVar

from .attempt_recovery import RecoveryMonitorCapability
from .child_process import NoProcess
from .cli_result import CliResult
from .comms import Comms
from .fresh_private_session import create_fresh_private_session
from .goals import NoGoal
from .importing import ImportBuffer, ImportLimits
from .launch_runtime import RestartEnvironment, ThreadLaunch
from .native_entries import TranscriptProjection
from .native_transcript import NativeTranscript
from .read_basis import MemberActivityDisplayScope
from .storage_rules import StorageRoots
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
        if not root.resolve().is_relative_to(scratch) or len(root.resolve().relative_to(scratch).parts) < 2:
            raise ValueError("A check root belongs under the caller's agent-scratch directory")
        if root.resolve().is_relative_to(self.source.root.resolve()):
            raise ValueError("A check root cannot be inside its source")
        root.mkdir(mode=0o700, parents=True)  # Refuse an existing directory, including a symlink.
        try:
            copied = Comms(root)
            try:
                self._populate(copied)
                yield copied
            finally:
                for thread in copied.registry.snapshot().threads.values():
                    if thread.process_alive:
                        copied.owners.stop(thread.name)
        finally:
            RecoveryMonitorCapability.retire_check_root(root)

    def _populate(self, copied: Comms) -> None:
        snapshot = self.source.registry.snapshot()
        names = tuple(dict.fromkeys(snapshot.require(name).name for name in self.threads))
        selected = frozenset(names)
        catalog = self.source.channels.catalog.read()
        aliases = frozenset(alias for alias, name in snapshot.aliases.items() if name in selected)
        targets = frozenset(target for name in names
                            for target in catalog.targets_for(snapshot.require(name).tags))
        scope = MemberActivityDisplayScope("#check", targets, participant_names=selected | aliases)
        with self.source.bus.log.opened_wire_snapshot() as source:
            messages = tuple(reversed(tuple(islice(
                (message for message in source.newest_messages(self.max_bytes) if scope.includes(message)),
                self.max_records))))
        copied.messaging.initialize_private_initial_protocol(history=messages)
        copied.channels.catalog.replace(catalog)
        declarations = {}
        for name in names:
            original = snapshot.require(name)
            project = copied.root / "projects" / name
            project.mkdir(parents=True, mode=0o700)
            session = self._session(original.session_file, copied, name, project)
            declarations[name] = replace(
                original.without_turn(), worktree=str(project),
                parent=original.parent if original.parent in selected else None,
                task=None, goal=NoGoal(), process_identity=NoProcess(), session_file=session,
                earlier_projects=(), project_resume_due=False,
                storage_roots=StorageRoots(copied.root / "scratch" / name),
                turn_generation=0, last_finished_turn_id=None,
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

    def _session(self, original: str | None, copied: Comms, name: str, project: Path) -> str:
        directory = copied.root / "native-sessions" / name
        session = create_fresh_private_session(directory, worktree=project)
        if original is None:
            return session.session_file
        reader = NativeTranscript(Path(original))
        entries = tuple(reversed(tuple(islice(reader.tail(max_bytes=self.max_bytes), self.max_records))))
        routes = TranscriptRoutes(self.source.root)
        output_routes = TranscriptRoutes(copied.root)
        parent = None
        limits = ImportLimits(messages=self.max_records, characters=self.max_bytes)
        with routes.for_session(original) as source_routes, session.path.open("a") as output:
            for entry in entries:
                # Core's portable import owns tool/thought conversion to historical
                # text. A newest window must never resume a partial old tool round.
                buffer = ImportBuffer(limits)
                routing = source_routes.get(entry.id)
                buffer.add(entry.events(TranscriptProjection(position=0, routing=routing)))
                for message in buffer.messages:
                    copied_entry = message.session_entry(parent, entry.timestamp.at)
                    output.write(json.dumps(copied_entry, ensure_ascii=False) + "\n")
                    parent = copied_entry["id"]
                    if routing is not None:
                        output_routes.record(session.session_file, (parent,), routing)
        return session.session_file

    def run(self, command: Sequence[str]) -> CheckRootResult:
        command = tuple(command)
        if command[:1] == ("--",):
            command = command[1:]
        if not command or "{root}" not in command:
            raise ValueError("check-root needs -- COMMAND with an explicit {root} argument")
        environment = ThreadLaunch.strip(dict(os.environ))
        RestartEnvironment.strip(environment, "root")
        with self.open() as copied:
            arguments = tuple(str(copied.root) if argument == "{root}" else argument for argument in command)
            return CheckRootResult(subprocess.run(arguments, cwd=copied.root, env=environment).returncode)
