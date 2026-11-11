"""The practice bus a rehearsal carries: its copy, private home, size and space."""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path

from agent_comms.context_store import ContextManifestReference, ContextStore
from agent_comms.coordination.database import CoordinationStore, ReadableStore
from agent_comms.coordination.participant_store import ParticipantRows, ParticipantStore
from agent_comms.coordination.tables.annotations import ContextSpanRows
from agent_comms.coordination.tables.participants import Participants
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.native_runtime_input import NativeRuntimeInput
from agent_comms.execution.native_session_reopen import NativeSessionIdentity
from agent_comms.input_disposition import (
    InputDispositions,
    InputDocument,
    SnapshotInputDispositions,
)
from agent_comms.installer.backup import pinned_bus_files, saved_bus_files
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.nominal.field_codec import ThreadLookup
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.registry_document import RegistryEntry
from agent_comms.registry.registry_store import RegistryCut, RegistryStore
from agent_comms.storage.locked_store import (
    CompleteInputReader,
    DeclaredFile,
    InputCaptureFile,
    InstallationFiles,
    LiveFilesRequest,
    RegistryFile,
    RetainedCopyRequest,
    RootFile,
    UndeclaredFile,
)
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, NoMaintenanceHolder
from agent_comms.storage.private_path import PrivateDirectoryRole


@dataclass(frozen=True)
class PracticeMeasurement:
    bytes: int

    @classmethod
    def capture(cls, copy: Path) -> PracticeMeasurement:
        """Count the complete materialized tree, including SQLite companions and stages."""
        return cls(sum(path.stat().st_size for path in copy.rglob("*") if path.is_file()))

    def remaining(self, maximum_bytes: int) -> int:
        """An explicit selected-input allowance, not a limit on complete backups."""
        if self.bytes > maximum_bytes:
            raise UpgradeRefused(
                f"Practice copy has {self.bytes} bytes, exceeding {maximum_bytes} allowed bytes")
        return maximum_bytes - self.bytes

    @classmethod
    def of(cls, copy: Path, floor: float) -> PracticeMeasurement:
        """Refuse a copy without room for the rehearsal's second copy above the disk floor. The
        free space is what a copy can cost; a fixed byte cap beside it refused a 3.01 GiB bus
        with 29 GiB free and stopped install 45."""
        measured = cls.capture(copy)
        if shutil.disk_usage(copy.parent).free < 2 * measured.bytes + floor:
            raise UpgradeRefused(
                f"Insufficient free space for rehearsal of {measured.bytes} copied bytes")
        return measured


@dataclass
class PracticeCopy:
    """A private, incrementally selected cut of the source owners' files."""

    root: Path
    path: Path
    copied: dict[Path, DeclaredFile | UndeclaredFile] = field(default_factory=dict)

    @property
    def request(self) -> RetainedCopyRequest:
        """The copy's files, still naming the live root their recorded paths refer to."""
        return RetainedCopyRequest(self.path, self.root)

    @property
    def copied_names(self) -> tuple[Path, ...]:
        return tuple(sorted(resource.owner.relocated(
            resource.live(self.root), resource.path, source,
        ) for source, resource in self.copied.items()))

    @property
    def floor_bytes(self) -> int:
        return ceil(StorageRule.load(self.root).floor_gib * 2**30)

    @property
    def available_bytes(self) -> int:
        return shutil.disk_usage(self.path.parent).free - self.floor_bytes

    def prepare(self) -> None:
        """One space and private-directory owner for whole and selected materialization."""
        PrivateDirectoryRole.make_parents(self.path)
        if self.available_bytes < 0:
            raise UpgradeRefused("Insufficient free space before building the practice copy")

    def remaining_bytes(self, maximum_bytes: int) -> int:
        """Share one explicit total allowance across incrementally selected stores.

        The current tree, not the copied-resource index, owns used bytes. This
        includes retained WAL/groups and data placed there by another copy step.
        Caller owns this private copy's serial materialization and wider cut.
        """
        return min(PracticeMeasurement.capture(self.path).remaining(maximum_bytes),
                   self.available_bytes)

    @contextmanager
    def capturing_inputs(self) -> Iterator[RegistryCut]:
        """Enclose the first and every subsequent selected-input materialization.

        The caller already owns the install's retargeted hold. Materialization
        drops its claim, including the exported claim inherited by new children,
        so ordinary source writers refuse while the private target stays writable.
        Pins prevent last-close WAL checkpoints, not writers. Drain
        in-flight transactions separately, releasing each SQL lock before seeking
        another or the registry. The hold excludes later non-holder transactions;
        source observations additionally reject changes outside that writer path.
        Ordinary whole-file include() and complete backups keep their own lifetime.
        This supplies a recorded cut, not native launch or write authority.
        """
        if self.copied or self.path.exists() or self.path.is_symlink():
            raise UpgradeRefused("Selected input capture must start before materialization")
        source, target = self.root.resolve(), self.path.resolve()
        if target.is_relative_to(source) or source.is_relative_to(target):
            raise UpgradeRefused("Selected input capture requires a separate destination")
        barrier = MaintenanceBarrier(RegistryFile.path_in(self.root))
        files = LiveFilesRequest(self.root)
        resources = InstallationFiles(files).owned_files(tuple(
            owner.path_in(directory)
            for owner, directory in InputCaptureFile.declarations_in(self.root)
        ))
        present = tuple(resource for resource in resources if files.location(resource).exists())
        with (barrier.capturing(LaunchRuntime.running()),
              pinned_bus_files(files, present)):
            for resource in present:
                with resource.owner.writers_drained(files.location(resource)):
                    pass
            with RegistryStore.at(self.root).current_snapshot() as original:
                before = saved_bus_files(files, resources)
                with NoMaintenanceHolder().acting():
                    yield original
                if not original.matches_current():
                    raise UpgradeRefused("Registry changed during selected input capture")
                if saved_bus_files(files, resources) != before:
                    raise UpgradeRefused("Source stores changed during selected input capture")

    def capture_inputs(
        self, owners: tuple[ThreadLookup, ...], *, maximum_bytes: int,
        identities: tuple[NativeSessionIdentity, ...] = (),
        requests: tuple[ContextManifestReference, ...] = (),
    ) -> PracticeMeasurement:
        """The selected operation, including its first complete reader copy.

        Resolve births from the original cut, not caller-supplied name/generation
        records. No selection callback runs before or outside this lifetime.
        The returned measurement describes data, never native launch authority.
        """
        with self.capturing_inputs() as original:
            entries = tuple(original.snapshot.entry(original.snapshot.born(owner).name)
                            for owner in owners)
            self.prepare()
            paths = tuple(owner.path_in(directory) for owner, directory
                          in CompleteInputReader.declarations_in(self.root))
            files = LiveFilesRequest(self.root)
            readers = saved_bus_files(files, InstallationFiles(files).owned_files(paths))
            required = sum(member.size for group in readers.values() for member in group.values())
            if required > self.remaining_bytes(maximum_bytes):
                raise UpgradeRefused("Complete input readers exceed the selected copy allowance")
            self.include(paths)
            self.remaining_bytes(maximum_bytes)
            self.include_contexts(identities, maximum_bytes=maximum_bytes, requests=requests)
            self.include_inputs(entries, maximum_bytes=maximum_bytes, identities=identities)
            self.require_inputs(entries, identities=identities, requests=requests)
            measured = PracticeMeasurement.capture(self.path)
            measured.remaining(maximum_bytes)
            return measured

    def require_inputs(
        self, entries: tuple[RegistryEntry, ...], *,
        identities: tuple[NativeSessionIdentity, ...],
        requests: tuple[ContextManifestReference, ...],
    ) -> None:
        """Read back the selected copy before its original capture cut is released.

        Use the ordinary unresolved projection, context resolver and participant
        aggregate reader. No lifecycle reset, input replay, callback or second
        hold can stand in for equality while these originals are still frozen.
        """
        ContextStore(self.root).require_retained(ContextStore(self.path), identities, requests)
        if InputDispositions(self.root).unresolved(entries) != InputDispositions(self.path).unresolved(entries):
            raise UpgradeRefused("Selected unresolved input reader differs from its original")
        with (
            ReadableStore(CoordinationStore.path_in(self.root)).reader_snapshot() as source,
            ReadableStore(CoordinationStore.path_in(self.path)).reader_snapshot() as retained,
        ):
            schema = CoordinationStore.schema_at(CoordinationStore.path_in(self.root))
            schema.require_schema(source)
            schema.require_schema(retained)
            for identity in identities:
                source_inputs, retained_inputs = ({row.input_id: row for row in NativeRuntimeInput.select(
                    db, session_id=identity.session_id, session_file=identity.session_file,
                )} for db in (source, retained))
                if source_inputs != retained_inputs:
                    raise UpgradeRefused("Selected native session/path inputs differ from their originals")
            for entry in entries:
                lookup = entry.thread.incarnation.require_recorded().lookup
                if Participants.count(source, participant_lookup=lookup) != Participants.count(
                    retained, participant_lookup=lookup,
                ):
                    raise UpgradeRefused("Selected participant presence differs from its original")
            for person in Participants.stream(retained):
                if ParticipantStore.read_at(source, person.participant_lookup) != ParticipantStore.read_at(
                    retained, person.participant_lookup,
                ):
                    raise UpgradeRefused("Selected participant aggregate differs from its original")

    def include_contexts(
        self, identities: Iterable[NativeSessionIdentity], *, maximum_bytes: int,
        requests: Iterable[ContextManifestReference] = (),
    ) -> None:
        """Explicit selected-context inputs, never a full-file backup.

        Normal include() and the installer's live-root backup remain whole-file
        operations. This selection must precede including the context resource;
        an existing destination is refused rather than overwritten or reimported.
        Select contexts before finalizing coordinator evidence, whose annotation
        dependency selection consumes these exact retained segments.
        """
        if CoordinationStore.path_in(self.root) in self.copied:
            raise UpgradeRefused("Select contexts before finalizing coordinator input evidence")
        self.prepare()
        source = ContextStore(self.root)
        resource, = InstallationFiles(LiveFilesRequest(self.root)).owned_files((source.path,))
        source.retain_contexts(
            ContextStore(self.path), identities, requests=requests,
            maximum_bytes=self.remaining_bytes(maximum_bytes),
        )
        self.copied[source.path] = resource

    def include_inputs(
        self, entries: tuple[RegistryEntry, ...], *, maximum_bytes: int,
        identities: Iterable[NativeSessionIdentity] = (),
    ) -> InputDocument:
        """Selected coordinator evidence beside the complete captured input readers.

        include() owns capturing the original bus and ledger; this method never
        substitutes a selected ledger or creates suppression evidence. Caller owns
        one consistent cut and serial materialization, not native launch authority.
        """
        self.prepare()
        for owner, directory in CompleteInputReader.declarations_in(self.root):
            source = owner.path_in(directory)
            if source.exists():
                if source not in self.copied:
                    raise UpgradeRefused(f"Capture complete {source.name} before selecting input evidence")
            elif owner.path_in(self.path).exists():
                raise UpgradeRefused(f"Copied {source.name} has no original reader source")
        contexts = ContextStore(self.path)
        source_contexts = ContextStore.path_in(self.root)
        if contexts.path.exists() and source_contexts not in self.copied:
            raise UpgradeRefused("Copied contexts have no captured original reader source")
        source = CoordinationStore.path_in(self.root)
        resource, = InstallationFiles(LiveFilesRequest(self.root)).owned_files((source,))
        segments = contexts.original_segments() if source_contexts in self.copied else ()
        with CoordinationStore.retaining_rows(
            source, CoordinationStore.path_in(self.path),
            maximum_bytes=self.remaining_bytes(maximum_bytes),
            original_type=ParticipantRows,
        ) as original:
            selected = SnapshotInputDispositions(self.path, original).retain_unresolved(entries)
            ParticipantStore.retain_owners(original, tuple(
                entry.thread.incarnation.require_recorded().lookup for entry in entries
            ))
            NativeRuntimeInput.retain_sessions(original, identities)
            ContextSpanRows.retain_segments(original, segments)
        self.copied[source] = resource
        return selected

    def include(self, paths: tuple[Path, ...]) -> None:
        self.prepare()
        witnesses = set(MaintenanceBarrier(RegistryFile.path_in(self.root)).witness_paths)
        for resource in InstallationFiles(LiveFilesRequest(self.root)).owned_files(paths):
            source = resource.live(self.root)
            if source in self.copied or source in witnesses or not source.exists():
                continue
            target = resource.retained(self.path)
            PrivateDirectoryRole.make_parents(target.parent)
            self.copied.update((member, resource) for member in resource.owner.copy_group(source, target))

    def retain(self, paths: tuple[Path, ...]) -> None:
        """Discard inputs used only to establish that an earlier carry was finished."""
        selected = InstallationFiles(LiveFilesRequest(self.root)).owned_files(paths)
        discarded = tuple(source for source, resource in self.copied.items() if resource not in selected)
        for source in discarded:
            resource = self.copied[source]
            primary = resource.live(self.root)
            resource.owner.relocated(primary, resource.retained(self.path), source).unlink(missing_ok=True)
            del self.copied[source]

    @property
    def measurement(self) -> PracticeMeasurement:
        return PracticeMeasurement.of(self.path, self.floor_bytes)
