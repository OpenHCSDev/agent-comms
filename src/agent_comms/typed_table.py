"""SQLite representation and access derived from frozen row declarations.

The connection and its transaction belong to the caller. This module neither
commits nor upgrades a store: installation creates the declared schema on a
fresh store, and readers accept exactly the declared columns and field types.
"""

from __future__ import annotations

import json
import sqlite3
import types
from abc import abstractmethod
from collections.abc import Generator, Mapping
from dataclasses import dataclass, fields
from enum import Enum, IntEnum, IntFlag
from functools import lru_cache
from typing import (
    Annotated,
    ClassVar,
    Literal,
    Self,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec

RelatedRow = TypeVar("RelatedRow", bound="TypedRow")

def _identifier(name: str) -> str:
    if not name or not name.isascii() or not name.replace("_", "a").isalnum():
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f'"{name}"'


def _base_type(annotation: object) -> tuple[object, bool]:
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) in (Union, types.UnionType):
        members = get_args(annotation)
        if types.NoneType in members and len(members) == 2:
            return next(member for member in members if member is not types.NoneType), True
    return annotation, False


class SqlStorage(DeclaredFamily, affix="Storage"):
    """Each SQLite representation owns its conversion at the storage boundary."""

    sql_type: ClassVar[str]

    @classmethod
    @abstractmethod
    def accepts(cls, annotation: object) -> bool: ...

    @classmethod
    def constraints(cls, column: str, annotation: object = None) -> tuple[str, ...]:
        declared, _ = _base_type(annotation)
        if get_origin(declared) is Literal:
            values = get_args(declared)
        elif isinstance(declared, type) and issubclass(declared, Enum):
            if issubclass(declared, IntFlag):
                return ()  # Combinations belong to the flag declaration's mask constraint.
            values = tuple(member.value for member in declared)
        else:
            return ()
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
        return (f"{_identifier(column)} IN ({choices})",)

    @classmethod
    def to_sql(cls, value: object) -> object:
        return FieldCodec.encode(value)

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
    def to_sql(cls, value: object) -> int:
        return int(value)

    @classmethod
    def from_sql(cls, value: object) -> bool:
        if type(value) is not int or value not in (0, 1):
            raise ValueError("SQLite boolean must be 0 or 1")
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
            and issubclass(annotation, DeclaredFamily)
        )

    @classmethod
    def to_sql(cls, value: object) -> str:
        return json.dumps(FieldCodec.encode(value), separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_sql(cls, value: object) -> object:
        if type(value) is not str:
            raise ValueError("SQLite JSON must be text")
        return json.loads(value)


@dataclass(frozen=True)
class Column:
    """Constraints on a field; its name and type remain on the dataclass field."""

    primary_key: bool = False
    auto_increment: bool = False
    unique: bool = False
    references: tuple[type[TypedTable], str] | None = None
    check: str | None = None
    index: bool = False
    generated: str | None = None
    storage: type[SqlStorage] | None = None
    nullable: bool = False  # SQLite NULL may decode to a mandatory nominal value.


@dataclass(frozen=True)
class Index:
    columns: tuple[str, ...]
    unique: bool = False
    where: str | None = None


@dataclass(frozen=True)
class ForeignKey:
    """Composite references declared beside their row, including cyclic owners."""

    columns: tuple[str, ...]
    target: type[TypedTable]
    target_columns: tuple[str, ...]
    deferred: bool = False
    on_delete: str | None = None

    def sql(self, owner: type[TypedTable]) -> str:
        if len(self.columns) != len(self.target_columns):
            raise ValueError("Foreign key column counts differ")
        return (
            f"FOREIGN KEY ({owner._column_list(self.columns)}) "
            f"REFERENCES {_identifier(self.target.declared_name)} "
            f"({self.target._column_list(self.target_columns)})"
            + (f" ON DELETE {self.on_delete}" if self.on_delete else "")
            + (" DEFERRABLE INITIALLY DEFERRED" if self.deferred else "")
        )


@dataclass(frozen=True)
class _Field:
    name: str
    init: bool
    annotation: object
    nullable: bool
    storage: type[SqlStorage]
    column: Column

    def encode(self, value: object) -> object:
        # Validate before SQLite can coerce a wrong Python value into its affinity.
        FieldCodec.decode(self.annotation, FieldCodec.encode(value, self.annotation))
        return None if value is None else self.storage.to_sql(value)

    def decode(self, value: object) -> object:
        return FieldCodec.decode(
            self.annotation, None if value is None else self.storage.from_sql(value)
        )


def sql_literal(value: str | None) -> str:
    """Quote a declaration-owned SQL constant, never a query parameter."""
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


class TypedRow:
    """Typed query projection; also the shared decoder for stored row types."""

    @classmethod
    @lru_cache(maxsize=256)
    def _fields(cls) -> tuple[_Field, ...]:
        if not cls.__dataclass_params__.frozen:
            raise TypeError("SQLite row declarations must be frozen dataclasses")
        hints = get_type_hints(cls, include_extras=True)
        return tuple(
            _Field(
                item.name,
                item.init,
                hints[item.name],
                _base_type(hints[item.name])[1] or item.metadata.get("sql", Column()).nullable,
                item.metadata.get("sql", Column()).storage or SqlStorage.for_type(hints[item.name]),
                item.metadata.get("sql", Column()),
            )
            for item in fields(cls)
        )

    @classmethod
    def columns(cls) -> tuple[str, ...]:
        return tuple(item.name for item in cls._fields())

    @classmethod
    def _positions(cls, cursor: sqlite3.Cursor, extra: tuple[str, ...] = ()) -> dict[str, int]:
        if cursor.description is None:
            raise ValueError("Typed read requires a result set")
        names = tuple(item[0] for item in cursor.description)
        if len(set(names)) != len(names) or set(names) != set((*cls.columns(), *extra)):
            raise ValueError(f"Query columns do not match {cls.__name__}: {names!r}")
        return {name: index for index, name in enumerate(names)}

    @classmethod
    def _decode_row(cls, row: sqlite3.Row | tuple[object, ...], positions: Mapping[str, int]) -> Self:
        values = {item.name: item.decode(row[positions[item.name]]) for item in cls._fields()}
        instance = cls(**{item.name: values[item.name] for item in cls._fields() if item.init})
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
    def joined(cls, cursor: sqlite3.Cursor, related: type[RelatedRow]) -> list[tuple[Self, RelatedRow]]:
        """Decode disjoint joined declarations once without another partial row schema.

        All actual columns must belong to exactly these declarations; duplicate,
        missing and extra columns are rejected by the same strict query boundary.
        This reads one SQLite snapshot and never re-encodes a stored row as JSON.
        """
        try:
            positions = cls._positions(cursor, related.columns())
            return [(cls._decode_row(row, positions), related._decode_row(row, positions))
                    for row in cursor]
        finally:
            cursor.close()

    @classmethod
    def read(cls, cursor: sqlite3.Cursor) -> list[Self]:
        """Decode a complete bounded query through the same streaming boundary."""
        return list(cls.iterate(cursor))


@dataclass(frozen=True)
class SQLiteSchemaObject(TypedRow):
    name: str
    sql: str


@dataclass(frozen=True)
class SQLiteForeignKeys(TypedRow):
    foreign_keys: bool


@dataclass(frozen=True)
class SQLiteJournalMode(TypedRow):
    journal_mode: str


@dataclass(frozen=True)
class SQLiteUserVersion(TypedRow):
    user_version: int


class TypedTable(TypedRow, DeclaredFamily, affix="Row"):
    """The row declaration owns its table; no separate table/schema registry.

    Fields may declare ``metadata={"sql": Column(...)}``. Composite keys derive
    from primary-key fields in declaration order. Table-wide constraints and
    indexes live on the same row declaration. SQL predicates contain trusted
    source expressions; values always use bound parameters.
    """

    unique: ClassVar[tuple[tuple[str, ...], ...]] = ()
    checks: ClassVar[tuple[str, ...]] = ()
    indexes: ClassVar[tuple[Index, ...]] = ()
    without_rowid: ClassVar[bool] = False

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        # A method permits mutually referring row classes without string names.
        return ()

    @classmethod
    def _column_list(cls, names: tuple[str, ...]) -> str:
        if not names or not set(names) <= set(cls.columns()):
            raise ValueError(f"Unknown or empty column list for {cls.declared_name}: {names}")
        return ", ".join(map(_identifier, names))

    @classmethod
    def schema_objects(cls) -> dict[str, str]:
        definitions = []
        keys = tuple(item.name for item in cls._fields() if item.column.primary_key)
        auto_keys = tuple(item.name for item in cls._fields() if item.column.auto_increment)
        if auto_keys and (auto_keys != keys or len(keys) != 1 or cls.without_rowid):
            raise TypeError("AUTOINCREMENT requires one rowid primary key")
        for item in cls._fields():
            column = item.column
            sql = f"{_identifier(item.name)} {item.storage.sql_type}"
            if column.auto_increment:
                sql += " PRIMARY KEY AUTOINCREMENT"
            if column.generated is not None:
                sql += f" GENERATED ALWAYS AS ({column.generated}) STORED"
            if not item.nullable or column.primary_key:
                sql += " NOT NULL"
            if column.unique:
                sql += " UNIQUE"
            if column.references is not None:
                target, name = column.references
                target._column_list((name,))
                sql += f" REFERENCES {_identifier(target.declared_name)} ({_identifier(name)})"
            for constraint in item.storage.constraints(item.name, item.annotation):
                sql += f" CHECK ({constraint})"
            if column.check:
                sql += f" CHECK ({column.check})"
            definitions.append(sql)
        if keys and not auto_keys:
            definitions.append(f"PRIMARY KEY ({cls._column_list(keys)})")
        elif cls.without_rowid:
            raise TypeError("WITHOUT ROWID requires a declared primary key")
        definitions.extend(f"UNIQUE ({cls._column_list(group)})" for group in cls.unique)
        definitions.extend(f"CHECK ({check})" for check in cls.checks)
        definitions.extend(reference.sql(cls) for reference in cls.references())
        table = _identifier(cls.declared_name)
        statements = {
            cls.declared_name: f"CREATE TABLE {table} ({', '.join(definitions)}) STRICT"
            + (", WITHOUT ROWID" if cls.without_rowid else "")
        }
        indexes = cls.indexes + tuple(
            Index((item.name,)) for item in cls._fields() if item.column.index
        )
        for ordinal, index in enumerate(indexes):
            index_name = f"{cls.declared_name}_{ordinal}_idx"
            statements[index_name] = (
                f"CREATE {'UNIQUE ' if index.unique else ''}INDEX "
                f"{_identifier(index_name)} ON {table} "
                f"({cls._column_list(index.columns)})"
                + (f" WHERE {index.where}" if index.where else "")
            )
        statements.update(cls.triggers())
        return statements

    @classmethod
    def triggers(cls) -> dict[str, str]:
        return {}

    @classmethod
    def ddl(cls) -> tuple[str, ...]:
        return tuple(cls.schema_objects().values())

    @classmethod
    def create(cls, db: sqlite3.Connection) -> None:
        for statement in cls.ddl():
            db.execute(statement)

    @classmethod
    def select(
        cls,
        db: sqlite3.Connection,
        *,
        where: str = "1",
        parameters: tuple = (),
        order_by: tuple[str, ...] = (),
    ) -> list[Self]:
        return cls.read(
            db.execute(
                f"SELECT {cls._column_list(cls.columns())} FROM {_identifier(cls.declared_name)} "
                f"WHERE {where}" + (f" ORDER BY {cls._column_list(order_by)}" if order_by else ""),
                parameters,
            )
        )

    @classmethod
    def one(cls, db: sqlite3.Connection, **key: object) -> Self | None:
        cls._column_list(tuple(key))
        selected = tuple(item for item in cls._fields() if item.name in key)
        rows = cls.select(
            db,
            where=" AND ".join(f"{_identifier(item.name)} IS ?" for item in selected),
            parameters=tuple(item.encode(key[item.name]) for item in selected),
        )
        if len(rows) > 1:
            raise ValueError(f"Expected one {cls.declared_name} for {tuple(key)}")
        return next(iter(rows), None)

    def _insertion(self) -> tuple[str, tuple]:
        writable = tuple(item for item in self._fields() if item.column.generated is None)
        return (
            f"INSERT INTO {_identifier(self.declared_name)} "
            f"({self._column_list(tuple(item.name for item in writable))}) "
            f"VALUES ({', '.join('?' for _ in writable)})",
            tuple(item.encode(getattr(self, item.name)) for item in writable),
        )

    def insert(self, db: sqlite3.Connection) -> sqlite3.Cursor:
        return db.execute(*self._insertion())

    def upsert(self, db: sqlite3.Connection) -> sqlite3.Cursor:
        """Replace writable values on a declared primary-key conflict."""
        statement, values = self._insertion()
        keys = tuple(item.name for item in self._fields() if item.column.primary_key)
        updates = tuple(
            item.name
            for item in self._fields()
            if not item.column.primary_key and item.column.generated is None
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
        cls._column_list(tuple(changes))
        selected = tuple(item for item in cls._fields() if item.name in changes)
        if any(item.column.generated is not None for item in selected):
            raise ValueError("Generated columns cannot be updated")
        return db.execute(
            f"UPDATE {_identifier(cls.declared_name)} SET "
            + ", ".join(f"{_identifier(item.name)}=?" for item in selected)
            + f" WHERE {where}",
            tuple(item.encode(changes[item.name]) for item in selected) + parameters,
        )
