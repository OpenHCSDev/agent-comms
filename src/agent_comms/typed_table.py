"""SQLite representation and access derived from frozen row declarations.

StoreWriter owns transaction completion and committed-change publication.
TypedStore installs the declared schema on a fresh store; readers accept exactly
the declared columns and field types without upgrading existing storage.
"""

from __future__ import annotations

import annotationlib
import hashlib
import json
import os
import re
import sqlite3
import types
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from enum import Enum, IntEnum, IntFlag
from functools import lru_cache
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    ClassVar,
    Literal,
    NoReturn,
    Self,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    overload,
)

from .declared_family import DeclaredFamily, declared_words
from .errors import RelationViolationError
from .field_codec import (
    FieldCodec,
    FieldDirective,
    FieldPlan,
    MalformedRecord,
    NullMember,
    ValueDecoder,
    WireName,
    WireValue,
    compile_decoder,
)
from .lifecycle import LifecycleState
from .locked_store import (
    DurableFile,
    FileStore,
    PublishedSQLiteFile,
    RegistryFile,
    RootFile,
    RuntimeFile,
    SQLiteFile,
    StoreBusy,
    StoreCommittedError,
)
from .maintenance_barrier import HOLDER_NONCE, MaintenanceBarrier
from .package_stamp import STAMP
from .store_files import _store_lock

if TYPE_CHECKING:
    from .execution_states import ExecutionState

RelatedRow = TypeVar("RelatedRow", bound="TypedRow")


class RowNotFoundError(MalformedRecord):
    """A required lookup found no row; malformed or duplicate rows remain corrupt."""


def _identifier(name: str) -> str:
    if not name or not name.isascii() or not name.replace("_", "a").isalnum():
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f'"{name}"'


def _absent(member: object) -> bool:
    """SQL NULL stores this union member: None, or a declared null member."""
    return member is types.NoneType or (isinstance(member, type) and issubclass(member, NullMember))


def _base_type(annotation: object) -> tuple[object, bool]:
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) in (Union, types.UnionType):
        members = get_args(annotation)
        if len(members) == 2 and sum(map(_absent, members)) == 1:
            return next(member for member in members if not _absent(member)), True
    return annotation, False


class SqlStorage(DeclaredFamily, affix="Storage"):
    """Each SQLite representation owns its conversion at the storage boundary."""

    sql_type: ClassVar[str]

    @classmethod
    @abstractmethod
    def accepts(cls, annotation: object) -> bool: ...

    @staticmethod
    def _choice_constraint(column: str, values: tuple) -> str:
        choices = ", ".join(
            (
                "'" + value.replace("'", "''") + "'"
                if isinstance(value, str)
                else str(int(value))
                if isinstance(value, bool)
                else str(value)
            )
            for value in values
        )
        return f"{_identifier(column)} IN ({choices})"

    @classmethod
    def family_constraints(cls, column: str, annotation: object) -> tuple[str, ...]:
        """The generated selector CHECK, also consumed by stopped DDL carries."""
        if compile_decoder(annotation).uses_representation:
            return ()
        declared, _ = _base_type(annotation)
        if get_origin(declared) is not type:
            return ()
        members = get_args(declared)
        if not members or not issubclass(members[0], DeclaredFamily):
            return ()
        return (cls._choice_constraint(column, members[0].names()),)

    @classmethod
    def constraints(cls, column: str, annotation: object = None) -> tuple[str, ...]:
        family = cls.family_constraints(column, annotation)
        if family:
            return family
        declared, _ = _base_type(annotation)
        if get_origin(declared) is Literal:
            values = get_args(declared)
        elif isinstance(declared, type) and issubclass(declared, Enum):
            if issubclass(declared, IntFlag):
                return ()  # Combinations belong to the flag declaration's mask constraint.
            values = tuple(member.value for member in declared)
        else:
            return ()
        return (cls._choice_constraint(column, values),)

    @classmethod
    def to_sql(cls, value: object) -> object:
        return value

    @classmethod
    def from_sql(cls, value: object) -> object:
        return value

    @classmethod
    def for_type(cls, annotation: object) -> type[SqlStorage]:
        annotation, _ = _base_type(annotation)
        if get_origin(annotation) is Literal:
            literal_types = {type(value) for value in get_args(annotation)}
            if len(literal_types) == 1:
                annotation = next(iter(literal_types))
        for storage in cls.members_with(cls):
            if storage.accepts(annotation):
                return storage
        raise TypeError(f"Unsupported SQLite field type: {annotation!r}")


class TextStorage(SqlStorage):
    sql_type = "TEXT"

    @classmethod
    def accepts(cls, annotation: object) -> bool:
        return annotation is str


class IntegerStorage(SqlStorage):
    sql_type = "INTEGER"

    @classmethod
    def accepts(cls, annotation: object) -> bool:
        return annotation is int


class RealStorage(SqlStorage):
    sql_type = "REAL"

    @classmethod
    def accepts(cls, annotation: object) -> bool:
        return annotation is float


class BooleanStorage(SqlStorage):
    sql_type = "INTEGER"

    @classmethod
    def accepts(cls, annotation: object) -> bool:
        return annotation is bool

    @classmethod
    def constraints(cls, column: str, annotation: object = None) -> tuple[str, ...]:
        return (*super().constraints(column, annotation), f"{_identifier(column)} IN (0, 1)")

    @classmethod
    def to_sql(cls, value: object) -> int | None:
        return None if value is None else int(value)

    @classmethod
    def from_sql(cls, value: object) -> bool:
        if type(value) is not int or value not in (0, 1):
            raise MalformedRecord("SQLite boolean must be 0 or 1")
        return bool(value)


class StringEnumStorage(SqlStorage):
    sql_type = "TEXT"

    @classmethod
    def accepts(cls, annotation: object) -> bool:
        return (
            isinstance(annotation, type)
            and issubclass(annotation, Enum)
            and all(isinstance(member.value, str) for member in annotation)
        )


class IntegerEnumStorage(SqlStorage):
    sql_type = "INTEGER"

    @classmethod
    def accepts(cls, annotation: object) -> bool:
        return isinstance(annotation, type) and issubclass(annotation, (IntEnum, IntFlag))


class FamilyClassStorage(SqlStorage):
    """Names in SQLite decode to the existing declared family exactly once."""

    sql_type = "TEXT"

    @classmethod
    def accepts(cls, annotation: object) -> bool:
        args = get_args(annotation)
        return get_origin(annotation) is type and bool(args) and issubclass(args[0], DeclaredFamily)


class ExactStorage(SqlStorage):
    """SQLite ANY retains the input storage class without affinity coercion."""

    sql_type = "ANY"

    @classmethod
    def accepts(cls, annotation: object) -> bool:
        return False  # Explicit field capability, never an inferred representation.


class JsonStorage(SqlStorage):
    sql_type = "TEXT"

    @classmethod
    def accepts(cls, annotation: object) -> bool:
        from dataclasses import is_dataclass

        return (
            is_dataclass(annotation)
            or get_origin(annotation) in (list, tuple, dict, frozenset, Union, types.UnionType)
            or isinstance(annotation, type)
            and issubclass(annotation, (DeclaredFamily, WireValue))
        )

    @classmethod
    def to_sql(cls, value: object) -> str | None:
        return None if value is None else FieldCodec.compact(value, allow_nan=False)

    @classmethod
    def from_sql(cls, value: object) -> object:
        if type(value) is not str:
            raise MalformedRecord("SQLite JSON must be text")
        return FieldCodec.parse(value)


class Reference(ABC):
    """The reference clause carried by a column or a composite foreign key."""

    @abstractmethod
    def reference_sql(self) -> str: ...


@dataclass(frozen=True)
class NoReference(Reference):
    def reference_sql(self) -> str:
        return ""


@dataclass(frozen=True)
class TableReference(Reference):
    target: type[TypedTable]
    target_columns: tuple[str, ...]

    def reference_sql(self) -> str:
        return (
            f" REFERENCES {_identifier(self.target.declared_name)} "
            f"({self.target._column_list(self.target_columns)})"
        )


@dataclass(frozen=True)
class Column(FieldDirective):
    """Constraints on a field; its name and type remain on the dataclass field."""

    primary_key: bool = False
    record_key: bool = False
    auto_increment: bool = False
    unique: bool = False
    references: Reference = field(default_factory=NoReference)
    check: str | None = None
    index: bool = False
    generated: str | None = None
    storage: type[SqlStorage] | None = None
    nullable: bool = False  # SQLite NULL may decode to a mandatory nominal value.

    @classmethod
    def declared(cls, annotation: object) -> Column:
        for column in cls.single(annotation):
            return column
        return cls()

    def immutable_fields(self, field: _Field) -> tuple[_Field, ...]:
        return ()

    @property
    def writable(self) -> bool:
        return self.generated is None

    @property
    def generated_sql(self) -> str:
        return "" if self.writable else f" GENERATED ALWAYS AS ({self.generated}) STORED"


class FrozenColumn(Column):
    """Immutability is declared with the field's other SQL constraints."""

    def immutable_fields(self, field: _Field) -> tuple[_Field, ...]:
        return (field,)


_NAME_SEPARATORS = re.compile(r"\W+")


@dataclass(frozen=True)
class Generated:
    """A stored column SQLite computes from its row, declared ``name: ClassVar[Generated]``.

    Keys, indexes, constraints and predicates name it like any column, in its declared
    place. No record carries it, so no record holds a value SQLite has not computed.
    """

    annotation: object
    column: Column

    def __post_init__(self) -> None:
        if self.column.writable:
            raise TypeError("A generated column declares its SQL expression")

    def __get__(self, instance: object, owner: type) -> NoReturn:
        raise AttributeError("SQLite computes this column for queries; no record carries it")

    def table_column(self, name: str) -> _Field:
        return _Field(
            name=name,
            wire_name=WireName.read(self.annotation, name) if self.column.record_key else name,
            init=False,
            annotation=self.annotation,
            nullable=self.column.nullable,
            storage=self.column.storage or SqlStorage.for_type(self.annotation),
            column=self.column,
            value=compile_decoder(self.annotation),
        )


@dataclass(frozen=True, slots=True)
class UnallocatedRowId(NullMember):
    """SQLite allocates this row's AUTOINCREMENT id when the row is inserted."""


RowId = int | UnallocatedRowId


@dataclass(frozen=True)
class Index:
    """An index over the row's columns, named by field."""

    terms: tuple[str, ...]
    unique: bool = field(default=False, kw_only=True)
    where: str | None = None

    def term_sql(self, owner: type[TypedTable]) -> str:
        return owner._column_list(self.terms)

    def name(self, owner: type[TypedTable]) -> str:
        """Derived from what the index covers, so adding another never renames this one."""
        parts = [owner.declared_name, self.term_sql(owner)]
        if self.where:
            parts += ["where", owner._sql(self.where)]
        if self.unique:
            parts.append("unique")
        return "_".join(_NAME_SEPARATORS.sub("_", part).strip("_").lower() for part in (*parts, "idx"))

    def sql(self, owner: type[TypedTable], *, if_not_exists: bool = False) -> str:
        existence = "IF NOT EXISTS " if if_not_exists else ""
        return (
            f"CREATE {'UNIQUE ' if self.unique else ''}INDEX "
            f"{existence}{_identifier(self.name(owner))} ON {_identifier(owner.declared_name)} "
            f"({self.term_sql(owner)})"
            + (f" WHERE {owner._sql(self.where)}" if self.where else "")
        )


@dataclass(frozen=True)
class ExpressionIndex(Index):
    """An index over SQL expressions of the row's {field} names."""

    def term_sql(self, owner: type[TypedTable]) -> str:
        return ", ".join(owner._sql(term) for term in self.terms)


@dataclass(frozen=True)
class ForeignKey(TableReference):
    """Composite references declared beside their row, including cyclic owners."""

    columns: tuple[str, ...]
    deferred: bool = False
    on_delete: str | None = None

    def sql(self, owner: type[TypedTable]) -> str:
        if len(self.columns) != len(self.target_columns):
            raise ValueError("Foreign key column counts differ")
        return (
            f"FOREIGN KEY ({owner._column_list(self.columns)})"
            + self.reference_sql()
            + (f" ON DELETE {self.on_delete}" if self.on_delete else "")
            + (" DEFERRABLE INITIALLY DEFERRED" if self.deferred else "")
        )


class SqlProjection:
    """Scalar and record projections read SQLite's result columns at one boundary."""

    @staticmethod
    def result_columns(cursor: sqlite3.Cursor) -> tuple[str, ...]:
        return tuple(column[0] for column in (cursor.description or ()))


@dataclass(frozen=True)
class SqlValue(SqlProjection):
    """A compiled SQLite value; named record fields add their column declaration."""

    annotation: object
    storage: type[SqlStorage]
    value: ValueDecoder[object]

    @classmethod
    def for_annotation(cls, annotation: object) -> SqlValue:
        return cls(annotation, SqlStorage.for_type(annotation), compile_decoder(annotation))

    def read(self, cursor: sqlite3.Cursor) -> list:
        """Read a single-column projection without losing empty or nullable results."""
        try:
            if len(self.result_columns(cursor)) != 1:
                raise MalformedRecord("Scalar projection requires exactly one SQLite column")
            return [self.decode(row[0]) for row in cursor]
        finally:
            cursor.close()

    def encode(self, value: object) -> object:
        return self.storage.to_sql(self.value.encode(value))

    def decode(self, value: object) -> object:
        return self.value.decode(None if value is None else self.storage.from_sql(value))


@dataclass(frozen=True)
class _Field(SqlValue):
    name: str
    wire_name: str
    init: bool
    nullable: bool
    column: Column

    def reference(self, alias: str) -> str:
        return f"{alias}.{_identifier(self.wire_name)}"

    @property
    def lifecycle_family(self) -> type[LifecycleState]:
        return cast(type[LifecycleState], _base_type(self.annotation)[0])

    def discriminator(self, alias: str) -> str:
        family = cast(type[DeclaredFamily], _base_type(self.annotation)[0])
        return f"json_extract({self.reference(alias)}, '$.{family.family_discriminator}')"

    @classmethod
    def for_field(cls, plan: FieldPlan) -> _Field:
        column = Column.declared(plan.annotation)
        return cls(
            name=plan.name,
            wire_name=plan.key if column.record_key else plan.name,
            init=plan.declared.init,
            annotation=plan.annotation,
            nullable=_base_type(plan.annotation)[1] or column.nullable,
            storage=column.storage or SqlStorage.for_type(plan.annotation),
            column=column,
            value=plan.value,
        )

    def rebuild_expression(self, stored: tuple[str, ...]) -> str:
        """A carried value remains stored; table constraints own whether NULL is valid."""
        if self.wire_name in stored:
            return _identifier(self.wire_name)
        return "NULL"


def sql_literal(value: str | None | type[DeclaredFamily]) -> str:
    """Quote a declaration-owned SQL constant, never a query parameter."""
    text = FieldCodec.decode(str | None, FieldCodec.encode(value))
    return "NULL" if text is None else "'" + text.replace("'", "''") + "'"


class TypedRow(SqlProjection):
    """Typed query projection; also the shared decoder for stored row types."""

    @classmethod
    @lru_cache(maxsize=256)
    def _fields(cls) -> tuple[_Field, ...]:
        if not cls.__dataclass_params__.frozen:
            raise TypeError("SQLite row declarations must be frozen dataclasses")
        return tuple(_Field.for_field(plan) for plan in FieldCodec.field_plans(cls))

    @classmethod
    def column_projection(cls) -> dict[str, str]:
        return {item.name: item.wire_name for item in cls._fields()}

    @classmethod
    def columns(cls) -> tuple[str, ...]:
        return tuple(cls.column_projection().values())

    @classmethod
    def _select_list(cls, table: type[TypedTable]) -> str:
        return ", ".join(
            (table._sql(item.column.generated) if not item.column.writable
             else table._column_list((item.name,))) + f" AS {_identifier(item.wire_name)}"
            for item in cls._fields())

    @classmethod
    def _fields_named(cls, names: tuple[str, ...]) -> tuple[_Field, ...]:
        """Resolve a declared field subset, including an unfiltered query."""
        declared = {item.name: item for item in cls._fields()}
        if unknown := set(names).difference(declared):
            raise ValueError(f"Unknown fields for {cls.__name__}: {sorted(unknown)}")
        return tuple(declared[name] for name in names)

    @classmethod
    def _computed_columns(cls) -> frozenset[str]:
        """Columns a query may return that no record of this declaration carries."""
        return frozenset()

    @classmethod
    def _positions(cls, cursor: sqlite3.Cursor,
                   extra: Mapping[str, str] = types.MappingProxyType({})) -> dict[str, int]:
        names = cls.result_columns(cursor)
        if not names:
            raise ValueError("Typed read requires a result set")
        projection = {**cls.column_projection(), **extra}
        carried = set(names).difference(cls._computed_columns())
        if len(set(names)) != len(names) or carried != set(projection.values()):
            raise MalformedRecord(f"Query columns do not match {cls.__name__}: {names!r}")
        return {key: names.index(column) for key, column in projection.items()}

    @classmethod
    def _decode_row(
        cls, row: sqlite3.Row | tuple[object, ...], positions: Mapping[str, int]
    ) -> Self:
        values = {item.name: item.decode(row[positions[item.name]]) for item in cls._fields()}
        instance = FieldCodec.construct(
            cls, {item.name: values[item.name] for item in cls._fields() if item.init}
        )
        for item in cls._fields():
            if not item.init:
                object.__setattr__(instance, item.name, values[item.name])
        return instance

    @classmethod
    def iterate(cls, cursor: sqlite3.Cursor) -> Generator[Self, None, None]:
        """Stream strictly decoded rows; close this iterator to release its query."""
        try:
            positions = cls._positions(cursor)
            for row in cursor:
                yield cls._decode_row(row, positions)
        finally:
            cursor.close()

    @classmethod
    def joined(
        cls, cursor: sqlite3.Cursor, related: type[RelatedRow]
    ) -> list[tuple[Self, RelatedRow]]:
        """Decode disjoint joined declarations once without another partial row schema.

        All actual columns must belong to exactly these declarations; duplicate,
        missing and extra columns are rejected by the same strict query boundary.
        This reads one SQLite snapshot and never re-encodes a stored row as JSON.
        """
        try:
            positions = cls._positions(cursor, related.column_projection())
            return [(cls._decode_row(row, positions), related._decode_row(row, positions))
                    for row in cursor]
        finally:
            cursor.close()

    @classmethod
    def read(cls, cursor: sqlite3.Cursor) -> list[Self]:
        """Decode a complete bounded query through the same streaming boundary."""
        return list(cls.iterate(cursor))


@dataclass(frozen=True)
class SQLiteSchemaDifference:
    name: str
    stored: str | None
    declared: str | None


@dataclass(frozen=True)
class SQLiteDatabase(TypedRow):
    seq: int
    name: str
    file: str


@dataclass(frozen=True)
class SQLiteSchemaObject(TypedRow):
    name: str
    sql: str

    @classmethod
    def differences(
        cls, actual: list[SQLiteSchemaObject], expected: Mapping[str, str],
    ) -> tuple[SQLiteSchemaDifference, ...]:
        stored = {row.name: row.sql for row in actual}
        if stored == expected:
            return ()
        return tuple(SQLiteSchemaDifference(name, stored.get(name), expected.get(name))
                     for name in sorted(stored.keys() | expected.keys())
                     if stored.get(name) != expected.get(name))


@dataclass(frozen=True)
class SQLiteTableName(TypedRow):
    """One table SQLite's catalog holds, whether or not a row declaration names it."""

    name: str

    @classmethod
    def stored(cls, db: sqlite3.Connection) -> list[SQLiteTableName]:
        """The file's own tables, newest first, so dependents drop before their parents."""
        return cls.read(db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY rowid DESC"
        ))

    def drop(self, db: sqlite3.Connection) -> None:
        db.execute(f"DROP TABLE {_identifier(self.name)}")


class CatalogKind(DeclaredFamily, affix="Kind"):
    """What SQLite's catalog calls an entry; each kind owns how a schema carry treats it."""

    @classmethod
    def drop(cls, db: sqlite3.Connection, name: str) -> None:
        db.execute(f"DROP {cls.declared_name.upper()} IF EXISTS {_identifier(name)}")

    @classmethod
    def derive(cls, db: sqlite3.Connection, item: SQLiteCatalogObject,
               declared: Mapping[str, str]) -> None:
        """An object that holds no rows is rebuilt from its declaration: one that differs from
        it, or that nothing declares, is dropped, and the carry creates what is declared."""
        if declared.get(item.name) != item.sql:
            cls.drop(db, item.name)


class TableKind(CatalogKind):
    @classmethod
    def derive(cls, db: sqlite3.Connection, item: SQLiteCatalogObject,
               declared: Mapping[str, str]) -> None:
        """Rows are never rebuilt from a declaration: a table that differs stays, and the
        caller's schema check refuses it."""


class IndexKind(CatalogKind):
    pass


class TriggerKind(CatalogKind):
    pass


class ViewKind(CatalogKind):
    pass


@dataclass(frozen=True)
class SQLiteCatalogObject(TypedRow):
    """One catalog entry: SQLite's own type, name and table."""

    type: type[CatalogKind]
    name: str
    tbl_name: str
    sql: str

    def drop(self, db: sqlite3.Connection) -> None:
        self.type.drop(db, self.name)

    @classmethod
    def stored(cls, db: sqlite3.Connection) -> tuple[SQLiteCatalogObject, ...]:
        return tuple(cls.read(db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name")))

    @classmethod
    def carry(cls, db: sqlite3.Connection, declared: Mapping[str, str],
              compared: frozenset[str]) -> None:
        """Inside the caller's write transaction, the compared objects onto the declaration:
        each kind derives its stored objects (rebuilt, or left for the caller's check), then
        every declared object the file lacks is created from its declared SQL."""
        for item in cls.stored(db):
            if item.name in compared:
                item.type.derive(db, item, declared)
        present = {item.name for item in cls.stored(db)}
        for name, sql in declared.items():
            if name not in present:
                db.execute(sql)


class SchemaChange(ABC):
    """Who may change a stored schema: the reconcile asks before it writes."""

    @abstractmethod
    def require(self) -> None: ...


@dataclass(frozen=True)
class HeldBusFile(SchemaChange):
    """A bus file every runtime on the root opens: its schema changes only in the process
    holding the root's maintenance, so a newer runtime opening the live bus outside an
    install refuses instead of moving it under the running owners (install 53, 2026-10-22)."""

    owner: type[RootFile]
    path: Path

    def require(self) -> None:
        barrier = MaintenanceBarrier(RegistryFile.path_in(self.owner.bus_root(self.path)))
        # Only the holder clears a hold, so a nonce this process holds cannot go stale while
        # it reads; the witness alone answers without the wire lock.
        barrier.current_unlocked().require_held_by(
            os.environ.get(HOLDER_NONCE, ""), f"Changing the schema of {self.path}")


class WriterOwnedFile(SchemaChange):
    """A writer-owned file follows its writer's schema without an installation carry.

    Private bridge journals have one writer; derived stores use schema-specific
    filenames and rebuild their rows from another authority.
    """

    def require(self) -> None:
        """The owning writer may rebuild this schema."""


class TableSchema(ABC):
    """The tables of one SQLite file, and the one reader of the SQL SQLite stored for them.

    The file's schema is derived from its member rows' declarations. Whether a stored
    schema that differs is rebuilt or refused is the file's parent: DerivedTables or
    DurableTables. A marker class every table of a file inherits names its members;
    a store names its rows.
    """

    __slots__ = ()
    schema_error: ClassVar[type[Exception]] = RelationViolationError

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return tuple(TypedTable.members_with(cls))

    @classmethod
    def declares_schema(cls) -> bool:
        """Whether this class names a file's tables, not only a parent of such classes."""
        return True

    @classmethod
    def schema_members(cls) -> tuple[type[TableSchema], ...]:
        """Every descendant that declares a schema, parents of stores passed through."""
        descendants = (kind for child in cls.__subclasses__()
                       for kind in (child, *child.schema_members()))
        return tuple(dict.fromkeys(kind for kind in descendants if kind.declares_schema()))

    @classmethod
    def declared_schema(cls) -> dict[str, str]:
        return {name: sql for row in cls.schema_rows() for name, sql in row.schema_objects().items()}

    @classmethod
    def stored_schema(cls, db: sqlite3.Connection) -> list[SQLiteSchemaObject]:
        """The objects this file's kind compares: by default every object the file owns.

        SQLite's own sqlite_sequence for AUTOINCREMENT is not one. A file kind with a
        different scope overrides this read and nothing else.
        """
        return SQLiteSchemaObject.read(db.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"))

    @classmethod
    def stored_tables(cls, db: sqlite3.Connection) -> list[SQLiteTableName]:
        """The physical tables this schema owns, dependents before their sources."""
        return SQLiteTableName.stored(db)

    @classmethod
    def schema_differs(cls, db: sqlite3.Connection) -> bool:
        return bool(SQLiteSchemaObject.differences(cls.stored_schema(db), cls.declared_schema()))

    @classmethod
    def require_schema(cls, db: sqlite3.Connection) -> None:
        """Readers use only the declared schema, whichever kind of file this is."""
        if cls.schema_differs(db):
            raise cls.schema_error(f"{cls.__name__} differs from its declaration")

    @classmethod
    def require_installed(cls, db: sqlite3.Connection) -> None:
        """Installed durable families require their declaration and enforced relations."""
        cls.require_schema(db)
        if SQLiteForeignKeys.read(db.execute("PRAGMA foreign_keys")) != [SQLiteForeignKeys(True)]:
            raise cls.schema_error(f"{cls.__name__} foreign-key enforcement is disabled")

    @classmethod
    def create_schema(cls, db: sqlite3.Connection) -> None:
        for row in cls.schema_rows():
            row.create(db)

    @classmethod
    def derive_schema(cls, db: sqlite3.Connection) -> None:
        """Inside the caller's write transaction, the stored schema onto the declaration
        wherever the declaration says how: a row's declared earlier table name, missing
        nullable fields, an index, trigger or view rebuilt from its SQL, or a missing object.
        Other table differences are refused with the rows untouched."""
        for row in cls.schema_rows():
            row.take_previous_name(db)
            row.derive_nullable_fields(db)
        SQLiteCatalogObject.carry(db, cls.declared_schema(),
                                  frozenset(item.name for item in cls.stored_schema(db)))
        cls.require_schema(db)

    @classmethod
    def reconcile_schema(cls, db: sqlite3.Connection, change: SchemaChange) -> bool:
        """The writer's answer to a stored schema that differs from the declaration, in a
        write transaction of its own and only where ``change`` allows; whether it changed
        the file."""
        if not cls.schema_differs(db):
            return False
        change.require()
        with SQLiteFile.write_transaction(db):
            cls.carry_schema(db)
        return True

    @classmethod
    def install_schema(cls, db: sqlite3.Connection, change: SchemaChange) -> None:
        """Every writer's opening: a file with no stored schema gets its declaration, and
        one that has a schema is reconciled onto it. The first read also attaches an existing
        WAL, and an opening that finds its schema takes no write lock."""
        if not cls.stored_schema(db):
            with SQLiteFile.write_transaction(db):
                if not cls.stored_schema(db):
                    cls.create_schema(db)
        cls.reconcile_schema(db, change)

    @classmethod
    @abstractmethod
    def carry_schema(cls, db: sqlite3.Connection) -> None:
        """Inside the reconcile's transaction: this kind of file's way onto its declaration."""


class ComposedSchemas:
    """One file derives its rows and carry policy from its nominal schema parents."""

    @classmethod
    def schema_families(cls) -> tuple[type[TableSchema], ...]:
        return tuple(family for family in cls.__bases__ if issubclass(family, TableSchema))

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return tuple(dict.fromkeys(
            row for family in cls.schema_families() for row in family.schema_rows()
        ))

    @classmethod
    def schema_differs(cls, db: sqlite3.Connection) -> bool:
        return any(family.schema_differs(db) for family in cls.schema_families())

    @classmethod
    def create_schema(cls, db: sqlite3.Connection) -> None:
        for family in cls.schema_families():
            family.create_schema(db)

    @classmethod
    def carry_schema(cls, db: sqlite3.Connection) -> None:
        for family in cls.schema_families():
            family.carry_schema(db)


class RowMutation(Enum):
    """SQLite's destructive row events own their immutable-history guards."""

    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"

    def abort_trigger(
        self, row: type[TypedTable], *, columns: tuple[str, ...] = (), when: str = "",
        message: str = "Rows are immutable", after: bool = False, suffix: str = "",
    ) -> SQLiteSchemaObject:
        suffix = ("_" + "_".join(columns) if columns else "") + suffix
        timing = "AFTER" if after else "BEFORE"
        name = f"{row.declared_name}_{self.value.lower()}{suffix}_{timing.lower()}_abort"
        selected = " OF " + row._column_list(columns) if columns else ""
        condition = " WHEN " + row._sql(when) if when else ""
        return SQLiteSchemaObject(
            name,
            f'CREATE TRIGGER "{name}" {timing} {self.value}{selected} ON "{row.declared_name}"'
            f"{condition} BEGIN SELECT RAISE(ABORT, '{message}'); END",
        )


class MutationCapability(ABC):
    """Each MRO declaration contributes guards; siblings never override one another."""

    @classmethod
    @abstractmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]: ...


class NoUpdate(MutationCapability):
    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        return (RowMutation.UPDATE.abort_trigger(row, message="Rows cannot be updated"),)


class NoDelete(MutationCapability):
    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        return (RowMutation.DELETE.abort_trigger(row, message="Rows cannot be deleted"),)


@dataclass(frozen=True)
class ColumnGuard:
    """A predicate on an UPDATE's selected columns, not another SQL trigger template."""

    columns: tuple[str, ...]
    condition: str = "1"

    def trigger(self, row: type[TypedTable]) -> SQLiteSchemaObject:
        return RowMutation.UPDATE.abort_trigger(
            row, columns=self.columns, when=self.condition, message="Column change is refused")


class GuardedColumns(MutationCapability):
    column_guards: ClassVar[tuple[ColumnGuard, ...]] = ()

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        return tuple(guard.trigger(row) for guard in row.column_guards)


class FrozenColumns(MutationCapability):
    @classmethod
    def immutable_columns(cls, row: type[TypedTable]) -> tuple[_Field, ...]:
        return tuple(
            immutable for field in row._columns()
            for immutable in field.column.immutable_fields(field)
        )

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        columns = cls.immutable_columns(row)
        if not columns:
            return ()
        condition = " OR ".join(
            f'NEW."{column.wire_name}" IS NOT OLD."{column.wire_name}"' for column in columns)
        return (RowMutation.UPDATE.abort_trigger(
            row, when=condition, suffix="_frozen", message="Column values are immutable"),)


class OneWayColumn(MutationCapability):
    """Every update consumes one declared old-to-new column transition."""

    transition_column: ClassVar[str]
    transition_from: ClassVar[int]
    transition_to: ClassVar[int]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        column = row._column_list((row.transition_column,))
        condition = (f"OLD.{column} IS NOT {row.transition_from} "
                     f"OR NEW.{column} IS NOT {row.transition_to}")
        return (RowMutation.UPDATE.abort_trigger(
            row, when=condition, suffix="_one_way", message="Column transition is one-way"),)


class InitialOneWayColumn(OneWayColumn):
    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        column = row._column_list((row.transition_column,))
        return (RowMutation.INSERT.abort_trigger(
            row, when=f"NEW.{column} IS NOT {row.transition_from}", suffix="_initial_value",
            message="Initial value must precede its transition"),)


class IncrementingColumn(MutationCapability):
    revision_column: ClassVar[str] = "revision"

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        column = row._column_list((row.revision_column,))
        return (RowMutation.UPDATE.abort_trigger(
            row, when=f"NEW.{column} != OLD.{column} + 1", suffix="_revision",
            message="Revision must advance once"),)


@dataclass(frozen=True)
class ColumnOrder(ABC):
    columns: tuple[str, ...]

    @property
    @abstractmethod
    def comparison(self) -> str: ...

    @property
    @abstractmethod
    def suffix(self) -> str: ...

    def violation(self, previous: str, following: str) -> str:
        return f"{following} IS NULL OR {following} {self.comparison} {previous}"

    def trigger(self, row: type[TypedTable]) -> SQLiteSchemaObject:
        conditions = (
            f"({field.reference('OLD')} IS NOT NULL AND "
            f"({self.violation(field.reference('OLD'), field.reference('NEW'))}))"
            for field in row._fields_named(self.columns))
        return RowMutation.UPDATE.abort_trigger(
            row, when=" OR ".join(conditions), suffix=self.suffix,
            message="Column ordering is monotone")


class NonDecreasing(ColumnOrder):
    comparison = "<"
    suffix = "_increasing"


class NonIncreasing(ColumnOrder):
    comparison = ">"
    suffix = "_decreasing"


class NonDecreasingWhenPresent(NonDecreasing):
    suffix = "_present_increasing"

    def violation(self, previous: str, following: str) -> str:
        return f"{following} IS NOT NULL AND {following} {self.comparison} {previous}"


class MonotoneColumns(MutationCapability):
    column_orders: ClassVar[tuple[ColumnOrder, ...]]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        return tuple(order.trigger(row) for order in row.column_orders)


class AccumulatingBits(MutationCapability):
    accumulating_columns: ClassVar[tuple[str, ...]]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        conditions = (
            f'((NEW."{field.wire_name}" | OLD."{field.wire_name}") != NEW."{field.wire_name}")'
            for field in row._fields_named(row.accumulating_columns))
        return (RowMutation.UPDATE.abort_trigger(
            row, when=" OR ".join(conditions), suffix="_accumulating",
            message="Observed bits cannot be erased"),)


class WriteOnceColumns(MutationCapability):
    write_once_columns: ClassVar[tuple[str, ...]]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        condition = " OR ".join(
            f"({field.reference('OLD')} IS NOT NULL AND "
            f"{field.reference('NEW')} IS NOT {field.reference('OLD')})"
            for field in row._fields_named(row.write_once_columns))
        return (RowMutation.UPDATE.abort_trigger(
            row, when=condition, suffix="_write_once", message="Recorded values are immutable"),)


class RequiredUpdateColumns(MutationCapability):
    required_update_columns: ClassVar[tuple[str, ...]]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        condition = " OR ".join(f"{field.reference('NEW')} IS NULL"
                                for field in row._fields_named(row.required_update_columns))
        return (RowMutation.UPDATE.abort_trigger(
            row, when=condition, suffix="_present", message="Update requires recorded references"),)


class FinalizedColumn(MutationCapability):
    finalized_column: ClassVar[str]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        (field,) = row._fields_named((row.finalized_column,))
        return (RowMutation.UPDATE.abort_trigger(
            row, when=f"{field.reference('OLD')} IS NOT NULL", suffix="_recorded",
            message="Recorded row cannot be changed"),)


class StableReferenceColumns(MutationCapability):
    reference_column: ClassVar[str]
    stable_reference_columns: ClassVar[tuple[str, ...]]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        (reference,) = row._fields_named((row.reference_column,))
        changes = " OR ".join(
            f"{field.reference('NEW')} IS NOT {field.reference('OLD')}"
            for field in row._fields_named(row.stable_reference_columns))
        return (RowMutation.UPDATE.abort_trigger(
            row, when=(f"{reference.reference('NEW')} IS {reference.reference('OLD')} "
                       f"AND ({changes})"),
            suffix="_reference", message="Values require a new reference"),)


class RequiredColumn(MutationCapability):
    dependent_column: ClassVar[str]
    required_column: ClassVar[str]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        dependent, required = row._fields_named((row.dependent_column, row.required_column))
        return (RowMutation.UPDATE.abort_trigger(
            row, when=f"{dependent.reference('NEW')} IS NOT NULL AND "
            f"{required.reference('NEW')} IS NULL", suffix="_required",
            message="Dependent value requires its original reference"),)


class FirstValueState(MutationCapability):
    initial_value_column: ClassVar[str]
    initial_value_state: ClassVar[type[LifecycleState]]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        (value,) = row._fields_named((row.initial_value_column,))
        (lifecycle,) = row._fields_named(("lifecycle",))
        names = ",".join(sql_literal(state.declared_name) for state in
                         lifecycle.lifecycle_family.members_with(row.initial_value_state))
        return (RowMutation.UPDATE.abort_trigger(
            row, when=f"{value.reference('OLD')} IS NULL AND {value.reference('NEW')} IS NOT NULL "
            f"AND {lifecycle.discriminator('NEW')} NOT IN ({names})", suffix="_first_value",
            message="Initial value requires its declared lifecycle"),)


class LifecycleTransitions(MutationCapability):
    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        (field,) = row._fields_named(("lifecycle",))
        previous, following = field.discriminator("OLD"), field.discriminator("NEW")
        edges = " OR ".join(
            f"({previous} = {sql_literal(name)} AND {following} IN "
            f"({','.join(sql_literal(target) for target in sorted(targets))}))"
            for name, targets in field.lifecycle_family.transition_table().items()
            if targets
        ) or "0"
        return (RowMutation.UPDATE.abort_trigger(
            row, columns=("lifecycle",), when=f"{previous} != {following} AND NOT ({edges})",
            suffix="_edges", message="Lifecycle transition is not declared"),)


class OrdinalTransitions(MutationCapability):
    """Python and SQLite consume each state's same ordinal-rule declaration."""

    ordinal_column: ClassVar[str] = "current_attempt_ordinal"

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        (state,) = row._fields_named(("lifecycle",))
        (ordinal,) = row._fields_named((row.ordinal_column,))
        family = cast("type[ExecutionState]", state.lifecycle_family)
        violations = []
        for member in family.members_with(family):
            rule = member.ordinal_rule
            expected = f"NULLIF(COALESCE({ordinal.reference('OLD')}, 0) + {rule.increment}, 0)"
            allowed = ",".join(sql_literal(previous.declared_name)
                               for previous in family.members_with(rule.predecessors))
            violations.append(
                f"({state.discriminator('NEW')} = {sql_literal(member.declared_name)} AND "
                f"({state.discriminator('OLD')} NOT IN ({allowed}) OR "
                f"{ordinal.reference('NEW')} IS NOT ({expected})))")
        return (RowMutation.UPDATE.abort_trigger(
            row, when=" OR ".join(violations), suffix="_ordinal",
            message="Attempt ordinal violates lifecycle rule"),)


class LifecycleChanges(MutationCapability):
    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        (field,) = row._fields_named(("lifecycle",))
        return (RowMutation.UPDATE.abort_trigger(
            row, when=f"{field.discriminator('OLD')} = {field.discriminator('NEW')}",
            suffix="_changed", message="Lifecycle disposition must change"),)


class FinalizedLifecycle(MutationCapability):
    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        (field,) = row._fields_named(("lifecycle",))
        closed = ",".join(sql_literal(name) for name, targets in
                          field.lifecycle_family.transition_table().items() if not targets)
        return (RowMutation.UPDATE.abort_trigger(
            row, when=f"{field.discriminator('OLD')} = {field.discriminator('NEW')} AND "
            f"{field.discriminator('OLD')} IN ({closed})", suffix="_final",
            message="Final lifecycle cannot be rewritten"),)


class CounterDependent:
    """A historical counter reference blocks its owner's advance while still live."""

    counter_reference: ClassVar[ForeignKey]


class CounterAdvance(MutationCapability):
    counter_column: ClassVar[str]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        (counter,) = row._fields_named((row.counter_column,))
        blocked = []
        for dependent in TypedTable.members_with(CounterDependent):
            reference = dependent.counter_reference
            if reference.target is not row:
                continue
            keys = " AND ".join(
                f"{source.reference('dependent')} = {target.reference('OLD')}"
                for source, target in zip(dependent._fields_named(reference.columns),
                                          row._fields_named(reference.target_columns), strict=True))
            (state,) = dependent._fields_named(("lifecycle",))
            closed = ",".join(sql_literal(name) for name, targets in
                              state.lifecycle_family.transition_table().items() if not targets)
            blocked.append(
                f"EXISTS (SELECT 1 FROM {_identifier(dependent.declared_name)} dependent "
                f"WHERE {keys} AND {state.discriminator('dependent')} NOT IN ({closed}))"
            )
        condition = (
            f"{counter.reference('NEW')} != {counter.reference('OLD')} AND ("
            + " OR ".join(
                [f"{counter.reference('NEW')} != {counter.reference('OLD')} + 1", *blocked]
            )
            + ")"
        )
        return (RowMutation.UPDATE.abort_trigger(
            row, when=condition, suffix="_counter", message="Counter advance has live dependents"),)


class GuardedUpdateMembership(MutationCapability):
    update_membership_condition: ClassVar[str]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        return (RowMutation.UPDATE.abort_trigger(
            row, when=row.update_membership_condition, suffix="_membership",
            message="Update requires published membership"),)


class GuardedInserts(MutationCapability):
    insert_condition: ClassVar[str]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        return (RowMutation.INSERT.abort_trigger(
            row, when=row.insert_condition, message="Insert violates row membership"),)


class GuardedRelatedInserts(MutationCapability):
    related_insert_target: ClassVar[type[TypedTable]]
    related_insert_condition: ClassVar[str]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        return (RowMutation.INSERT.abort_trigger(
            row.related_insert_target, when=row.related_insert_condition,
            suffix=f"_related_{row.declared_name}",
            message="Insert violates related row membership"),)


class ValidatedInserts(MutationCapability):
    validator_function: ClassVar[str]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        arguments = ", ".join(f'NEW."{field.wire_name}"' for field in row._fields() if field.init)
        return (RowMutation.INSERT.abort_trigger(
            row, when=f"{row.validator_function}({arguments}) != 1", suffix="_validation",
            message="Insert fails declared validation"),)


class GuardedAfterWrites(MutationCapability):
    after_write_condition: ClassVar[str]

    @classmethod
    def mutation_guards(cls, row: type[TypedTable]) -> tuple[SQLiteSchemaObject, ...]:
        return tuple(mutation.abort_trigger(
            row, when=row.after_write_condition, after=True,
            message="Write violates related row invariants")
            for mutation in (RowMutation.INSERT, RowMutation.UPDATE))


class ImmutableRows(NoUpdate, NoDelete):
    """A row family refuses both destructive mutations."""


class AppendOnlyTables(ImmutableRows, TableSchema):
    """Declared history tables reject UPDATE and DELETE, even from raw SQL."""


class SharedFileTables(TableSchema):
    """Tables that share a file with others: only their own tables and indexes compare."""

    __slots__ = ()

    @classmethod
    def stored_schema(cls, db: sqlite3.Connection) -> list[SQLiteSchemaObject]:
        return TypedTable.objects_of(db, cls.schema_rows())

    @classmethod
    def stored_tables(cls, db: sqlite3.Connection) -> list[SQLiteTableName]:
        names = frozenset(row.declared_name for row in cls.schema_rows())
        return [table for table in super().stored_tables(db) if table.name in names]


class AllObjectsTables(TableSchema):
    """A file that declares every object it holds, SQLite's own included."""

    __slots__ = ()

    @classmethod
    def stored_schema(cls, db: sqlite3.Connection) -> list[SQLiteSchemaObject]:
        return SQLiteSchemaObject.read(db.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))


class DerivedTables(TableSchema):
    """Rows derived from another source: their owner can recreate schema and contents."""

    __slots__ = ()

    @classmethod
    def projection_rows(cls) -> tuple[type[TypedTable], ...]:
        return tuple(row for row in cls.schema_rows() if issubclass(row, ProducedRow))

    @classmethod
    def schema_differs(cls, db: sqlite3.Connection) -> bool:
        return (super().schema_differs(db)
                or not ProjectionProducer.matches(db, cls.projection_rows()))

    @classmethod
    def create_schema(cls, db: sqlite3.Connection) -> None:
        super().create_schema(db)
        for producer in ProjectionProducer.records_for(cls.projection_rows()):
            producer.upsert(db)

    @classmethod
    def reset_schema(cls, db: sqlite3.Connection) -> None:
        """Inside the caller's transaction: rebuild only this schema's derived tables."""
        for row in cls.schema_rows():
            row.take_previous_name(db)
        for stored in cls.stored_tables(db):
            stored.drop(db)
        cls.create_schema(db)

    @classmethod
    def carry_schema(cls, db: sqlite3.Connection) -> None:
        cls.reset_schema(db)


class DurableTables(TableSchema):
    """Rows nothing else can rebuild: the writer carries a stored schema that differs onto
    the declaration where the declaration says how, keeping every row, and refuses the rest.
    No hand-written step repeats what a declaration already says."""

    __slots__ = ()

    @classmethod
    def carry_schema(cls, db: sqlite3.Connection) -> None:
        cls.derive_schema(db)


class ProjectionProducerTables(SharedFileTables, DurableTables):
    """The producer declaration last committed with each rebuilt projection."""


class ProducedRow:
    """Rows rebuilt whenever the installed Core build changes."""


@lru_cache(maxsize=4)
def _claimed_names(members: tuple[type[TypedTable], ...]) -> frozenset[str]:
    """Every object name any row declares; keyed by the registry, so a new member refreshes it."""
    return frozenset(name for row in members for name in row.schema_objects())


@dataclass(frozen=True)
class SQLiteForeignKeys(TypedRow):
    foreign_keys: bool


@dataclass(frozen=True)
class SQLiteJournalMode(TypedRow):
    journal_mode: str


@dataclass(frozen=True)
class SQLiteUserVersion(TypedRow):
    user_version: int


class TypedTable(
    FrozenColumns, TypedRow, DeclaredFamily, affix="Row", discovery_package="agent_comms"
):
    """The row declaration owns its table; no separate table/schema registry.

    Fields declare SQL capabilities with ``Annotated[T, Column(...)]``. Composite keys derive
    from primary-key fields in declaration order. Table-wide constraints and
    indexes live on the same row declaration. SQL predicates contain trusted
    source expressions; values always use bound parameters.
    """

    family_discriminator: ClassVar[str] = "row"
    unique: ClassVar[tuple[tuple[str, ...], ...]] = ()
    checks: ClassVar[tuple[str, ...]] = ()
    indexes: ClassVar[tuple[Index, ...]] = ()
    without_rowid: ClassVar[bool] = False
    renamed_from: ClassVar[tuple[str, ...]] = ()
    """Table names this row's rows were stored under before; the writer renames the one it
    finds, so a renamed declaration needs no hand-written step."""

    @classmethod
    def _member_declarations(cls) -> tuple[type[DeclaredFamily], ...]:
        """Enumerate the schema owner's already imported row declarations.

        Schema owners load their row modules before querying capabilities. A
        checkpoint read must not import unrelated application modules while
        holding its bus lock. External table-name decoding still uses the
        registry's complete lazy discovery through DeclaredFamily.member_named.
        """
        return tuple(dict.values(cls.__registry__))

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        # A method permits mutually referring row classes without string names.
        return ()

    @classmethod
    @lru_cache(maxsize=256)
    def _columns(cls) -> tuple[_Field, ...]:
        """Every stored column: the record's fields, each generated column after the field
        declared before it."""
        fields = cls._fields()
        if any(not item.column.writable for item in fields):
            raise TypeError(f"{cls.__name__} must declare generated columns as ClassVar[Generated]")
        names = {item.name for item in fields}
        following: dict[str, list[_Field]] = {}
        for owner in cls.__mro__:
            declared = tuple(annotationlib.get_annotations(owner, format=annotationlib.Format.STRING))
            for position, name in enumerate(declared):
                match vars(owner).get(name):
                    case Generated() as declaration:
                        match [before for before in declared[:position] if before in names]:
                            case [*_, anchor]:
                                following.setdefault(anchor, []).append(
                                    declaration.table_column(name))
                            case []:
                                raise TypeError(f"{cls.__name__}.{name} follows no record field")
        return tuple(column for item in fields for column in (item, *following.get(item.name, ())))

    @classmethod
    def _computed_columns(cls) -> frozenset[str]:
        return frozenset(item.wire_name for item in cls._columns() if not item.column.writable)

    @classmethod
    def _fields_named(cls, names: tuple[str, ...]) -> tuple[_Field, ...]:
        """Resolve declared columns, generated ones included, by their declared names."""
        declared = {item.name: item for item in cls._columns()}
        if unknown := set(names).difference(declared):
            raise ValueError(f"Unknown columns for {cls.__name__}: {sorted(unknown)}")
        return tuple(declared[name] for name in names)

    @classmethod
    def _column_list(cls, names: tuple[str, ...]) -> str:
        if not names:
            raise ValueError(f"Empty column list for {cls.declared_name}")
        return ", ".join(_identifier(item.wire_name) for item in cls._fields_named(names))

    @classmethod
    def _select_list(cls, table: type[TypedTable]) -> str:
        return cls._column_list(tuple(cls.column_projection()))

    @classmethod
    def _sql(cls, expression: str) -> str:
        for item in cls._columns():
            expression = expression.replace("{" + item.name + "}", item.wire_name)
        return expression

    @classmethod
    def _table_sql(cls, *, if_not_exists: bool = False, columns: tuple[str, ...] = ()) -> str:
        existence = "IF NOT EXISTS " if if_not_exists else ""
        definitions = []
        fields = cls._fields_named(columns) if columns else cls._columns()
        keys = tuple(item.name for item in fields if item.column.primary_key)
        auto_keys = tuple(item.name for item in fields if item.column.auto_increment)
        if auto_keys and (auto_keys != keys or len(keys) != 1 or cls.without_rowid):
            raise TypeError("AUTOINCREMENT requires one rowid primary key")
        for item in fields:
            column = item.column
            sql = f"{_identifier(item.wire_name)} {item.storage.sql_type}"
            if column.auto_increment:
                sql += " PRIMARY KEY AUTOINCREMENT"
            sql += cls._sql(column.generated_sql)
            if not item.nullable or column.primary_key:
                sql += " NOT NULL"
            if column.unique:
                sql += " UNIQUE"
            sql += column.references.reference_sql()
            for constraint in item.storage.constraints(item.wire_name, item.annotation):
                sql += f" CHECK ({constraint})"
            if column.check:
                sql += f" CHECK ({cls._sql(column.check)})"
            definitions.append(sql)
        if keys and not auto_keys:
            definitions.append(f"PRIMARY KEY ({cls._column_list(keys)})")
        elif cls.without_rowid:
            raise TypeError("WITHOUT ROWID requires a declared primary key")
        definitions.extend(f"UNIQUE ({cls._column_list(group)})" for group in cls.unique)
        definitions.extend(f"CHECK ({cls._sql(check)})" for check in cls.checks)
        definitions.extend(reference.sql(cls) for reference in cls.references())
        table = _identifier(cls.declared_name)
        return (f"CREATE TABLE {existence}{table} ({', '.join(definitions)}) STRICT"
                + (", WITHOUT ROWID" if cls.without_rowid else ""))

    @classmethod
    def schema_objects(cls, *, if_not_exists: bool = False) -> dict[str, str]:
        statements = {cls.declared_name: cls._table_sql(if_not_exists=if_not_exists)}
        indexes = cls.indexes + tuple(
            Index((item.name,)) for item in cls._columns() if item.column.index
        )
        for index in indexes:
            if index.name(cls) in statements:
                raise TypeError(f"{cls.__name__} declares two indexes named {index.name(cls)}")
            statements[index.name(cls)] = index.sql(cls, if_not_exists=if_not_exists)
        statements.update(cls.triggers(if_not_exists=if_not_exists))
        return statements

    @classmethod
    def triggers(cls, *, if_not_exists: bool = False) -> dict[str, str]:
        guards = (
            trigger for capability in cls.__mro__
            if issubclass(capability, MutationCapability)
            and "mutation_guards" in vars(capability) and capability is not MutationCapability
            for trigger in capability.mutation_guards(cls)
        )
        statements: dict[str, str] = {}
        for trigger in guards:
            if trigger.name in statements:
                raise TypeError(f"{cls.__name__} declares two guards named {trigger.name}")
            statements[trigger.name] = trigger.sql.replace(
                "CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ", 1
            ) if if_not_exists else trigger.sql
        return statements

    @classmethod
    def structure_objects(cls, *, if_not_exists: bool = False) -> dict[str, str]:
        """Tables, indexes and views, with mutation guards excluded by their declarations."""
        guards = cls.triggers()
        return {name: sql for name, sql in cls.schema_objects(if_not_exists=if_not_exists).items()
                if name not in guards}

    @classmethod
    def ddl(cls, *, if_not_exists: bool = False) -> tuple[str, ...]:
        return tuple(cls.schema_objects(if_not_exists=if_not_exists).values())

    @classmethod
    def create(cls, db: sqlite3.Connection, *, if_not_exists: bool = False) -> None:
        """Create the declared schema; SQLite owns concurrent existence checks."""
        for statement in cls.ddl(if_not_exists=if_not_exists):
            db.execute(statement)

    @classmethod
    def drop(cls, db: sqlite3.Connection) -> None:
        """Drop the declared table; SQLite drops its indexes with it."""
        db.execute(f"DROP TABLE IF EXISTS {_identifier(cls.declared_name)}")

    @classmethod
    def rebuild(cls, db: sqlite3.Connection,
                changed: Mapping[str, str] = types.MappingProxyType({})) -> None:
        """Copy writable fields into the declared schema, in the caller's transaction.

        Data conversions supply expressions only for the fields they change.
        The table owns every other column, its indexes, constraints and triggers.
        """
        cls._writable_fields(tuple(changed))
        fields = cls._writable_fields()
        stored = cls.stored_columns(db)
        expressions = (
            changed[item.name] if item.name in changed else item.rebuild_expression(stored)
            for item in fields
        )
        selected = ", ".join(f'{expression} AS {_identifier(item.wire_name)}'
                             for item, expression in zip(fields, expressions, strict=True))
        table = _identifier(cls.declared_name)
        temporary = _identifier(f"rebuild_{cls.declared_name}")
        db.execute(f"CREATE TEMP TABLE {temporary} AS SELECT {selected} FROM {table}")
        cls.drop(db)
        cls.create(db)
        columns = cls._column_list(tuple(item.name for item in fields))
        db.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {temporary}")
        db.execute(f"DROP TABLE {temporary}")

    @classmethod
    def derive_nullable_fields(cls, db: sqlite3.Connection) -> None:
        """Only an exact earlier declaration missing nullable fields can grow rows.

        Extra columns or any other table difference still require a data conversion.
        Compare through the same DDL owner; do not maintain a second column schema.
        """
        stored = cls.stored_columns(db)
        fields = cls._columns()
        missing = tuple(item for item in fields if item.wire_name not in stored)
        if not stored or not missing:
            return
        if not all(item.nullable and item.column.writable for item in missing):
            return
        previous = cls._table_sql(
            columns=tuple(item.name for item in fields if item.wire_name in stored),
        )
        actual = {item.name: item.sql for item in cls.stored_objects(db)}
        if actual.get(cls.declared_name) == previous:
            cls.rebuild(db)

    @classmethod
    def take_previous_name(cls, db: sqlite3.Connection) -> None:
        """Rows stored under a declared earlier name move to the declared one; rows under both
        are refused, since nothing says which to keep."""
        stored = {table.name for table in SQLiteTableName.stored(db)}
        match [name for name in cls.renamed_from if name in stored]:
            case []:
                pass
            case [previous] if cls.declared_name not in stored:
                db.execute(f"ALTER TABLE {_identifier(previous)} RENAME TO "
                           f"{_identifier(cls.declared_name)}")
            case found:
                holders = sorted({*found, cls.declared_name} & stored)
                raise RelationViolationError(
                    f"{cls.declared_name}: tables {holders} could each hold its rows;"
                    " refusing to choose")

    @classmethod
    def stored_columns(cls, db: sqlite3.Connection) -> tuple[str, ...]:
        """The columns the stored table has now, which a carry compares with the declaration."""
        return tuple(name for (name,) in db.execute(
            "SELECT name FROM pragma_table_info(?)", (cls.declared_name,)))

    @classmethod
    def rename_stored_column(cls, db: sqlite3.Connection, stored: str, field_name: str) -> None:
        """A carry renames a stored column to the declared field's column."""
        (declared,) = cls._fields_named((field_name,))
        db.execute(f"ALTER TABLE {_identifier(cls.declared_name)} RENAME COLUMN "
                   f"{_identifier(stored)} TO {_identifier(declared.wire_name)}")

    @classmethod
    def _condition(
        cls, where: str, parameters: tuple, key: dict[str, object]
    ) -> tuple[str, tuple]:
        """Compile predicate field names and declared key values once."""
        selected = cls._fields_named(tuple(key))
        if selected:
            where = "(" + where + ") AND " + " AND ".join(
                f"{_identifier(item.wire_name)} IS ?" for item in selected)
            parameters = (*parameters, *(item.encode(key[item.name]) for item in selected))
        return cls._sql(where), parameters

    @classmethod
    def _query(
        cls, db: sqlite3.Connection, where: str, parameters: tuple,
        order_by: tuple[str, ...], descending: bool, key: dict[str, object],
        row_type: type[RelatedRow] | None, limit: int | None,
    ) -> Generator[Self | RelatedRow, None, None]:
        condition, parameters = cls._condition(where, parameters, key)
        direction, selected = " DESC" if descending else "", row_type or cls
        return selected.iterate(db.execute(
            f"SELECT {selected._select_list(cls)} FROM {_identifier(cls.declared_name)} "
            f"WHERE {condition}"
            + (" ORDER BY " + ", ".join(cls._column_list((name,)) + direction for name in order_by)
               if order_by else "") + (" LIMIT ?" if limit is not None else ""),
            parameters + ((limit,) if limit is not None else ()),
        ))

    @overload
    @classmethod
    def select(
        cls, db: sqlite3.Connection, *, row_type: type[RelatedRow], where: str = "1",
        parameters: tuple = (), order_by: tuple[str, ...] = (), descending: bool = False,
        limit: int | None = None, **key: object,
    ) -> list[RelatedRow]: ...

    @overload
    @classmethod
    def select(
        cls, db: sqlite3.Connection, *, row_type: None = None, where: str = "1",
        parameters: tuple = (), order_by: tuple[str, ...] = (), descending: bool = False,
        limit: int | None = None, **key: object,
    ) -> list[Self]: ...

    @classmethod
    def select(
        cls,
        db: sqlite3.Connection,
        *,
        where: str = "1",
        parameters: tuple = (),
        order_by: tuple[str, ...] = (),
        descending: bool = False,
        row_type: type[RelatedRow] | None = None, limit: int | None = None,
        **key: object,
    ) -> Sequence[Self | RelatedRow]:
        return list(cls._query(db, where, parameters, order_by, descending, key, row_type, limit))

    @overload
    @classmethod
    def stream(
        cls, db: sqlite3.Connection, *, row_type: type[RelatedRow], where: str = "1",
        parameters: tuple = (), order_by: tuple[str, ...] = (), descending: bool = False,
        limit: int | None = None, **key: object,
    ) -> Generator[RelatedRow, None, None]: ...

    @overload
    @classmethod
    def stream(
        cls, db: sqlite3.Connection, *, row_type: None = None, where: str = "1",
        parameters: tuple = (), order_by: tuple[str, ...] = (), descending: bool = False,
        limit: int | None = None, **key: object,
    ) -> Generator[Self, None, None]: ...

    @classmethod
    def stream(
        cls,
        db: sqlite3.Connection,
        *,
        where: str = "1",
        parameters: tuple = (),
        order_by: tuple[str, ...] = (),
        descending: bool = False,
        row_type: type[RelatedRow] | None = None, limit: int | None = None,
        **key: object,
    ) -> Generator[Self | RelatedRow, None, None]:
        """Rows decoded as the caller consumes them; close it to release the query."""
        return cls._query(db, where, parameters, order_by, descending, key, row_type, limit)

    @classmethod
    def qualified(cls, *names: str) -> str:
        """Columns named through this table, for one select joining tables that share names."""
        table = _identifier(cls.declared_name)
        return ", ".join(f"{table}.{_identifier(item.wire_name)}" for item in cls._fields_named(names))

    @classmethod
    def decode_at(cls, row: tuple[object, ...], offset: int) -> Self:
        """Decode this table's columns, selected by ``qualified`` in declaration order at ``offset``."""
        return cls._decode_row(row, {name: offset + index
                                     for index, name in enumerate(cls.column_projection())})

    @classmethod
    def one(cls, db: sqlite3.Connection, **key: object) -> Self | None:
        rows = cls._keyed(db, key)
        return next(iter(rows), None)

    @classmethod
    def require(cls, db: sqlite3.Connection, **key: object) -> Self:
        """The one row a reference names; a missing row is corruption."""
        rows = cls._keyed(db, key)
        if not rows:
            raise RowNotFoundError(f"Expected one {cls.declared_name} for {tuple(key)}")
        return rows[0]

    @classmethod
    def _keyed(cls, db: sqlite3.Connection, key: dict[str, object]) -> list[Self]:
        """At most one row is stored for a key; a repeated key is corruption."""
        stored = cls.select(db, limit=2, **key)
        if len(stored) > 1:
            raise MalformedRecord(f"Expected one {cls.declared_name} for {tuple(key)}")
        return stored

    @classmethod
    def stored_objects(cls, db: sqlite3.Connection) -> list[SQLiteSchemaObject]:
        return TypedTable.objects_of(db, (cls,))

    @staticmethod
    def objects_of(db: sqlite3.Connection, rows: Iterable[type[TypedTable]]) -> list[SQLiteSchemaObject]:
        """The rows' own stored objects, in one catalog read.

        Every object they declare, whichever table a trigger guards, and any object
        attached to their tables that no row's declaration claims: a stray.
        """
        rows = tuple(rows)
        declared = {name for row in rows for name in row.schema_objects()}
        tables = tuple(row.declared_name for row in rows)
        claimed = _claimed_names(TypedTable.members_with(TypedTable))
        stored = SQLiteSchemaObject.read(db.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL AND (name IN ("
            + ",".join("?" for _ in declared) + ") OR tbl_name IN ("
            + ",".join("?" for _ in tables) + "))",
            (*declared, *tables)))
        return [item for item in stored if item.name in declared or item.name not in claimed]

    @classmethod
    def present(cls, db: sqlite3.Connection) -> bool:
        return SqlValue.for_annotation(bool).read(db.execute(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",
            (cls.declared_name,)))[0]

    @classmethod
    def exists(cls, db: sqlite3.Connection, *, where: str = "1", parameters: tuple = (), **key: object) -> bool:
        condition, parameters = cls._condition(where, parameters, key)
        return SqlValue.for_annotation(bool).read(db.execute(
            f"SELECT EXISTS(SELECT 1 FROM {_identifier(cls.declared_name)} WHERE {condition})",
            parameters))[0]

    @classmethod
    def count(cls, db: sqlite3.Connection, *, where: str = "1", parameters: tuple = (), **key: object) -> int:
        condition, parameters = cls._condition(where, parameters, key)
        return SqlValue.for_annotation(int).read(db.execute(
            f"SELECT COUNT(*) FROM {_identifier(cls.declared_name)} WHERE {condition}", parameters))[0]

    @classmethod
    def _writable_fields(cls, columns: tuple[str, ...] | None = None) -> tuple[_Field, ...]:
        if columns is None:
            return tuple(item for item in cls._fields() if item.column.writable)
        selected = cls._fields_named(columns)
        if any(not item.column.writable for item in selected):
            raise ValueError("Generated columns cannot be written")
        return selected

    @classmethod
    def insertion_statement(cls, columns: tuple[str, ...] = ()) -> str:
        writable = cls._writable_fields(columns or None)
        return (f"INSERT INTO {_identifier(cls.declared_name)} "
                f"({cls._column_list(tuple(item.name for item in writable))}) "
                f"VALUES ({', '.join('?' for _ in writable)})")

    @classmethod
    def insert_values(cls, db: sqlite3.Connection, **values: object) -> sqlite3.Cursor:
        writable = cls._writable_fields(tuple(values) or None)
        return db.execute(cls.insertion_statement(tuple(item.name for item in writable)),
                          tuple(item.encode(values[item.name]) for item in writable))

    @classmethod
    def delete(
        cls, db: sqlite3.Connection, *, where: str = "1",
        parameters: tuple = (), **key: object,
    ) -> sqlite3.Cursor:
        condition, parameters = cls._condition(where, parameters, key)
        return db.execute(f"DELETE FROM {_identifier(cls.declared_name)} WHERE {condition}", parameters)

    def _insertion(self) -> tuple[str, tuple]:
        writable = self._writable_fields()
        return (self.insertion_statement(),
                tuple(item.encode(getattr(self, item.name)) for item in writable))

    def insertion(self) -> tuple[str, tuple]:
        """The statement and values that insert this row; a table whose insert differs
        overrides this, so one row and a batch of rows insert alike."""
        return self._insertion()

    def insert(self, db: sqlite3.Connection) -> sqlite3.Cursor:
        return db.execute(*self.insertion())

    def insert_if_absent(self, db: sqlite3.Connection) -> sqlite3.Cursor:
        statement, values = self._insertion()
        return db.execute(statement + " ON CONFLICT DO NOTHING", values)

    def upsert(self, db: sqlite3.Connection) -> sqlite3.Cursor:
        """Replace writable values on a declared primary-key conflict."""
        statement, values = self._insertion()
        keys = tuple(item.name for item in self._fields() if item.column.primary_key)
        updates = tuple(
            item.wire_name
            for item in self._writable_fields()
            if not item.column.primary_key
        )
        statement += f" ON CONFLICT ({self._column_list(keys)}) DO "
        statement += (
            "UPDATE SET "
            + ", ".join(f"{_identifier(name)}=excluded.{_identifier(name)}" for name in updates)
            if updates
            else "NOTHING"
        )
        return db.execute(statement, values)

    @classmethod
    def update(
        cls,
        db: sqlite3.Connection,
        *,
        where: str,
        parameters: tuple = (),
        **changes: object,
    ) -> sqlite3.Cursor:
        selected = cls._writable_fields(tuple(changes))
        if not selected:
            raise ValueError(f"Update requires fields for {cls.declared_name}")
        return db.execute(
            f"UPDATE {_identifier(cls.declared_name)} SET "
            + ", ".join(f"{_identifier(item.wire_name)}=?" for item in selected)
            + f" WHERE {cls._sql(where)}",
            tuple(item.encode(changes[item.name]) for item in selected) + parameters,
        )


@dataclass(frozen=True)
class ProjectionProducer(TypedTable, ProjectionProducerTables):
    projection: Annotated[str, Column(primary_key=True)]
    commit: str

    @classmethod
    def records_for(cls, rows: tuple[type[TypedTable], ...]) -> tuple[Self, ...]:
        return tuple(cls(row.declared_name, STAMP.commit()) for row in rows)

    @classmethod
    def matches(cls, db: sqlite3.Connection, rows: tuple[type[TypedTable], ...]) -> bool:
        if not rows:
            return True
        if not cls.present(db):
            return False
        stored = {record.projection: record for record in cls.select(db)}
        return all(stored.get(record.projection) == record for record in cls.records_for(rows))


class StoreWriter(PublishedSQLiteFile, FileStore):
    """A transaction capability shared by stores and their turn-owned writers.

    Whether the file outlives an installation belongs to each store: it inherits
    CarriedWriter or RuntimeWriter, and a TypedStore inherits DurableStore or DerivedStore.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    @property
    def store(self) -> StoreWriter:
        """The store whose committed row changes this writer publishes."""
        return self

    @contextmanager
    def transaction(self, connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """Publish through the physical store after its row transaction commits."""
        self.require_maintenance_open()
        with self.publishing_changes(connection, self.store.path):
            with self.write_transaction(connection):
                yield connection

    @abstractmethod
    def require_maintenance_open(self) -> None:
        """Whether maintenance refuses this writer, which the file's kind declares."""

    @abstractmethod
    def writing(self) -> AbstractContextManager[sqlite3.Connection]:
        """Borrow one committed write transaction from this lifetime owner."""


class ReconciledFile(SQLiteFile):
    """A SQLite file whose writer reconciles a stored schema that differs from its
    declaration. The installer reconciles every one with the target's runtime under
    maintenance, after the outgoing owners stop, so no owner meets a schema change when
    it opens the file and the backup holds every file the change rewrites."""

    __slots__ = ()

    @classmethod
    @abstractmethod
    def table_schema(cls) -> type[TableSchema]:
        """The declaration this file's schema is reconciled onto."""

    @classmethod
    def schema_at(cls, path: Path) -> type[TableSchema]:
        return cls.table_schema()

    @classmethod
    def schema_change(cls, path: Path) -> SchemaChange:
        return HeldBusFile(cls, path)

    @classmethod
    def reconcile_at(cls, path: Path) -> bool:
        """Whether reconciling the file at ``path`` changed it."""
        schema = cls.schema_at(path)
        with _store_lock(path), closing(cls.writer(path, isolation_level=None)) as db:
            return schema.reconcile_schema(db, cls.schema_change(path))


class CarriedWriter(StoreWriter, DurableFile):
    """A writer of a file every installation carries: while maintenance runs, only its
    holder writes it, so the install's carry check sees no other change."""

    def require_maintenance_open(self) -> None:
        MaintenanceBarrier(RegistryFile.path_in(self.bus_root(self.path))).assert_open_unlocked()


class RuntimeWriter(StoreWriter, RuntimeFile):
    """A writer of a file no carry writes: running owners rewrite it at any time,
    maintenance included."""

    def require_maintenance_open(self) -> None:
        """No installation reads this file, so maintenance never refuses its writers."""


class WalStore(StoreWriter):
    """One WAL file owns connection setup, attachment and retirement."""

    synchronous: ClassVar[str] = "FULL"

    @contextmanager
    def _attached(
        self, opening: Callable[[], AbstractContextManager[sqlite3.Connection]],
    ) -> Iterator[sqlite3.Connection]:
        """Serialize WAL attachment and retirement, not authorized SQL work.

        Even a read-only cold attachment initializes the WAL index. Another
        attachment or last-connection cleanup can make that initialization
        return BUSY before any query competes with a data writer. The same
        physical owner serializes these connection boundaries; readers and
        writers retain SQLite's parallel WAL transactions between them.
        """
        with ExitStack() as lifetime:
            with _store_lock(self.path):
                resource = opening()
                connection = resource.__enter__()

            def retire(*error):
                with _store_lock(self.path):
                    return resource.__exit__(*error)

            lifetime.push(retire)
            yield connection

    @contextmanager
    def _open_writer(self, *, check_same_thread: bool = True) -> Iterator[sqlite3.Connection]:
        """Open one private writer and apply the file's declared WAL policy."""
        # Never open/close an extra descriptor for an existing SQLite inode:
        # doing so can release another connection's process-wide fcntl locks.
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        connection = self.writer(
            self.path, isolation_level=None, check_same_thread=check_same_thread)
        try:
            connection.execute(f"PRAGMA synchronous={self.synchronous}")
            connection.execute("PRAGMA foreign_keys=ON")
            if SQLiteJournalMode.read(connection.execute("PRAGMA journal_mode=WAL")) != [
                SQLiteJournalMode("wal")
            ]:
                raise RelationViolationError(f"{self.filename} requires WAL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def writer_connection(self, *, check_same_thread: bool = True) -> Iterator[sqlite3.Connection]:
        """Own attachment; joined workers may borrow one serialized connection."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._attached(lambda: self._open_writer(
                check_same_thread=check_same_thread)) as connection:
            yield connection

    def _retire_committed(self, lifetime: ExitStack) -> None:
        try:
            lifetime.close()
        except (sqlite3.Error, OSError) as error:
            raise StoreCommittedError(
                f"Committed {self.path}, but connection retirement failed: {error}"
            ) from error

    @contextmanager
    def writing(self) -> Iterator[sqlite3.Connection]:
        with ExitStack() as lifetime:
            connection = lifetime.enter_context(self.writer_connection())
            try:
                with self.transaction(connection):
                    yield connection
            except StoreCommittedError:
                self._retire_committed(lifetime)
                raise
            self._retire_committed(lifetime)


class TypedStore(WalStore, TableSchema):
    """One owner-only SQLite file whose schema its TypedTable rows declare.

    Members name the file and their rows, and inherit DurableStore or DerivedStore.
    A transaction installs an absent store; a snapshot reads it as an empty in-memory
    schema without creating storage. Readers refuse a schema that differs; the
    writer reconciles it as the member's kind of file does.
    """

    rows: ClassVar[tuple[type[TypedTable], ...]]
    root: Path

    @property
    def path(self) -> Path:
        return self.path_in(self.root)

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return cls.rows

    @classmethod
    @abstractmethod
    def schema_change(cls, path: Path) -> SchemaChange:
        """Whether this file's schema belongs to installation or its runtime."""

    @classmethod
    def declares_schema(cls) -> bool:
        return "rows" in vars(cls)

    @contextmanager
    def _open_writer(self, *, check_same_thread: bool = True) -> Iterator[sqlite3.Connection]:
        """Keep this store's schema policy inside its serialized attachment."""
        with super()._open_writer(check_same_thread=check_same_thread) as connection:
            self.install_schema(connection, self.schema_change(self.path))
            yield connection

    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        """Read a committed store, or its declared empty schema before its first write."""
        with ExitStack() as lifetime:
            if self.path.exists():
                connection = lifetime.enter_context(self._attached(
                    lambda: self.observing(self.path, StoreBusy())))
            else:
                connection = lifetime.enter_context(closing(
                    self.writer(":memory:", isolation_level=None)))
                self.create_schema(connection)
                self._configure_reader(connection)
                self.begin_snapshot(connection)
            self.require_schema(connection)
            yield connection


class DurableStore(ReconciledFile, TypedStore, DurableTables, CarriedWriter):
    """Rows nothing else can rebuild, in a file every installation carries."""

    @classmethod
    def table_schema(cls) -> type[TableSchema]:
        return cls


class DerivedStore(TypedStore, DerivedTables, RuntimeWriter):
    """Rows rebuilt from another source, in a file running owners rewrite at any time,
    maintenance included, so no installation backs it up, carries it or checks it.

    The file is named by its declared schema: a runtime on another schema keeps its own
    file instead of rebuilding this one under the writer's lock.
    """

    @classmethod
    def schema_change(cls, path: Path) -> SchemaChange:
        return WriterOwnedFile()

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        schema = json.dumps(cls.declared_schema(), sort_keys=True).encode()
        cls.filename = (
            f"{declared_words(cls.__name__)}.{hashlib.sha256(schema).hexdigest()[:12]}.sqlite3"
        )


class WithoutRowid(TypedTable, declared_name=None):
    """The table is stored directly by its declared primary key."""

    without_rowid = True


@dataclass(frozen=True)
class SingletonRow(TypedTable, declared_name=None):
    """The table has one fixed primary key, preceding its owned value fields."""

    singleton: Annotated[Literal[1], Column(primary_key=True)]
