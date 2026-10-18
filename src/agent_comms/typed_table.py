"""SQLite representation and access derived from frozen row declarations.

The connection and its transaction belong to the caller. This module neither
commits nor upgrades a store: installation creates the declared schema on a
fresh store, and readers accept exactly the declared columns and field types.
"""

from __future__ import annotations

import json
import os
import sqlite3
import types
from abc import abstractmethod
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager
from dataclasses import dataclass, fields
from enum import Enum, IntEnum, IntFlag
from functools import lru_cache
from pathlib import Path
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

from .locked_store import RootFile
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, MalformedRecord, ValueDecoder, WireValue, compile_decoder

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
    # A value whose own representation owns JSON null, such as a NullMember, stores NULL.
    return annotation, isinstance(annotation, type) and issubclass(annotation, WireValue) and (
        annotation.accepts_null
    )


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
        if get_origin(annotation) is Annotated:
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
    def to_sql(cls, value: object) -> str:
        return json.dumps(FieldCodec.encode(value), separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_sql(cls, value: object) -> object:
        if type(value) is not str:
            raise MalformedRecord("SQLite JSON must be text")
        return FieldCodec.parse(value)


@dataclass(frozen=True)
class Column:
    """Constraints on a field; its name and type remain on the dataclass field."""

    primary_key: bool = False
    record_key: bool = False
    auto_increment: bool = False
    unique: bool = False
    references: tuple[type[TypedTable], str] | None = None
    check: str | None = None
    index: bool = False
    generated: str | None = None
    storage: type[SqlStorage] | None = None
    nullable: bool = False  # SQLite NULL may decode to a mandatory nominal value.

    @property
    def writable(self) -> bool:
        return self.generated is None

    @property
    def generated_sql(self) -> str:
        return "" if self.writable else f" GENERATED ALWAYS AS ({self.generated}) STORED"


@dataclass(frozen=True)
class Index:
    columns: tuple[str, ...]
    unique: bool = False
    where: str | None = None

    def sql(self, owner: type[TypedTable], name: str, *, if_not_exists: bool = False) -> str:
        existence = "IF NOT EXISTS " if if_not_exists else ""
        return (
            f"CREATE {'UNIQUE ' if self.unique else ''}INDEX "
            f"{existence}{_identifier(name)} ON {_identifier(owner.declared_name)} "
            f"({owner._column_list(self.columns)})"
            + (f" WHERE {owner._sql(self.where)}" if self.where else "")
        )


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
class SqlValue:
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
            if cursor.description is None or len(cursor.description) != 1:
                raise MalformedRecord("Scalar projection requires exactly one SQLite column")
            return [self.decode(row[0]) for row in cursor]
        finally:
            cursor.close()

    def encode(self, value: object) -> object:
        # Validate before SQLite can coerce a wrong Python value into its affinity.
        encoded = FieldCodec.encode(value, self.annotation)
        self.value.decode(encoded)
        return None if encoded is None else self.storage.to_sql(encoded)

    def decode(self, value: object) -> object:
        return self.value.decode(None if value is None else self.storage.from_sql(value))


@dataclass(frozen=True)
class _Field(SqlValue):
    name: str
    wire_name: str
    init: bool
    nullable: bool
    column: Column


def sql_literal(value: str | None | type[DeclaredFamily]) -> str:
    """Quote a declaration-owned SQL constant, never a query parameter."""
    text = FieldCodec.decode(str | None, FieldCodec.encode(value))
    return "NULL" if text is None else "'" + text.replace("'", "''") + "'"


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
                name=item.name,
                wire_name=(FieldCodec.wire_key(item)
                           if item.metadata.get("sql", Column()).record_key else item.name),
                init=item.init,
                annotation=hints[item.name],
                nullable=_base_type(hints[item.name])[1] or item.metadata.get("sql", Column()).nullable,
                storage=item.metadata.get("sql", Column()).storage or SqlStorage.for_type(hints[item.name]),
                column=item.metadata.get("sql", Column()),
                value=compile_decoder(hints[item.name]),
            )
            for item in fields(cls)
        )

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
    def _positions(cls, cursor: sqlite3.Cursor,
                   extra: Mapping[str, str] = types.MappingProxyType({})) -> dict[str, int]:
        if cursor.description is None:
            raise ValueError("Typed read requires a result set")
        names = tuple(item[0] for item in cursor.description)
        projection = {**cls.column_projection(), **extra}
        if len(set(names)) != len(names) or set(names) != set(projection.values()):
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
class SQLiteForeignKeys(TypedRow):
    foreign_keys: bool


@dataclass(frozen=True)
class SQLiteJournalMode(TypedRow):
    journal_mode: str


@dataclass(frozen=True)
class SQLiteUserVersion(TypedRow):
    user_version: int


class TypedTable(TypedRow, DeclaredFamily, affix="Row", discovery_package="agent_comms"):
    """The row declaration owns its table; no separate table/schema registry.

    Fields may declare ``metadata={"sql": Column(...)}``. Composite keys derive
    from primary-key fields in declaration order. Table-wide constraints and
    indexes live on the same row declaration. SQL predicates contain trusted
    source expressions; values always use bound parameters.
    """

    family_discriminator: ClassVar[str] = "row"
    unique: ClassVar[tuple[tuple[str, ...], ...]] = ()
    checks: ClassVar[tuple[str, ...]] = ()
    indexes: ClassVar[tuple[Index, ...]] = ()
    without_rowid: ClassVar[bool] = False

    @classmethod
    def _member_declarations(cls) -> tuple[type[DeclaredFamily], ...]:
        """Enumerate the schema owner's already imported row declarations.

        Schema owners load their row modules before querying capabilities. A
        checkpoint read must not import unrelated application modules while
        holding its bus lock. External table-name decoding still uses the
        registry's complete lazy discovery through DeclaredFamily.decode.
        """
        return tuple(dict.values(cls.__registry__))

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        # A method permits mutually referring row classes without string names.
        return ()

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
        for item in cls._fields():
            expression = expression.replace("{" + item.name + "}", item.wire_name)
        return expression

    @classmethod
    def schema_objects(cls, *, if_not_exists: bool = False) -> dict[str, str]:
        existence = "IF NOT EXISTS " if if_not_exists else ""
        definitions = []
        keys = tuple(item.name for item in cls._fields() if item.column.primary_key)
        auto_keys = tuple(item.name for item in cls._fields() if item.column.auto_increment)
        if auto_keys and (auto_keys != keys or len(keys) != 1 or cls.without_rowid):
            raise TypeError("AUTOINCREMENT requires one rowid primary key")
        for item in cls._fields():
            column = item.column
            sql = f"{_identifier(item.wire_name)} {item.storage.sql_type}"
            if column.auto_increment:
                sql += " PRIMARY KEY AUTOINCREMENT"
            sql += cls._sql(column.generated_sql)
            if not item.nullable or column.primary_key:
                sql += " NOT NULL"
            if column.unique:
                sql += " UNIQUE"
            if column.references is not None:
                target, name = column.references
                sql += f" REFERENCES {_identifier(target.declared_name)} ({target._column_list((name,))})"
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
        statements = {
            cls.declared_name: f"CREATE TABLE {existence}{table} ({', '.join(definitions)}) STRICT"
            + (", WITHOUT ROWID" if cls.without_rowid else "")
        }
        indexes = cls.indexes + tuple(
            Index((item.name,)) for item in cls._fields() if item.column.index
        )
        for ordinal, index in enumerate(indexes):
            index_name = f"{cls.declared_name}_{ordinal}_idx"
            statements[index_name] = index.sql(cls, index_name, if_not_exists=if_not_exists)
        statements.update(cls.triggers(if_not_exists=if_not_exists))
        return statements

    @classmethod
    def trigger_bodies(cls) -> dict[str, str]:
        """Named trigger suffixes, including their leading SQL whitespace."""
        return {}

    @classmethod
    def triggers(cls, *, if_not_exists: bool = False) -> dict[str, str]:
        existence = "IF NOT EXISTS " if if_not_exists else ""
        return {name: f"CREATE TRIGGER {existence}{name}{cls._sql(body)}"
                for name, body in cls.trigger_bodies().items()}

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
    ) -> list[Self | RelatedRow]:
        return list(cls._query(db, where, parameters, order_by, descending, key, row_type, limit))

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
    def one(cls, db: sqlite3.Connection, **key: object) -> Self | None:
        rows = cls.select(db, **key)
        if len(rows) > 1:
            raise MalformedRecord(f"Expected one {cls.declared_name} for {tuple(key)}")
        return next(iter(rows), None)

    @classmethod
    def present(cls, db: sqlite3.Connection) -> bool:
        return SqlValue.for_annotation(bool).read(db.execute(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",
            (cls.declared_name,)))[0]

    @classmethod
    def exists(cls, db: sqlite3.Connection, *, where: str = "1", parameters: tuple = ()) -> bool:
        return SqlValue.for_annotation(bool).read(db.execute(
            f"SELECT EXISTS(SELECT 1 FROM {_identifier(cls.declared_name)} WHERE {cls._sql(where)})",
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

    def insert(self, db: sqlite3.Connection) -> sqlite3.Cursor:
        return db.execute(*self._insertion())

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


class TypedStore(RootFile):
    """One owner-only SQLite file whose schema its TypedTable rows declare.

    Members name the file and their rows. A transaction installs an absent
    store; a snapshot reads it as an empty in-memory schema without creating
    storage. An existing store whose schema differs from its declaration raises.
    """

    rows: ClassVar[tuple[type[TypedTable], ...]]
    synchronous: ClassVar[str] = "FULL"
    root: Path

    @property
    def path(self) -> Path:
        return self.path_in(self.root)

    @classmethod
    def declared_schema(cls) -> dict[str, str]:
        return {name: sql for row in cls.rows for name, sql in row.schema_objects().items()}

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> list[SQLiteSchemaObject]:
        return SQLiteSchemaObject.read(connection.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"))

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
        from .store_files import _store_lock

        with ExitStack() as lifetime:
            with _store_lock(self.path):
                resource = opening()
                connection = resource.__enter__()

            def retire(*error):
                with _store_lock(self.path):
                    return resource.__exit__(*error)

            lifetime.push(retire)
            yield connection

    def _open_writer(self) -> sqlite3.Connection:
        """Attach and install an absent store under the connection boundary."""
        from .coordination_database import CoordinationStore

        # Never open/close an extra descriptor for an existing SQLite inode:
        # doing so can release another connection's process-wide fcntl locks.
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        connection = CoordinationStore.writer(self.path, isolation_level=None)
        try:
            connection.execute(f"PRAGMA synchronous={self.synchronous}")
            # This query attaches an existing WAL before releasing the boundary.
            if not self._schema(connection):
                connection.execute("BEGIN IMMEDIATE")
                if not self._schema(connection):
                    for row in self.rows:
                        row.create(connection)
                connection.execute("COMMIT")
            if SQLiteJournalMode.read(connection.execute("PRAGMA journal_mode=WAL")) != [
                SQLiteJournalMode("wal")
            ]:
                raise RelationViolationError(f"{self.filename} requires WAL")
            return connection
        except BaseException:
            connection.close()
            raise

    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        """Read a committed store, or its declared empty schema before its first write."""
        from .coordination_database import CoordinationStore, StoreBusy

        with ExitStack() as lifetime:
            if self.path.exists():
                connection = lifetime.enter_context(self._attached(
                    lambda: CoordinationStore.observing(self.path, StoreBusy())))
            else:
                connection = lifetime.enter_context(closing(
                    sqlite3.connect(":memory:", isolation_level=None)))
                for row in self.rows:
                    row.create(connection)
                CoordinationStore._configure_reader(connection)
                CoordinationStore.begin_snapshot(connection)
            self.require_schema(self._schema(connection))
            yield connection

    def require_schema(self, actual: list[SQLiteSchemaObject]) -> None:
        if SQLiteSchemaObject.differences(actual, self.declared_schema()):
            raise RelationViolationError(f"{self.filename} differs from its declaration")

    @contextmanager
    def writing(self) -> Iterator[sqlite3.Connection]:
        with self._attached(lambda: closing(self._open_writer())) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self.require_schema(self._schema(connection))
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
