"""Future retained-summary installation through the original stopped batch.

The release owner supplies reviewed immutable artifacts and the typed task carry
member. This module contains no guessed target, automatic approval, retry, client
signal, native input or alternative owner-stop/launch implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from abc import ABC, abstractmethod
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Annotated, ClassVar
from urllib.parse import unquote, urlsplit

from agent_comms.active_route import ActiveRoute, active_route_path, read_active_route, _publish_active_route_locked, guard_default_route_write
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_package import verify_native_package
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.owner_restart import OwnerRestartRequest
from agent_comms.owner_launch import RestartEnvironment, RetainedOwnerLaunch
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.private_path import PrivateDirectoryRole
from agent_comms.store_files import _atomic_write_text
from agent_comms.threads import Thread
from publish_openhcs_recovery import COMMANDS, LINKS, ROOT, digest, fsync_directory, require_no_clients, retain_file
from retained_summary_reset import RuntimeCompactionFiles, RuntimeGoalFiles
from native_schema_carry import RuntimeNativeFiles
from runtime_installation import RuntimeInstallation
from cutover_child import restore_stopped_batch


@dataclass(frozen=True)
class ReviewedArtifact:
    path: Annotated[Path, PathText]
    sha256: str

    def require_original(self):
        if digest(self.path) != self.sha256:
            raise RuntimeError(f'Reviewed artifact changed: {self.path}')


@dataclass(frozen=True)
class CohortActivation:
    """The existing verify.py artifact format, including its fourth dependency."""
    stage: Annotated[Path, PathText]
    pins: dict[str, str]
    sdk: str
    textual_diff_view: str
    native_package: Annotated[Path, PathText]
    state: str
    staging_receipt: Annotated[Path, PathText]
    bins: dict[str, str]
    native_cli: str
    native_manifest: str
    native_tree: str
    native_configuration_note: str

    def source_heads(self):
        # This scalar is the ORIGINAL producer's declared fourth dependency,
        # not an inferred omission, alternate pin list or dropped proof row.
        return {**self.pins, 'textual_diff_view': self.textual_diff_view}


@dataclass(frozen=True)
class PackageVcsInfo:
    vcs: str
    commit_id: str
    requested_revision: str


@dataclass(frozen=True)
class PackageDirectUrl(ABC):
    url: str

    @abstractmethod
    def require_original(self, source_head: str, artifacts: tuple[ReviewedArtifact, ...]):
        """Validate the installer origin; source bytes are owned by InstalledSource."""


@dataclass(frozen=True)
class VcsPackageDirectUrl(PackageDirectUrl):
    vcs_info: PackageVcsInfo

    def require_original(self, source_head: str, artifacts: tuple[ReviewedArtifact, ...]):
        if self.vcs_info.commit_id != source_head:
            raise RuntimeError('Installed VCS origin differs from the declared source')


@dataclass(frozen=True)
class PackageArchiveInfo:
    hashes: dict[str, str] = field(default_factory=dict, metadata={'wire_omit_default': True})
    hash: str | None = field(default=None, metadata={'wire_omit_default': True})

    def require_original(self, artifact: ReviewedArtifact):
        hashes = dict(self.hashes)
        if self.hash is not None:
            algorithm, separator, value = self.hash.partition('=')
            if not separator or not algorithm or not value:
                raise RuntimeError('Installed archive has a malformed legacy hash')
            if algorithm in hashes and hashes[algorithm] != value:
                raise RuntimeError('Installed archive has conflicting hash provenance')
            hashes[algorithm] = value
        sha256 = hashes.get('sha256')
        if sha256 is not None and sha256 != artifact.sha256:
            raise RuntimeError('Installed archive SHA256 differs from the reviewed artifact')
        artifact.require_original()


@dataclass(frozen=True)
class ArchivePackageDirectUrl(PackageDirectUrl):
    archive_info: PackageArchiveInfo

    def require_original(self, source_head: str, artifacts: tuple[ReviewedArtifact, ...]):
        origin = urlsplit(self.url)
        if origin.scheme != 'file' or origin.netloc not in ('', 'localhost'):
            raise RuntimeError('Installed archive requires its original local artifact')
        path = Path(unquote(origin.path))
        originals = tuple(artifact for artifact in artifacts if artifact.path == path)
        if len(originals) != 1:
            raise RuntimeError('Installed archive requires one reviewed original artifact')
        self.archive_info.require_original(originals[0])


@dataclass(frozen=True)
class InstalledSource:
    module: str
    head: str
    location: str
    files: int
    python_files: int
    byte_equal: bool
    direct_url: VcsPackageDirectUrl | ArchivePackageDirectUrl
    inventory_sha256: str

    @classmethod
    def command_arguments(cls, arguments: list[str]):
        """Decode the declared installer source once at a private CLI boundary.

        Commands forward this owner and its reviewed archives; they do not
        infer a Git source revision from file-wheel metadata.
        """
        import argparse

        parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        parser.add_argument('--installed-source', type=Path, required=True)
        parser.add_argument('--archive', nargs=2, action='append', default=[],
                            metavar=('PATH', 'SHA256'))
        supplied, remaining = parser.parse_known_args(arguments)
        source = FieldCodec.decode(cls, json.loads(supplied.installed_source.read_text()))
        artifacts = tuple(ReviewedArtifact(Path(path).absolute(), sha256)
                          for path, sha256 in supplied.archive)
        return source, artifacts, remaining

    def require_original(self, artifacts: tuple[ReviewedArtifact, ...] = ()):
        if not self.byte_equal:
            raise RuntimeError('Installed source bytes are not verified')
        self.direct_url.require_original(self.head, artifacts)

    def require_package(self, module: str, location: Path, direct_url: dict,
                        artifacts: tuple[ReviewedArtifact, ...]) -> str:
        """Bind the declared source to the actual imported installer origin."""
        if (self.module, self.location) != (module, str(location)):
            raise RuntimeError('Installed source names another imported package')
        if FieldCodec.encode(self.direct_url) != direct_url:
            raise RuntimeError('Installed source names another installer origin')
        self.require_original(artifacts)
        return self.head


@dataclass(frozen=True)
class InstalledSourceProof:
    state: str
    prefix: Annotated[Path, PathText]
    sources: tuple[InstalledSource, ...]
    native_package: Annotated[Path, PathText]
    native_cli: str
    native_manifest: str
    native_tree: str
    native_full_trust: bool
    sdk: str
    package_count: int
    packages: tuple[tuple[str, str], ...]
    requirements_sha256: str
    protected_old_prefix_files: int
    protected_old_prefix_files_unchanged: bool
    public_install_changed: bool
    new_native_build: bool
    source_overlay: bool
    dependency_bypass: bool
    journey_owners: tuple[str, ...]
    journey_assessment: str
    archive_artifacts: tuple[ReviewedArtifact, ...] = field(
        default=(), metadata={'wire_omit_default': True})

    def require_activation(self, activation: CohortActivation):
        actual = {source.module: source.head for source in self.sources}
        if len(actual) != len(self.sources) or actual != activation.source_heads():
            raise RuntimeError('Source proof differs from the complete activation dependencies')
        if self.prefix != activation.stage or self.sdk != activation.sdk:
            raise RuntimeError('Source proof names another prefix/SDK')
        if (self.native_package, self.native_manifest, self.native_tree) != (
                activation.native_package, activation.native_manifest, activation.native_tree):
            raise RuntimeError('Source proof names another native artifact')
        for source in self.sources:
            source.require_original(self.archive_artifacts)
        if not self.native_full_trust or self.source_overlay or self.dependency_bypass:
            raise RuntimeError('Package/source/native trust is incomplete')

    def require_frontend_successor(self, original: InstalledSourceProof):
        """Preserve every installed package resource outside the Toad package."""
        for proof in (original, self):
            if (proof.native_package, proof.native_manifest, proof.native_tree) != (
                    original.native_package, original.native_manifest, original.native_tree):
                raise RuntimeError('Frontend publication cannot change native provenance')
            if not proof.native_full_trust or proof.source_overlay or proof.dependency_bypass:
                raise RuntimeError('Frontend publication requires original trusted packages')
        if original.packages != self.packages or original.sdk != self.sdk:
            raise RuntimeError('Frontend publication cannot change backend dependencies')
        before = {source.module: source for source in original.sources}
        after = {source.module: source for source in self.sources}
        if before.keys() != after.keys() or 'toad' not in before:
            raise RuntimeError('Frontend source membership changed')
        for module in before.keys() - {'toad'}:
            if before[module].head != after[module].head:
                raise RuntimeError(f'Frontend publication changed backend source: {module}')
        # Compare the actual installed resource trees, including dependencies
        # and metadata. Different bin shebangs are outside these package trees.
        for proof in (original, self):
            for source in proof.sources:
                if not Path(source.location).resolve().is_relative_to(proof.prefix.resolve()):
                    raise RuntimeError('Installed source lies outside its declared prefix')
                source.require_original(proof.archive_artifacts)
        old_site = Path(before['toad'].location).parent
        new_site = Path(after['toad'].location).parent
        def resources(site):
            return {path.relative_to(site): path for path in site.rglob('*')
                    if path.is_file() and '__pycache__' not in path.parts
                    and path.relative_to(site).parts[0] != 'toad'
                    and not path.relative_to(site).parts[0].startswith('batrachian_toad-')}
        old_files, new_files = resources(old_site), resources(new_site)
        if old_files.keys() != new_files.keys():
            raise RuntimeError('Frontend publication changed backend resource membership')
        for relative, old in old_files.items():
            if relative.name == 'RECORD' and relative.parent.name.endswith('.dist-info'):
                # RECORD includes prefix-specific executable hashes; its
                # package resource membership is already compared above.
                continue
            if old.read_bytes() != new_files[relative].read_bytes():
                raise RuntimeError(f'Frontend publication changed backend resource: {relative}')


@dataclass(frozen=True)
class ReviewedRetainedSummaryCohort:
    commands: ClassVar[tuple[str, ...]] = COMMANDS
    target: Annotated[Path, PathText]
    current_prefix: Annotated[Path, PathText]
    original_route: ActiveRoute
    native: Annotated[Path, PathText]
    activation: ReviewedArtifact
    source_proof: ReviewedArtifact
    actual_gates: tuple[ReviewedArtifact, ...]

    def require_original(self):
        if self.original_route.root != ROOT:
            raise RuntimeError('This reviewed one-use publisher names another public root')
        if sys.executable != str(self.target / 'bin/python'):
            raise RuntimeError('Use the reviewed target interpreter')
        self.activation.require_original()
        self.source_proof.require_original()
        activation = FieldCodec.decode(CohortActivation, json.loads(self.activation.path.read_text()))
        if activation.stage != self.target:
            raise RuntimeError('Activation names another source cohort')
        if activation.sdk != '0.12.1' or activation.native_package != self.native:
            raise RuntimeError('Activation names another SDK/native pair')
        if activation.staging_receipt != self.source_proof.path:
            raise RuntimeError('Activation names another source proof')
        proof = FieldCodec.decode(InstalledSourceProof, json.loads(self.source_proof.path.read_text()))
        proof.require_activation(activation)
        gates = {gate.path for gate in self.actual_gates}
        if not gates or len(gates) != len(self.actual_gates) or gates.intersection(
                (self.activation.path, self.source_proof.path)):
            raise RuntimeError('Distinct reviewed actual installed journey gates are required')
        for gate in self.actual_gates:
            gate.require_original()
        self.require_runtime()
        self.require_publication_originals()

    def require_runtime(self):
        verify_native_package(self.native)

    def require_publication_originals(self):
        if read_active_route() != self.original_route:
            raise RuntimeError('Original active route changed; recapture/review required')
        for command in self.commands:
            if (LINKS / command).readlink() != self.current_prefix / 'bin' / command:
                raise RuntimeError('Original default changed; recapture/review required')
            if not (self.target / 'bin' / command).is_file():
                raise RuntimeError('Reviewed target entrypoint is missing')
            temporary = LINKS / (command + '.retained-summary-publish')
            if temporary.exists() or temporary.is_symlink():
                raise RuntimeError('Original publication attempt requires review')

    def publish(self, directory: int):
        self.require_publication_originals()
        self.publish_route(directory)
        self.publish_links()

    def publish_route(self, directory: int):
        target_route = replace(self.original_route, native_package=self.native)
        _publish_active_route_locked(target_route, active_route_path(), directory,
                                     expected=self.original_route)

    def publish_links(self):
        for command in self.commands:
            link = LINKS / command
            if link.readlink() != self.current_prefix / 'bin' / command:
                raise RuntimeError('Default changed during publication; remain stopped')
            temporary = LINKS / (command + '.retained-summary-publish')
            temporary.symlink_to(self.target / 'bin' / command)
            temporary.replace(link)
        fsync_directory(LINKS)
        if read_active_route() != replace(self.original_route, native_package=self.native):
            raise RuntimeError('Target route readback differs')


@dataclass(frozen=True)
class ReviewedFrontendCohort(ReviewedRetainedSummaryCohort):
    """Publish only the UI when every imported backend byte stays unchanged."""

    current_source_proof: ReviewedArtifact
    commands: ClassVar[tuple[str, ...]] = ('toad',)

    def require_runtime(self):
        # This operation neither changes nor acquires the native package.
        # Its trust comes from the original published proof, not a new read.
        self.current_source_proof.require_original()
        original = FieldCodec.decode(InstalledSourceProof,
                                    json.loads(self.current_source_proof.path.read_text()))
        target = FieldCodec.decode(InstalledSourceProof,
                                  json.loads(self.source_proof.path.read_text()))
        if original.prefix != self.current_prefix:
            raise RuntimeError('Original published proof names another installation')
        if self.original_route.native_package != self.native:
            raise RuntimeError('Frontend publication cannot change the native route')
        target.require_frontend_successor(original)
        for command in COMMANDS:
            if command not in self.commands and (LINKS / command).readlink() != self.current_prefix / 'bin' / command:
                raise RuntimeError('Original backend default changed')

    def publish_route(self, directory: int):
        if read_active_route() != self.original_route:
            raise RuntimeError('Original route changed during frontend publication')

    def publish_frontend(self, receipt: Path):
        """One link switch under original client custody; no owner stop/restart."""
        if receipt.exists() or receipt.is_symlink():
            raise RuntimeError('Original frontend attempt requires review; never repeat')
        PrivateDirectoryRole.require(receipt.parent.lstat())
        self.require_original()
        with guard_default_route_write(self.original_route.root, blocking=False):
            self._publish_frontend_links(receipt)

    def _publish_frontend_links(self, receipt: Path):
        directory = os.open(LINKS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.fstat(directory).st_uid != os.geteuid():
                raise RuntimeError('Default command directory is not owned')
            self.require_original()
            descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'w') as opened:
                json.dump({'phase': 'frontend-originals-verified', 'cohort': FieldCodec.encode(self),
                           'started': time.time()}, opened, indent=2)
                opened.flush()
                os.fsync(opened.fileno())
            fsync_directory(receipt.parent)
            try:
                self.publish(directory)
                _atomic_write_text(receipt, json.dumps({'phase': 'frontend-published-live-ui-pending',
                    'cohort': FieldCodec.encode(self), 'finished': time.time()}, indent=2)+'\n', fsync_parent=True)
            except BaseException as cause:
                # Recovery owns only the link this operation could change.
                link = LINKS / 'toad'
                if link.readlink() == self.target / 'bin/toad':
                    temporary = LINKS / 'toad.retained-summary-publish'
                    if temporary.exists() or temporary.is_symlink():
                        raise RuntimeError('Frontend recovery has uncertain temporary link')
                    temporary.symlink_to(self.current_prefix / 'bin/toad')
                    temporary.replace(link)
                    fsync_directory(LINKS)
                phase = ('frontend-failed-original-link-restored'
                         if link.readlink() == self.current_prefix / 'bin/toad'
                         else 'frontend-failed-link-unknown')
                _atomic_write_text(receipt, json.dumps({'phase': phase,
                    'cohort': FieldCodec.encode(self), 'error': repr(cause),
                    'finished': time.time()}, indent=2)+'\n', fsync_parent=True)
                raise
        finally:
            os.close(directory)


@dataclass(frozen=True)
class PublishRetainedSummary(StoppedOwnerInstallation):
    cohort: ReviewedRetainedSummaryCohort
    audience: tuple[OwnerRestartSelection, ...]
    originals: tuple[Thread, ...]
    task_carry: StoppedOwnerInstallation
    runtime_installation: RuntimeInstallation
    route_directory: int
    receipt: Path
    recovery_originals: tuple[ReviewedArtifact, ...] = field(init=False, repr=False)

    def __post_init__(self):
        # This witness precedes EVERY fence/signal. A failure before stopped
        # capture still has evidence; later installation cannot redefine it.
        object.__setattr__(self, 'recovery_originals', tuple(
            ReviewedArtifact(path, digest(path)) for path in sorted(self.recovery_paths())
        ))

    def recovery_paths(self) -> frozenset[Path]:
        """Include payloads the installation may replace, not only invariants."""
        from agent_comms.wire_log import WireLog

        bus = WireLog(ROOT / 'bus.jsonl')
        paths = self.protected_files().union(
            RuntimeCompactionFiles(ROOT).paths, RuntimeNativeFiles(ROOT).paths,
            (bus.path, bus.metadata_path),
            self.task_carry.recovery_paths(),
        )
        return frozenset(path for path in paths if path.exists() or path.is_symlink())

    def require_recovery_originals(self):
        if self.recovery_paths() != frozenset(item.path for item in self.recovery_originals):
            raise RuntimeError('Original recovery membership changed; remain stopped')
        for original in self.recovery_originals:
            original.require_original()

    def note(self, phase, **facts):
        previous = json.loads(self.receipt.read_text())
        previous.update(phase=phase, **facts)
        _atomic_write_text(self.receipt, json.dumps(previous, indent=2)+'\n', fsync_parent=True)

    def require_selection(self, snapshot, owners):
        live = {thread.name for thread in OwnerRestartRequest().threads(snapshot)}
        if live != {thread.name for thread in owners} or live != {item.name for item in self.audience}:
            raise RuntimeError('Complete original owner audience changed; recapture/review required')
        for selection, original in zip(self.audience, self.originals, strict=True):
            current = selection.require_current(snapshot)
            current.require_idle()
            if current != original:
                raise RuntimeError('Original owner settings changed')
        self.cohort.require_original()
        require_no_clients(self.audience)
        InputDispositions(ROOT / InputDispositions.filename).read()
        self.task_carry.require_selection(snapshot, owners)

    def protected_files(self) -> frozenset[Path]:
        # Original uncertainty and evidence stay in their OWN stores. Runtime
        # compaction rows and task-carry bus rows are not competing authorities.
        paths = set()
        for name in (InputDispositions.filename, 'coordination.sqlite3',
                     'native_prompt_bindings.sqlite3', 'goal_history.sqlite3',
                     'goal_waits.json', 'goal_pause_events.json'):
            for suffix in ('', '-journal', '-wal', '-shm'):
                path = ROOT / (name + suffix)
                if path.exists() or path.is_symlink():
                    paths.add(path)
        for original in self.originals:
            if original.session_file is not None:
                session = Path(original.session_file)
                paths.add(session)
                for suffix in ('.input-proof', '.input-proof-journal', '.input-proof-wal', '.input-proof-shm'):
                    proof = Path(str(session) + suffix)
                    if proof.exists() or proof.is_symlink():
                        paths.add(proof)
        paths.update(path for path in RuntimeGoalFiles(ROOT).paths
                     if path.exists() or path.is_symlink())
        return frozenset(paths)

    def after_stopped(self, lifecycle):
        self.cohort.require_original()
        require_no_clients(self.audience)
        snapshot = lifecycle.registry.snapshot()
        for original in self.originals:
            if original.process_alive or snapshot.threads[original.name] != original:
                raise RuntimeError('Original owner exit/configuration proof changed')
        directory = self.receipt.with_suffix('.originals')
        directory.mkdir(mode=0o700)
        PrivateDirectoryRole.require(directory.lstat())
        paths = self.protected_files()
        protected = {str(path): digest(path) for path in sorted(paths)}
        # No decoding of old input records from a target-compaction journal.
        with ExitStack() as custody:
            runtime = custody.enter_context(RuntimeCompactionFiles(ROOT).acquire())
            goals = custody.enter_context(RuntimeGoalFiles(ROOT).acquire())
            # Freeze original membership before deriving the byte-preserved
            # partition. Goal members have their own preimage/row/DDL proof.
            unchanged = self.runtime_installation.unchanged_protected(paths, runtime).difference(goals.paths)
            invariant = {str(path): protected[str(path)] for path in unchanged}
            original_files = self.runtime_installation.retain_protected(paths, directory)
            retain_file(ROOT / 'registry.json', directory / 'registry.json')
            fsync_directory(directory)
            fsync_directory(directory.parent)
            self.note('all-original-owners-stopped-originals-audited',
                      protected_original_sha256=protected, protected_preimages=original_files,
                      registry_original_sha256=digest(directory / 'registry.json'))
            goal_installation = self.runtime_installation.synchronize_goal(
                goals, directory / 'goal-ledger')
            installed_goals = custody.enter_context(RuntimeGoalFiles(ROOT).acquire())
            # The carry and runtime member share the ORIGINAL stopped wire custody.
            self.task_carry.after_stopped(lifecycle)
            installed = self.runtime_installation.install(runtime, directory / 'runtime-compaction')
            if self.protected_files() != paths or {str(path): digest(path) for path in unchanged} != invariant:
                raise RuntimeError('Original input/native/proof/goal bytes changed; remain stopped')
            installed_goals.require_original()
            self.note('runtime-installed-protected-originals-unchanged', runtime_installation=installed,
                      goal_installation=goal_installation, byte_invariant_originals=invariant)
        self.cohort.publish(self.route_directory)
        self.note('target-route-and-defaults-published-before-retained-launch')

    def failed(self, failure):
        # Disposition completes INSIDE this operation, before its caller closes
        # the route-directory resource or exits. Installation is never retried.
        self.restore_unchanged(failure)

    def recover(self, stopped):
        """Only byte-identical originals may return to their original runtime.

        These physical proofs precede ANY original registry decoder. A committed
        #514 ledger or changed route therefore cannot be read/reverted by #508.
        The same RAM handoff and original wire OFD cross the existing child.
        """
        self.cohort.require_publication_originals()
        self.require_recovery_originals()
        # The original handoff owns fenced identity/admission, rather than a
        # raw registry hash minted only after stopped validation. Full stored
        # settings must also remain the captured originals before source restore.
        snapshot = stopped.lifecycle.registry.snapshot()
        for original in self.originals:
            if snapshot.threads[original.name] != original:
                raise RuntimeError('Original stopped configuration changed; remain stopped')

        restored = restore_stopped_batch(stopped)
        self.note('failed-install-original-runtime-restored',
                  restored=FieldCodec.encode(restored), finished=time.time())
        return restored

    def complete(self, stopped):
        results = super().complete(stopped)
        snapshot = stopped.lifecycle.registry.snapshot()
        for original, result in zip(self.originals, results, strict=True):
            current = snapshot.threads[result.thread]
            if result.thread != original.name or replace(current, process_identity=original.process_identity) != original:
                raise RuntimeError('Original settings readback differs under retained wire custody')
            RetainedOwnerLaunch.capture(current, snapshot, interpreter=str(self.cohort.target / 'bin/python'))
        self.note('retained-batch-launched-configurations-verified-public-ui-pending',
                  results=FieldCodec.encode(results), finished=time.time())
        return results

    def bind_target_launch(self, lifecycle):
        lifecycle.pin_private_nk_launch(ROOT, self.cohort.original_route.wire_root_id,
                                       self.cohort.native)


def publish(cohort: ReviewedRetainedSummaryCohort, task_carry: StoppedOwnerInstallation,
            runtime_installation: RuntimeInstallation, receipt: Path):
    """Parent-only EXECUTION entry, with the reviewed affected journey gates and carry.

    A retry never happens here. Any existing receipt/preimage refuses before
    admission, and the only stop/fence/launch implementation is the original one.
    """
    if receipt.exists() or receipt.is_symlink() or receipt.with_suffix('.originals').exists():
        raise RuntimeError('Original attempt requires review; never automatically repeat')
    cohort.require_original()
    service = Comms(ROOT, private_initial_writes=False, private_claim_writes=False)
    snapshot = service.registry.snapshot()
    owners = tuple(OwnerRestartRequest().threads(snapshot))
    if not owners:
        raise RuntimeError('Empty original audience requires review')
    audience = tuple(OwnerRestartSelection.capture(snapshot, thread.name) for thread in owners)
    for original in owners:
        original.require_idle()
        RetainedOwnerLaunch.capture(original, snapshot)
    directory = os.open(active_route_path().parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        operation = PublishRetainedSummary(cohort, audience, owners, task_carry,
                                          runtime_installation, directory, receipt)
        operation.require_selection(snapshot, owners)
        receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        PrivateDirectoryRole.require(receipt.parent.lstat())
        descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as opened:
            json.dump({'phase':'preflight-complete', 'started':time.time(),
                       'cohort':FieldCodec.encode(cohort),
                       'runtime_installation':FieldCodec.encode(runtime_installation),
                       'recovery_originals':FieldCodec.encode(operation.recovery_originals),
                       'owners_before':FieldCodec.encode(audience)}, opened, indent=2)
            opened.flush()
            os.fsync(opened.fileno())
        fsync_directory(receipt.parent)
        runtime = RestartEnvironment(path=str(cohort.target / 'bin')+':'+os.environ['PATH'],
                                     virtual_env=str(cohort.target))
        return service.owners.restart_owners(runtime=runtime, cutover=operation)
    finally:
        os.close(directory)
