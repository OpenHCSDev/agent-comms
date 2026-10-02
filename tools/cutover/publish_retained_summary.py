"""Future retained-summary installation through the original stopped batch.

The release owner supplies reviewed immutable artifacts and the typed task carry
member. This module contains no guessed target, automatic approval, retry, client
signal, native input or alternative owner-stop/launch implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Annotated

from agent_comms.active_route import ActiveRoute, active_route_path, read_active_route, _publish_active_route_locked
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
class PackageDirectUrl:
    url: str
    vcs_info: PackageVcsInfo


@dataclass(frozen=True)
class InstalledSource:
    module: str
    head: str
    location: str
    files: int
    python_files: int
    byte_equal: bool
    direct_url: PackageDirectUrl
    inventory_sha256: str


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
            if not source.byte_equal or source.direct_url.vcs_info.commit_id != source.head:
                raise RuntimeError('Unverified source/native proof')
        if not self.native_full_trust or self.source_overlay or self.dependency_bypass:
            raise RuntimeError('Package/source/native trust is incomplete')


@dataclass(frozen=True)
class ReviewedRetainedSummaryCohort:
    target: Annotated[Path, PathText]
    source_interpreter: Annotated[Path, PathText]
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
        if not self.source_interpreter.is_absolute() or not self.source_interpreter.is_file():
            raise RuntimeError('Authentic source interpreter is required')
        self.activation.require_original()
        self.source_proof.require_original()
        if self.activation.path != self.target / 'activation.json':
            raise RuntimeError('Activation is not the selected immutable target')
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
        if len(gates) < 2 or gates.intersection((self.activation.path, self.source_proof.path)):
            raise RuntimeError('Distinct reviewed actual installed journey gates are required')
        for gate in self.actual_gates:
            gate.require_original()
        verify_native_package(self.native)
        self.require_publication_originals()

    def require_publication_originals(self):
        if read_active_route() != self.original_route:
            raise RuntimeError('Original active route changed; recapture/review required')
        for command in COMMANDS:
            if (LINKS / command).readlink() != self.current_prefix / 'bin' / command:
                raise RuntimeError('Original default changed; recapture/review required')
            if not (self.target / 'bin' / command).is_file():
                raise RuntimeError('Reviewed target entrypoint is missing')
            temporary = LINKS / (command + '.retained-summary-publish')
            if temporary.exists() or temporary.is_symlink():
                raise RuntimeError('Original publication attempt requires review')

    def publish(self, directory: int):
        self.require_publication_originals()
        target_route = replace(self.original_route, native_package=self.native)
        _publish_active_route_locked(target_route, active_route_path(), directory,
                                     expected=self.original_route)
        for command in COMMANDS:
            link = LINKS / command
            if link.readlink() != self.current_prefix / 'bin' / command:
                raise RuntimeError('Default changed during publication; remain stopped')
            temporary = LINKS / (command + '.retained-summary-publish')
            temporary.symlink_to(self.target / 'bin' / command)
            temporary.replace(link)
        fsync_directory(LINKS)
        if read_active_route() != target_route:
            raise RuntimeError('Target route readback differs')


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
        self.after_stopped(stopped.lifecycle)
        results = stopped.launch()
        snapshot = stopped.lifecycle.registry.snapshot()
        for original, result in zip(self.originals, results, strict=True):
            current = snapshot.threads[result.thread]
            if result.thread != original.name or replace(current, process_identity=original.process_identity) != original:
                raise RuntimeError('Original settings readback differs under retained wire custody')
            RetainedOwnerLaunch.capture(current, snapshot, interpreter=str(self.cohort.target / 'bin/python'))
        self.note('retained-batch-launched-configurations-verified-public-ui-pending',
                  results=FieldCodec.encode(results), finished=time.time())
        return results


def publish(cohort: ReviewedRetainedSummaryCohort, task_carry: StoppedOwnerInstallation,
            runtime_installation: RuntimeInstallation, receipt: Path):
    """Parent-only EXECUTION entry, once both reviewed gates/carry are supplied.

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
        RetainedOwnerLaunch.capture(original, snapshot, interpreter=str(cohort.source_interpreter))
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
        service.owners.pin_private_nk_launch(ROOT, cohort.original_route.wire_root_id, cohort.native)
        runtime = RestartEnvironment(path=str(cohort.target / 'bin')+':'+os.environ['PATH'],
                                     virtual_env=str(cohort.target))
        return service.owners.restart_owners(runtime=runtime,
            source_interpreter=str(cohort.source_interpreter), cutover=operation)
    finally:
        os.close(directory)
