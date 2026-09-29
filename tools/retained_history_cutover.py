"""ONE USE: prepare and install current retained-history snapshots; delete after use.

Only roots already declared as archived HistorySource are eligible. Live bus,
registry, native journals, UNKNOWN input outcomes and delivery state are excluded.
An original directory is retained off the manifest with all proof bytes intact.
There is no runtime reader for the predecessor publisher representation.
"""
from __future__ import annotations

import argparse
from abc import abstractmethod
from contextlib import ExitStack
from dataclasses import dataclass, fields, replace
import fcntl
import json
import os
from pathlib import Path
import shutil
from typing import Literal

from agent_comms.bus_publication import PRIVATE_WIRE_FIELD, unique_wire_object, validate_delivery_record
from agent_comms.declared_family import DeclaredFamily
from agent_comms.delivery_policy import DeliveryManifest, InitialDeliveryPolicy, KeyedResponseReceipt
from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import HistorySource
from agent_comms.messages import Message
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.store_files import _atomic_write_text, _store_lock, file_revision
from agent_comms.wire_log import WireLog
from agent_comms.wire_metadata import ArchivedAccess, WireMetadata


@dataclass(frozen=True, kw_only=True)
class RetainedPublication(DeclaredFamily, affix='RetainedPublication'):
    version: Literal[1]

    @abstractmethod
    def current(self, message: Message, root_id: str) -> dict | None: ...

    @classmethod
    def read(cls, value: dict) -> RetainedPublication:
        # This single predecessor boundary exists only in the deleted-after-use tool.
        case = next((kind for kind in cls.members_with(cls)
                     if set(value) == {field.name for field in fields(kind)}), None)
        if case is None:
            raise ValueError('Unknown retained publisher declaration; source left untouched')
        return FieldCodec.decode(case, {'kind': case.declared_name, **value})


@dataclass(frozen=True, kw_only=True)
class InitialRetainedPublication(RetainedPublication):
    initial: DeliveryManifest

    def current(self, message: Message, root_id: str) -> dict:
        policy = InitialDeliveryPolicy(version=self.version, initial=self.initial)
        value = FieldCodec.encode(policy)
        validate_delivery_record({**message.to_wire(), PRIVATE_WIRE_FIELD: value}, root_id)
        return value


@dataclass(frozen=True, kw_only=True)
class ResponseRetainedPublication(RetainedPublication):
    response: KeyedResponseReceipt

    def current(self, message: Message, root_id: str) -> None:
        # No historical audience existed. Retain the original receipt off-route;
        # the current display row carries only its identical public envelope.
        self.response.require(root_id, message)
        return None


@dataclass(frozen=True)
class PreparedSource:
    source: HistorySource
    folder: str
    bus_revision: tuple[int, int, int, int]
    registry_revision: tuple[int, int, int, int]
    metadata_revision: tuple[int, int, int, int]
    checkpoint_revision: tuple[int, int, int, int]
    rows: int
    initial_proofs: int
    response_proofs: int

    def unchanged(self) -> None:
        self.source.validate()
        root = Path(self.source.root)
        observed = tuple(file_revision(root/name) for name in
                         ('bus.jsonl','registry.json','bus_meta.json','private_bus_checkpoint.sqlite3'))
        expected = (self.bus_revision,self.registry_revision,self.metadata_revision,self.checkpoint_revision)
        if observed != expected:
            raise ValueError('Retained source changed after preparation; no cutover')


def archived_marker(root: Path) -> WireMetadata:
    marker = WireLog(root/'bus.jsonl').read_metadata_unlocked(required=True)
    if not isinstance(marker.access, ArchivedAccess):
        raise ValueError('Only explicitly archived snapshots can be cut over')
    if marker.admission_after_seq != marker.last_seq:
        raise ValueError('Archived source must have no active admission range')
    return marker


def certify_current(root: Path) -> None:
    log = WireLog(root/'bus.jsonl')
    marker = archived_marker(root)
    marker.checkpoint_version = None
    marker.checkpoint_seal = None
    (root/'private_bus_checkpoint.sqlite3').unlink(missing_ok=True)
    log.write_metadata_unlocked(marker)
    guard_path = root/'.registry-owner-guard'
    guard_path.unlink(missing_ok=True)
    guard = PrivateRegistryGuard(root/'registry.json', marker.root_id)
    guard.create_pending()
    guard.commit_initial()
    install_private_bus_checkpoint(log)
    # Same canonical installed decoder, schema and sealed barrier as every reader.
    with log.locked():
        for _ in log._verified_private_rows_unlocked(log.read_metadata_unlocked(required=True)):
            pass


def prepare(root: Path, output: Path) -> tuple[PreparedSource, ...]:
    root=root.resolve();output=output.resolve()
    if output.exists():
        raise ValueError('Preparation output must be new; do not overwrite earlier work')
    raw=(root/'history_sources.json').read_text()
    sources=tuple(FieldCodec.decode(HistorySource,v) for v in json.loads(raw))
    output.mkdir(mode=0o700,parents=True)
    (output/'original-manifest.json').write_text(raw)
    prepared=[]
    for index,source in enumerate(sources):
        source.validate()
        original=Path(source.root)
        if original.parent.resolve() != (root/'history').resolve():
            raise ValueError('Only this root’s retained snapshot directories are eligible')
        marker=archived_marker(original)
        folder=str(index)
        stage=output/folder
        shutil.copytree(original,stage)
        # Exact originals stay available to inspect all historical receipt bytes.
        proof=output/'originals'/folder
        shutil.copytree(original,proof)
        rows=initials=responses=0
        with (original/'bus.jsonl').open('rb') as src,(stage/'bus.jsonl').open('wb') as dst:
            for line in src:
                if not line.endswith(b'\n'):
                    raise ValueError('Incomplete original history; cutover refused')
                row=json.loads(line,object_pairs_hook=unique_wire_object)
                message=Message.from_wire(row)
                public={key:value for key,value in row.items() if key != PRIVATE_WIRE_FIELD}
                if public != message.to_wire():
                    raise ValueError('Original public envelope is not canonical')
                rows+=1
                private=row.get(PRIVATE_WIRE_FIELD)
                if private is not None:
                    declaration=RetainedPublication.read(private)
                    upgraded=declaration.current(message,marker.root_id)
                    row=message.to_wire()
                    if upgraded is not None:
                        row[PRIVATE_WIRE_FIELD]=upgraded;initials+=1
                    else:
                        responses+=1
                    line=json.dumps(row,ensure_ascii=False,allow_nan=False).encode()+b'\n'
                dst.write(line)
            dst.flush();os.fsync(dst.fileno())
        certify_current(stage)
        item=PreparedSource(source,folder,*(file_revision(original/name) for name in
                            ('bus.jsonl','registry.json','bus_meta.json','private_bus_checkpoint.sqlite3')),
                            rows,initials,responses)
        item.unchanged()
        prepared.append(item)
    (output/'prepared.json').write_text(json.dumps([FieldCodec.encode(p) for p in prepared]))
    return tuple(prepared)


def apply(root: Path, output: Path) -> tuple[HistorySource, ...]:
    root=root.resolve();output=output.resolve()
    prepared=tuple(FieldCodec.decode(PreparedSource,v) for v in json.loads((output/'prepared.json').read_text()))
    manifest=root/'history_sources.json'
    originals=(output/'original-manifest.json').read_text()
    # Preparation and all decoder/proof validation finish before this transaction.
    with _store_lock(manifest), ExitStack() as locks:
        if manifest.read_text() != originals:
            raise ValueError('History manifest changed after preparation')
        for item in prepared:item.unchanged()
        staged=[]
        for item in prepared:
            source=Path(item.source.root)
            replacement=source.with_name(source.name+'-current-prepared')
            retained=source.with_name(source.name+'-retained-original')
            if replacement.exists() or retained.exists():
                raise ValueError('One-use destination already exists; refuse repeat')
            shutil.copytree(output/item.folder,replacement)
            # Final filesystem inodes are sealed here; directory rename does not
            # change child inode revisions. No live root allocator is involved.
            certify_current(replacement)
            # Acquire the existing archive leaf without its predecessor read
            # barrier; creation follows the canonical owner's lock semantics.
            descriptor=os.open(source/'.bus.jsonl.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            lock = locks.enter_context(os.fdopen(descriptor,'a+b'))
            fcntl.flock(lock,fcntl.LOCK_EX)
            item.unchanged()
            staged.append(SnapshotCutover(item,source,replacement,retained))
        installed=[]
        activated=[]
        try:
            for snapshot in staged:
                snapshot.activate()
                activated.append(snapshot)
                installed.append(snapshot.binding())
            for snapshot in activated:snapshot.sync_directory()
            _atomic_write_text(manifest,json.dumps([FieldCodec.encode(s) for s in installed]),fsync_parent=True)
        except BaseException as error:
            failures=[error]
            for snapshot in reversed(activated):
                try:snapshot.rollback()
                except BaseException as rollback_error:failures.append(rollback_error)
            try:_atomic_write_text(manifest,originals,fsync_parent=True)
            except BaseException as rollback_error:failures.append(rollback_error)
            if len(failures)>1:
                raise BaseExceptionGroup('Cutover failed; retained originals require operator inspection',failures)
            raise
    return tuple(installed)


@dataclass(frozen=True)
class SnapshotCutover:
    """One archived directory owns activation, rollback and its new binding."""
    prepared: PreparedSource
    source: Path
    replacement: Path
    retained: Path

    def activate(self) -> None:
        os.rename(self.source,self.retained)
        try:os.rename(self.replacement,self.source)
        except BaseException:
            os.rename(self.retained,self.source)
            raise

    def rollback(self) -> None:
        os.rename(self.source,self.replacement)
        os.rename(self.retained,self.source)
        self.sync_directory()

    def sync_directory(self) -> None:
        descriptor=os.open(self.source.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)

    def binding(self) -> HistorySource:
        return replace(self.prepared.source,
            snapshot_bus_revision=file_revision(self.source/'bus.jsonl'),
            snapshot_registry_revision=file_revision(self.source/'registry.json'))


class CutoverOperation(DeclaredFamily,affix='CutoverOperation'):
    @staticmethod
    @abstractmethod
    def run(root: Path,output: Path) -> tuple: ...


class PrepareCutoverOperation(CutoverOperation):
    run=staticmethod(prepare)


class ApplyCutoverOperation(CutoverOperation):
    run=staticmethod(apply)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=CutoverOperation.names())
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    operation=CutoverOperation.decode(args.operation)
    result=operation.run(args.root,args.output)
    print(json.dumps({'sources':len(result)}))


if __name__=='__main__':main()
