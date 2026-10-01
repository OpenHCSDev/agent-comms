"""Declared native commit/reconcile commands and their retained intent projection.

These are requests, not owner grants. The writer supplies the inherited-fd
attestation at its only serialization boundary; the native helper rechecks it.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

from .compaction_records import CompactionOperation
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .owner_compaction_prepare import NativeWitness
from .pi_summary_payloads import SummaryFiles, SummaryUsage
from .text_digest import TextDigest

if TYPE_CHECKING:
    from .compaction_identity import SelectedCommitReference
    from .compaction_source import CompactionSource
    from .owner_compaction_gate import OwnerCompactionAttestation


@dataclass(frozen=True)
class NativeAuthority:
    parent_pid: int = field(metadata={"wire_name": "parentPid"})
    device: str
    inode: str

    @classmethod
    def capture(cls, fd: int) -> NativeAuthority:
        held = os.fstat(fd)
        return cls(os.getpid(), str(held.st_dev), str(held.st_ino))


@dataclass(frozen=True)
class NativeCommitIdentity:
    commit_id: str = field(metadata={"wire_name": "commitId"})
    payload_digest: str = field(metadata={"wire_name": "payloadDigest"})
    metadata_digest: str = field(metadata={"wire_name": "metadataDigest"})


@dataclass(frozen=True, kw_only=True)
class NativeRequest(DeclaredFamily, affix="NativeRequest"):
    family_discriminator = "action"
    witness: NativeWitness
    commit: NativeCommitIdentity


@dataclass(frozen=True, kw_only=True)
class NativeSummaryPayload:
    summary: str
    tokens_before: int = field(metadata={"wire_name": "tokensBefore"})
    details: SummaryFiles | None = field(default=None, metadata={"wire_omit_default": True})
    usage: SummaryUsage | None = field(default=None, metadata={"wire_omit_default": True})

    def __post_init__(self):
        if not self.summary.strip() or not 0 <= self.tokens_before <= 2**53 - 1:
            raise ValueError("Bounded native compaction payload required")

    def payload_digest(self, witness: NativeWitness) -> str:
        return TextDigest.of(json.dumps(
            [self.summary, witness.first_kept_entry_id, self.tokens_before],
            ensure_ascii=False, separators=(",", ":"),
        )).value

    def metadata_digest(self) -> str:
        # Existing Python/native marker contract: UTF-8 path hex, IEEE754 cost hex.
        metadata = [
            self.details.commit_metadata() if self.details is not None else None,
            self.usage.commit_metadata() if self.usage is not None else None,
        ]
        return hashlib.sha256(
            b"agent-comms-metadata-v1\n" + json.dumps(metadata, separators=(",", ":")).encode("ascii")
        ).hexdigest()

    def request(self, witness: NativeWitness, identity: NativeCommitIdentity) -> CommitNativeRequest:
        return CommitNativeRequest(
            witness=witness, commit=identity,
            **{item.name: getattr(self, item.name) for item in fields(self)},
        )


@dataclass(frozen=True, kw_only=True)
class CommitNativeRequest(NativeSummaryPayload, NativeRequest):
    pass


@dataclass(frozen=True, kw_only=True)
class ReconcileNativeRequest(NativeRequest):
    pass


@dataclass(frozen=True)
class NativeIntent:
    """Native writer's declared projection of the journal-owned intent.

    Owner/source/selected data remain under their actual authorities. No decoding
    by union shape and no revalidation of raw keys throughout reconciliation.
    """
    witness: NativeWitness
    payload_digest: str = field(metadata={"wire_name": "payloadDigest"})
    metadata_digest: str = field(metadata={"wire_name": "metadataDigest"})

    def journal_json(
        self, owner: OwnerCompactionAttestation, source: CompactionSource,
        selected: SelectedCommitReference | None = None,
    ) -> str:
        """Retain the original source view alongside the declared native intent."""
        record = dict(
            FieldCodec.encode(self), owner=FieldCodec.encode(owner),
            source=FieldCodec.project(source, "journal"),
        )
        if selected is not None:
            record.update(FieldCodec.encode(selected))
        return self.frame_record(record)

    @classmethod
    def frame_record(cls, record: dict[str, Any]) -> str:
        from .retained_task_facts import RetainedTaskFacts

        return RetainedTaskFacts.frame_journal(
            record, payload_path=("source", "retained")
        )

    @classmethod
    def read(cls, operation: CompactionOperation) -> NativeIntent:
        raw = FieldCodec.decode(dict[str, Any], json.loads(operation.intent_json))
        cls.frame_record(raw)
        return FieldCodec.decode(cls, {wire: raw[wire] for _, wire in FieldCodec._fields(cls)})

    def identity(self, commit_id: str) -> NativeCommitIdentity:
        return NativeCommitIdentity(commit_id, self.payload_digest, self.metadata_digest)

    def reconciliation(self, commit_id: str) -> ReconcileNativeRequest:
        return ReconcileNativeRequest(witness=self.witness, commit=self.identity(commit_id))
