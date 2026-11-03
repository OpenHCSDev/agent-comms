"""Bindings from Python's generic declarations, shared by record fields and union members."""
from __future__ import annotations

import types
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Annotated, TypeVar, Union, cast, get_args, get_origin


class TypeArgumentMismatch(TypeError):
    """A fixed member specialization does not belong to the requested generic family."""


def substitute(annotation: object, bindings: Mapping[object, object]) -> object:
    """Resolve the foreign typing expression; metadata is not another type expression."""
    if isinstance(annotation, TypeVar):
        value = bindings.get(annotation, annotation)
        return annotation if value is annotation else substitute(value, bindings)
    origin, arguments = get_origin(annotation), get_args(annotation)
    if not arguments:
        return annotation
    if origin is Annotated:
        value, *metadata = arguments
        return Annotated[substitute(value, bindings), *metadata]
    resolved = tuple(substitute(argument, bindings) for argument in arguments)
    if resolved == arguments:
        return annotation
    if origin in (Union, types.UnionType):
        return Union[resolved]
    return origin[resolved[0] if len(resolved) == 1 else resolved]


def match(pattern: object, actual: object, bindings: dict[object, object]) -> None:
    """Unify a member's inherited type expression with the requested family arguments."""
    pattern = substitute(pattern, bindings)
    actual = substitute(actual, bindings)
    if pattern == actual:
        return
    if isinstance(pattern, TypeVar):
        bindings[pattern] = actual
        return
    expected_origin, actual_origin = get_origin(pattern), get_origin(actual)
    expected_args, actual_args = get_args(pattern), get_args(actual)
    if expected_origin is None or expected_origin != actual_origin or len(expected_args) != len(actual_args):
        raise TypeArgumentMismatch(f"{pattern} does not match {actual}")
    for expected, supplied in zip(expected_args, actual_args, strict=True):
        match(expected, supplied, bindings)


@dataclass(frozen=True)
class TypeArguments:
    """One binding scope, including arguments inherited through intermediate bases."""

    bindings: tuple[tuple[object, object], ...] = ()

    @classmethod
    def of(cls, annotation: object) -> TypeArguments:
        origin = cast(type, get_origin(annotation) or annotation)
        arguments = get_args(annotation)
        values: dict[object, object] = dict(zip(origin.__type_params__, arguments, strict=True)) if arguments else {}
        pending = [origin]
        visited: set[type] = set()
        while pending:
            declaration = pending.pop()
            if declaration in visited:
                continue
            visited.add(declaration)
            for base in types.get_original_bases(declaration):
                parent = cast(type, get_origin(base) or base)
                # Built-in/object/typing.Generic bases declare no PEP695 parameters.
                parameters = getattr(parent, "__type_params__", ())
                supplied = tuple(substitute(item, values) for item in get_args(base))
                if parameters and supplied:
                    for parameter, value in zip(parameters, supplied, strict=True):
                        match(values.get(parameter, parameter), value, values)
                pending.append(parent)
        return cls(tuple(values.items()))

    def resolve(self, annotation: object) -> object:
        return substitute(annotation, dict(self.bindings))

    @classmethod
    def member(cls, family: object, member: type) -> object:
        root = cast(type, get_origin(family) or family)
        inherited = cls.of(member)
        bindings: dict[object, object] = {}
        for parameter, actual in zip(root.__type_params__, get_args(family), strict=True):
            match(inherited.resolve(parameter), actual, bindings)
        arguments = tuple(substitute(parameter, bindings) for parameter in member.__type_params__)
        return member[arguments] if arguments else member

    @classmethod
    def members(cls, family: object) -> Iterator[object]:
        root = cast(type, get_origin(family) or family)
        if not get_args(family):
            yield from root.members_with(root)
            return
        for member in root.members_with(root):
            try:
                bound = cls.member(family, member)
            except TypeArgumentMismatch:
                # A fixed Foo[int] declaration is not a case of Foo[str].
                continue
            yield bound
