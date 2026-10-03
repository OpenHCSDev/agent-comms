"""Native writer/owner/ingress lock lifetime and its captured source authority."""
from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .compaction_source import CompactionSource
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .input_disposition import FutureInputQueue, InputDispositions
from .native_entries import NativeEvidenceRead
from .owner_compaction_gate import OwnerCompactionAttestation
from .owner_compaction_prepare import NativeWitness
from .private_bus_checkpoint import CertifiedSourceRead
from .registration import Registration
from .retained_task_facts import ExactTaskFact, RetainedTaskFacts
from .session_fence import idle_session_writer_fence
from .store_files import _store_lock
from .text_digest import TextDigest
from .threads import Thread
from .wire_log import WireLog


@dataclass(frozen=True)
class CompactionBoundary:
    registry: Registration
    inputs: InputDispositions
    future_queue: FutureInputQueue | None = None
    native_reader: NativeEvidenceRead | None = field(default=None, kw_only=True)

    @property
    def root(self) -> Path:
        return self.registry.store.path.parent.resolve(strict=True)

    def retained_history(self, owner: Thread, *, diff: bool = False):
        """Inspect original attempts without constructing a journal writer."""
        from .compaction_journal import CompactionJournal

        try:
            session_file = owner.require_saved_session()
        except ValueError as error:
            return dict(available=False, reason=str(error),
                        scope="no canonical saved session; native facts cannot be inferred")
        path = self.root / "compaction-commits.sqlite3"
        if diff:
            return CompactionJournal.retained_changes(path, session_file)
        return CompactionJournal.retained_history(path, session_file)

    def inspect(self, name: str):
        """Observe current sources and original compaction cuts separately.

        The current wire/registry/input cut is certified by its existing reader.
        The journal has its own read transaction. Neither cut is an owner/writer
        grant; future input queue membership and native branch/cut selection are
        exclusively determined by hold/capture during actual compaction.
        """
        from .retained_context import RetainedSegment

        with WireLog(self.root / "bus.jsonl").retained_sources(name, self.registry) as (
            owner, snapshot, facts, inputs, export
        ):
            pinned = facts.original_input_facts(inputs)
            authored = RetainedTaskFacts((*facts.facts, *pinned)).for_owner(owner, snapshot)
            segment = RetainedSegment.capture(authored, owner, snapshot, export)
            keys = tuple(dict.fromkeys((*inputs.owner_originals(owner),
                                        *(row.key for fact in pinned for row in fact.input_sources()))))
            originals = inputs.retained_task_facts(keys)
            observed = RetainedTaskFacts((*facts.facts, *owner.retained_task_facts(),
                                         *originals)).for_owner(owner, snapshot)
        return dict(segment.inspection(),
                    observations=dict(
                        scope="current certified wire/registry/input read; durable owner inputs and pinned originals including queued and UNKNOWN; not a compaction admission",
                        facts=FieldCodec.encode(observed),
                    ),
                    compaction=self.retained_history(owner),
                    native_source_contract=(
                        "NativeWitness.retained_task_facts reads only its exact session, leaf and file revision; "
                        "NativeEvidenceRead preserves original request/result entry pairs. Current native leaf, "
                        "uncaptured tool results and filesystem state are not inferred by this inspection. "
                        "Original attempt requests/intents retain the facts captured by HeldCompaction; "
                        "operation and summary states retain failures and UNKNOWN without replay."
                    ))

    @contextmanager
    def hold(
        self,
        owner: Thread,
        owner_generation: int,
        witness: NativeWitness,
        *,
        settled: bool = True,
        pending_input_keys: tuple[str, ...] = (),
    ) -> Iterator[HeldCompaction]:
        expected = owner.compaction_attestation(owner_generation, witness)
        # Existing bus publication acquires bus BEFORE registry. Never invert
        # that edge, even though this scope does not yet publish an outcome.
        with idle_session_writer_fence(expected.session_file) as executor_fd:
            # Read the original journal before taking any bus/registry/input
            # locks. The existing writer fence retains this exact source cut.
            native_facts = witness.retained_task_facts(self.native_reader)
            with (
                _store_lock(self.root / "wire") as wire_lock,
                _store_lock(self.root / "bus.jsonl") as bus_lock,
                self.registry.guard_owner_compaction(owner, expected) as (receipt, fd),
                self.inputs.locked() as input_fd,
            ):
                if settled:
                    self.inputs._read_unlocked().compaction_rows(
                        owner, pending_input_keys, self.future_queue
                    )
                yield HeldCompaction(
                    self, witness, receipt, fd,
                    (executor_fd, wire_lock.descriptor, bus_lock.descriptor, input_fd),
                    native_facts, bus_lock.certified_read(),
                )


    @staticmethod
    def ingress_revision(path: Path) -> str:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return "missing"
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RelationViolationError("Compaction ingress must be regular storage")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return (
            f"{info.st_dev}:{info.st_ino}:{info.st_size}:"
            f"{info.st_mtime_ns}:{info.st_ctime_ns}:{digest}"
        )


    @classmethod
    def settings_revision(cls, paths: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if paths is None:
            return None
        if not paths or any(not Path(path).is_absolute() for path in paths):
            raise RelationViolationError("Exact effective settings paths required")
        states = []
        for path in paths:
            file = Path(path)
            try:
                info = file.lstat()
            except FileNotFoundError:
                states.append("missing")
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1048576:
                raise RelationViolationError("Unsafe effective settings source")
            states.append(cls.ingress_revision(file))
        return tuple(states)


@dataclass(frozen=True)
class HeldCompaction:
    """Borrowed only inside CompactionBoundary.hold; never a serializable grant."""
    boundary: CompactionBoundary
    witness: NativeWitness
    receipt: OwnerCompactionAttestation
    authority_fd: int
    retained_fds: tuple[int, ...]
    native_facts: tuple[ExactTaskFact, ...]
    wire_source: CertifiedSourceRead

    def capture(
        self,
        pending_input_keys: tuple[str, ...],
        settings_paths: tuple[str, ...] | None,
    ) -> CompactionSource:
        root = self.boundary.root.stat()
        self.witness.require_current_file(Path(self.witness.session_file))
        snapshot = self.boundary.registry.store._read_unlocked().snapshot()
        owner = snapshot.threads[self.receipt.thread]
        inputs = self.boundary.inputs._read_unlocked()
        rows, input_facts = inputs.compaction_material(
            owner, pending_input_keys, self.boundary.future_queue
        )
        facts = tuple(self.wire_source.retained_task_facts(owner.incarnation))
        facts += owner.retained_task_facts()
        facts += input_facts
        facts += self.native_facts
        return CompactionSource(
            self.witness,
            f"{self.boundary.root}:{root.st_dev}:{root.st_ino}",
            self.receipt.thread,
            self.receipt.owner_generation,
            self.receipt.turn_id,
            self.receipt.goal_id,
            self.receipt.goal_revision,
            TextDigest.of(json.dumps(FieldCodec.encode(rows), sort_keys=True)).value,
            RetainedTaskFacts(facts).for_owner(owner, snapshot),
            pending_inputs=inputs.original_provenances(pending_input_keys),
            settings_paths=settings_paths,
            settings_revision=self.boundary.settings_revision(settings_paths),
        )
