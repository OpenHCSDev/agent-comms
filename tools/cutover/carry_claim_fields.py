"""Give retained claim fields their declared names, once, under install maintenance.

The old spelling is selected by the record declaration, never by the JSON value.
Opaque provider JSON is not a record and is never searched for matching strings.
Delete this step after every retained root has crossed this format change.
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from agent_comms.acp_extension import LiveCursorScope, LiveQueueScope
from agent_comms.envelope_claim_transitions import AcquiringClaim, RecordedScope
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import (
    FieldCodec,
    InlineFieldPlan,
    InlineRecord,
    MalformedRecord,
    RecordMember,
    SingleFieldValue,
    SubclassUnion,
    UnionDecoder,
    ValueDecoder,
    _ArrayDecoder,
    _DictDecoder,
    _FamilyRecordDecoder,
    _RecordDecoder,
    _RepresentedDecoder,
    _StringKeyDictDecoder,
    _PairKeyDictDecoder,
    compile_decoder,
)
from agent_comms.goal_failure_observation import FailedTurnEvidence
from agent_comms.historical_views import HistoryArchive
from agent_comms.input_attempt import StoredInput
from agent_comms.input_origin import HumanInputOrigin
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import JsonDocument, RegistryFile, RootFile, SQLiteFile
from agent_comms.mro_dispatch import MroDispatch, handles
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.queued_input import QueuedInputContext
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore
from agent_comms.restart_queue import DIRECTORY, QueuedRestart
from agent_comms.selected_write_plan import (
    SelectedWriteBinding,
    SelectedWriteIntent,
    SelectedWritePlans,
)
from agent_comms.store_files import _atomic_write_text, _store_lock
from agent_comms.thread_identity import ClaimedIdentity, TurnFence
from agent_comms.turn_lease import ActiveTurn
from agent_comms.typed_table import HeldBusFile, JsonStorage, TypedTable, _identifier

from .carry_bus_history import CarryCatalogSource
from .carry_context_request_key import ContextRequestKey
from .carry_owner_requests import OwnerRequests
from .carry_turn_owner import TurnOwner


@dataclass(frozen=True)
class RenamedField:
    owner: type
    name: str
    old: str


# These are the retired declarations. Current spelling, JSON layout, SQL storage,
# descendants and consumers all come from FieldCodec/TypedTable, not this list.
RENAMED = (
    RenamedField(LiveCursorScope, "claimed_identity", "admission"),
    RenamedField(LiveQueueScope, "claimed_identity", "admission"),
    RenamedField(HumanInputOrigin, "claimed_identity", "admission"),
    RenamedField(QueuedInputContext, "claimed_identity", "admission"),
    RenamedField(SelectedWriteBinding, "claimed_identity", "admission"),
    RenamedField(AcquiringClaim, "resource_scope", "admission"),
    RenamedField(RecordedScope, "owner_claimed_generation", "owner_admission_generation"),
    RenamedField(StoredInput, "claim_generation", "admission"),
    RenamedField(FailedTurnEvidence, "claim_generation", "admission"),
    RenamedField(ClaimedIdentity, "claimed_generation", "admission_generation"),
    RenamedField(TurnFence, "claimed_generation", "admission_generation"),
    RenamedField(ActiveTurn, "claimed_generation", "admission_generation"),
    RenamedField(OwnerRestartSelection, "claimed_generation", "admission_generation"),
    RenamedField(SelectedWriteIntent, "claim_epoch", "admission_epoch"),
    RenamedField(RegistryDocument, "claims", "admissions"),
    RenamedField(NativeRuntimeInput, "sent_owner_claimed_generation", "sent_owner_admission_generation"),
    RenamedField(CurrentNativeCursor, "owner_claimed_generation", "owner_admission_generation"),
)


@cache
def renames(record: type) -> dict[str, str]:
    return {item.name: item.old for item in RENAMED if issubclass(record, item.owner)}


class ClaimFieldNames(MroDispatch):
    """Walk the existing codec's plans; only the retired field names change."""

    @staticmethod
    def represented(plan: _RepresentedDecoder) -> ValueDecoder:
        owner = plan.representation
        if issubclass(owner, RecordMember):
            return _RecordDecoder(owner)
        if issubclass(owner, SingleFieldValue):
            return owner.wire_field().value
        if issubclass(owner, SubclassUnion):
            return ValueDecoder.for_members(owner.members_with(owner))
        if issubclass(owner, InlineRecord):
            return _RecordDecoder(owner.wire_record())
        return plan.structural

    def children(self, plan: ValueDecoder) -> tuple[ValueDecoder, ...]:
        return next(self.handlers_for(plan))(plan, None)

    @cache
    def touches(self, plan: ValueDecoder, seen: frozenset[object] = frozenset()) -> bool:
        if plan in seen:
            return False
        if isinstance(plan, _RecordDecoder) and renames(plan.target):
            return True
        return any(self.touches(child, seen | {plan}) for child in self.children(plan))

    def rewrite(self, plan: ValueDecoder, raw: object) -> object:
        if raw is None or not self.touches(plan):
            return raw
        return next(self.handlers_for(plan))(plan, raw)

    @handles(ValueDecoder)
    def opaque(self, plan: ValueDecoder, raw: object) -> object:
        return () if raw is None else raw

    @handles(_RepresentedDecoder)
    def representation(self, plan: _RepresentedDecoder, raw: object) -> object:
        child = self.represented(plan)
        return (child,) if raw is None else self.rewrite(child, raw)

    @handles(_FamilyRecordDecoder)
    def family(self, plan: _FamilyRecordDecoder, raw: object) -> object:
        if raw is None:
            return tuple(_RecordDecoder(member) for member in plan.target.members_with(plan.target))
        member, _ = plan.member(raw)
        return self.rewrite(_RecordDecoder(member), raw)

    @handles(_RecordDecoder)
    def record(self, plan: _RecordDecoder, raw: object) -> object:
        entries = tuple(entry for entry in FieldCodec.field_plans(plan.target)
                        if entry.records and entry.declared.init)
        if raw is None:
            return tuple(entry.value for entry in entries)
        if not isinstance(raw, dict):
            raise MalformedRecord(f"Expected {plan.target.__name__} record")
        result = dict(raw)
        keys = renames(plan.target)
        for entry in entries:
            old = keys.get(entry.name)
            if old is not None and old in result:
                if entry.key in result:
                    raise RelationViolationError(
                        f"{plan.target.__name__} contains both {old} and {entry.key}")
                result = {entry.key if key == old else key: value
                          for key, value in result.items()}
            if isinstance(entry, InlineFieldPlan):
                result = self.rewrite(entry.value, result)
            elif entry.key in result:
                result[entry.key] = self.rewrite(entry.value, result[entry.key])
        return result

    @handles(UnionDecoder)
    def union(self, plan: UnionDecoder, raw: object) -> object:
        if raw is None:
            return plan.alternatives
        answers = []
        for alternative in plan.alternatives:
            try:
                changed = self.rewrite(alternative, raw)
                alternative.decode_bound(changed, {})
            except MalformedRecord:
                continue
            if changed not in answers:
                answers.append(changed)
        if len(answers) != 1:
            raise RelationViolationError(
                f"Claim field carry cannot select exactly one declared {plan.target}: "
                f"{len(answers)} different results")
        return answers[0]

    @handles(_ArrayDecoder)
    def array(self, plan: _ArrayDecoder, raw: object) -> object:
        if raw is None:
            return plan.items
        return [self.rewrite(child, item) for child, item in plan.plan_items(raw)]

    @handles(_DictDecoder)
    def mapping(self, plan: _DictDecoder, raw: object) -> object:
        return (plan.key, plan.value)

    @handles(_StringKeyDictDecoder)
    def string_mapping(self, plan: _StringKeyDictDecoder, raw: object) -> object:
        if raw is None:
            return self.mapping(plan, raw)
        return {key: self.rewrite(plan.value, item) for key, item in plan.entries(raw)}

    @handles(_PairKeyDictDecoder)
    def pair_mapping(self, plan: _PairKeyDictDecoder, raw: object) -> object:
        if raw is None:
            return self.mapping(plan, raw)
        return [[self.rewrite(plan.key, key), self.rewrite(plan.value, item)]
                for key, item in plan.entries(raw)]


WIRE_NAMES = ClaimFieldNames()


class ClaimFields(Carry):
    precedes = (OwnerRequests, TurnOwner, ContextRequestKey)

    @staticmethod
    def documents(root: Path, files: dict[Path, type[RootFile]]) -> dict[Path, object]:
        documents = {path: owner.record_type for path, owner in files.items()
                     if issubclass(owner, JsonDocument) and not issubclass(owner, RegistryStore)}
        documents.update((path, SelectedWriteIntent) for path in SelectedWritePlans.matching_in(
            root / SelectedWritePlans.root_directory))
        documents.update((path, QueuedRestart) for path in (root / DIRECTORY).rglob("*.json"))
        return documents

    def roots(self) -> tuple[Path, ...]:
        return (self.root, *(source.mapped_root(self.root, self.original_root)
                             for source in CarryCatalogSource.read(self.root, self.original_root)))

    def backup_files(self) -> tuple[Path, ...]:
        paths = set()
        for root in self.roots():
            files = RootFile.existing_in(root)
            paths.update(path for path, owner in files.items() if issubclass(owner, SQLiteFile))
            paths.update(self.documents(root, files))
            paths.update(RegistryStore.at(root).written)
        paths.add(HistoryArchive.path_in(self.root))
        for path in paths:
            self.require_confined(self.root, path)
        return tuple(sorted(path for path in paths if path.exists()))

    def carry(self, *, apply: bool) -> Carried:
        if apply:
            HeldBusFile(RegistryFile, RegistryFile.path_in(self.root)).require()
        rewritten = []
        changed = 0
        visitor = WIRE_NAMES
        with _store_lock(HistoryArchive.path_in(self.root)):
            sources = CarryCatalogSource.read(self.root, self.original_root)
            for root in self.roots():
                registry = RegistryStore.at(root)
                if registry.path.exists():
                    with registry.locked():
                        registry.private_guard_unlocked()
                        raw = json.loads(registry.path.read_bytes())
                        current = visitor.rewrite(compile_decoder(RegistryDocument), raw)
                        if current != raw:
                            changed += 1
                            if apply:
                                registry._write_unlocked(json.dumps(current, indent=2))
                                rewritten.extend(str(path.relative_to(self.root))
                                                 for path in registry.written)
                files = RootFile.existing_in(root)
                for path, owner in files.items():
                    self.require_confined(self.root, path)
                    if issubclass(owner, SQLiteFile):
                        count = self.database(path, visitor, apply=apply)
                        changed += count
                        if count and apply:
                            rewritten.append(str(path.relative_to(self.root)))
                for path, declared in self.documents(root, files).items():
                    self.require_confined(self.root, path)
                    plan = compile_decoder(declared)
                    if not visitor.touches(plan):
                        continue
                    with _store_lock(path):
                        raw = json.loads(path.read_bytes())
                        current = visitor.rewrite(plan, raw)
                        count = int(current != raw)
                        if count and apply:
                            _atomic_write_text(path, json.dumps(current))
                    changed += count
                    if count and apply:
                        rewritten.append(str(path.relative_to(self.root)))
            if apply and rewritten:
                rebound = tuple(source.carried_registry(source.mapped_root(self.root, self.original_root))
                                for source in sources)
                if rebound:
                    _atomic_write_text(HistoryArchive.path_in(self.root),
                                       json.dumps(FieldCodec.encode(rebound)))
                    rewritten.append(HistoryArchive.filename)
        return Carried(changed, rewritten=tuple(rewritten))

    @staticmethod
    def database(path: Path, visitor: ClaimFieldNames, *, apply: bool) -> int:
        # Read only typed JSON columns that can contain these declarations. The
        # multi-gigabyte message bodies are neither copied into RAM nor decoded.
        pending = []
        rebuilds = []
        changed = 0
        with closing(SQLiteFile.read_snapshot(path)) as db:
            tables = tuple(row for row in TypedTable.members_with(TypedTable)
                           if row.stored_columns(db))
            for row in tables:
                stored = row.stored_columns(db)
                renamed = {field.name: _identifier(old)
                           for field in row._fields()
                           if field.column.record_key
                           and (old := renames(row).get(field.name)) in stored}
                if any(field.wire_name in stored for field in row._fields()
                       if field.name in renamed):
                    raise RelationViolationError(f"{row.__name__} contains old and new claim columns")
                json_fields = tuple(field for field in row._fields()
                                    if issubclass(field.storage, JsonStorage) and field.wire_name in stored
                                    and visitor.touches(field.value))
                primary = tuple(field for field in row._fields() if field.column.primary_key)
                if json_fields:
                    selected = (*primary, *json_fields)
                    columns = ",".join(renamed.get(field.name, _identifier(field.wire_name))
                                       for field in selected)
                    where = " AND ".join(
                        f"{renamed.get(field.name, _identifier(field.wire_name))} IS ?"
                        for field in primary)
                    if not primary:
                        raise RelationViolationError(f"{row.__name__} has no declared row key")
                    with closing(db.execute(f"SELECT {columns} FROM {_identifier(row.declared_name)}")) as rows:
                        for values in rows:
                            keys, payloads = values[:len(primary)], values[len(primary):]
                            updates = {}
                            for field, value in zip(json_fields, payloads, strict=True):
                                if value is None:
                                    continue
                                raw = json.loads(value)
                                current = visitor.rewrite(field.value, raw)
                                if current != raw:
                                    updates[field.wire_name] = json.dumps(current)
                            if updates:
                                assignments = ",".join(f"{_identifier(key)}=?" for key in updates)
                                updates_for_row = (f"UPDATE {_identifier(row.declared_name)} SET {assignments} WHERE {where}",
                                                   (*updates.values(), *keys))
                                # Keep only changed rows, not the scanned source.
                                pending.append(updates_for_row)
                                changed += 1
                if renamed:
                    rebuilds.append((row, renamed))
                    changed += 1
        if not apply or not changed:
            return changed
        with _store_lock(path), closing(SQLiteFile.writer(path, isolation_level=None)) as db:
            triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
            db.execute("PRAGMA foreign_keys=OFF")
            with SQLiteFile.write_transaction(db):
                for name, _ in triggers:
                    db.execute(f"DROP TRIGGER {_identifier(name)}")
                for query, arguments in pending:
                    db.execute(query, arguments)
                for row, renamed in rebuilds:
                    row.rebuild(db, renamed)
                present = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                for name, sql in triggers:
                    if name not in present:
                        db.execute(sql)
                if problems := db.execute("PRAGMA foreign_key_check").fetchall():
                    raise RelationViolationError(f"Claim carry broke references in {path}: {problems}")
            return changed
