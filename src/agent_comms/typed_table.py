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
from dataclasses import dataclass, fields
from functools import lru_cache
from typing import ClassVar, Literal, Self, Union, get_args, get_origin, get_type_hints

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec


def _identifier(name: str) -> str:
    if not name or not name.isascii() or not name.replace("_", "a").isalnum():
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f'"{name}"'


def _base_type(annotation: object) -> tuple[object, bool]:
    if get_origin(annotation) in (Union, types.UnionType):
        members = get_args(annotation)
        if type(None) in members and len(members) == 2:
            return next(member for member in members if member is not type(None)), True
    return annotation, False


class SqlStorage(DeclaredFamily, affix="Storage"):
    """Each SQLite representation owns its conversion at the storage boundary."""

    sql_type: ClassVar[str]

    @classmethod
    @abstractmethod
    def accepts(cls, annotation: object) -> bool: ...

    @classmethod
    def encode(cls, value: object) -> object:
        return FieldCodec.encode(value)

    @classmethod
    def decode(cls, value: object) -> object:
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
    def encode(cls, value: object) -> int:
        return int(value)

    @classmethod
    def decode(cls, value: object) -> bool:
        if type(value) is not int or value not in (0, 1):
            raise ValueError("SQLite boolean must be 0 or 1")
        return bool(value)


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
    def encode(cls, value: object) -> str:
        return json.dumps(FieldCodec.encode(value), separators=(",", ":"), allow_nan=False)

    @classmethod
    def decode(cls, value: object) -> object:
        if type(value) is not str:
            raise ValueError("SQLite JSON must be text")
        return json.loads(value)


@dataclass(frozen=True)
class Column:
    """Constraints on a field; its name and type remain on the dataclass field."""

    primary_key: bool = False
    unique: bool = False
    references: tuple[type[TypedTable], str] | None = None
    check: str | None = None
    index: bool = False


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

    def sql(self, owner: type[TypedTable]) -> str:
        if len(self.columns) != len(self.target_columns):
            raise ValueError("Foreign key column counts differ")
        return (
            f"FOREIGN KEY ({owner._column_list(self.columns)}) "
            f"REFERENCES {_identifier(self.target.declared_name)} "
            f"({self.target._column_list(self.target_columns)})"
            + (" DEFERRABLE INITIALLY DEFERRED" if self.deferred else "")
        )


@dataclass(frozen=True)
class _Field:
    name: str
    annotation: object
    nullable: bool
    storage: type[SqlStorage]
    column: Column

    def encode(self, value: object) -> object:
        # Validate before SQLite can coerce a wrong Python value into its affinity.
        FieldCodec.decode(self.annotation, FieldCodec.encode(value))
        return None if value is None else self.storage.encode(value)

    def decode(self, value: object) -> object:
        return FieldCodec.decode(
            self.annotation, None if value is None else self.storage.decode(value)
        )


class TypedRow:
    """Typed query projection; also the shared decoder for stored row types."""

    @classmethod
    @lru_cache(maxsize=256)
    def _fields(cls) -> tuple[_Field, ...]:
        if not cls.__dataclass_params__.frozen:
            raise TypeError("SQLite row declarations must be frozen dataclasses")
        hints = get_type_hints(cls)
        return tuple(
            _Field(
                item.name,
                hints[item.name],
                _base_type(hints[item.name])[1],
                SqlStorage.for_type(hints[item.name]),
                item.metadata.get("sql", Column()),
            )
            for item in fields(cls)
            if item.init
        )

    @classmethod
    def columns(cls) -> tuple[str, ...]:
        return tuple(item.name for item in cls._fields())

    @classmethod
    def read(cls, cursor: sqlite3.Cursor) -> list[Self]:
        """Decode a whole query once, independently of connection row_factory."""
        if cursor.description is None:
            raise ValueError("Typed read requires a result set")
        names = tuple(item[0] for item in cursor.description)
        if len(set(names)) != len(names) or set(names) != set(cls.columns()):
            raise ValueError(f"Query columns do not match {cls.__name__}: {names!r}")
        positions = {name: index for index, name in enumerate(names)}
        return [
            cls(**{item.name: item.decode(row[positions[item.name]]) for item in cls._fields()})
            for row in cursor
        ]


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
    def ddl(cls) -> tuple[str, ...]:
        definitions = []
        keys = tuple(item.name for item in cls._fields() if item.column.primary_key)
        for item in cls._fields():
            column = item.column
            sql = f"{_identifier(item.name)} {item.storage.sql_type}"
            if not item.nullable or column.primary_key:
                sql += " NOT NULL"
            if column.unique:
                sql += " UNIQUE"
            if column.references is not None:
                target, name = column.references
                target._column_list((name,))
                sql += f" REFERENCES {_identifier(target.declared_name)} ({_identifier(name)})"
            if item.storage is BooleanStorage:
                sql += f" CHECK ({_identifier(item.name)} IN (0, 1))"
            if column.check:
                sql += f" CHECK ({column.check})"
            definitions.append(sql)
        if keys:
            definitions.append(f"PRIMARY KEY ({cls._column_list(keys)})")
        elif cls.without_rowid:
            raise TypeError("WITHOUT ROWID requires a declared primary key")
        definitions.extend(f"UNIQUE ({cls._column_list(group)})" for group in cls.unique)
        definitions.extend(f"CHECK ({check})" for check in cls.checks)
        definitions.extend(reference.sql(cls) for reference in cls.references())
        table = _identifier(cls.declared_name)
        statements = [
            f"CREATE TABLE {table} ({', '.join(definitions)}) STRICT"
            + (", WITHOUT ROWID" if cls.without_rowid else "")
        ]
        indexes = cls.indexes + tuple(
            Index((item.name,)) for item in cls._fields() if item.column.index
        )
        for ordinal, index in enumerate(indexes):
            statements.append(
                f"CREATE {'UNIQUE ' if index.unique else ''}INDEX "
                f"{_identifier(f'{cls.declared_name}_{ordinal}_idx')} ON {table} "
                f"({cls._column_list(index.columns)})"
                + (f" WHERE {index.where}" if index.where else "")
            )
        return tuple(statements)

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

    def insert(self, db: sqlite3.Connection) -> sqlite3.Cursor:
        return db.execute(
            f"INSERT INTO {_identifier(self.declared_name)} "
            f"({self._column_list(self.columns())}) "
            f"VALUES ({', '.join('?' for _ in self._fields())})",
            tuple(item.encode(getattr(self, item.name)) for item in self._fields()),
        )

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
        return db.execute(
            f"UPDATE {_identifier(cls.declared_name)} SET "
            + ", ".join(f"{_identifier(item.name)}=?" for item in selected)
            + f" WHERE {where}",
            tuple(item.encode(changes[item.name]) for item in selected) + parameters,
        )
