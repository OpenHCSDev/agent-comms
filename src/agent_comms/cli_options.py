"""CLI projection and nested-record updates owned by their declared fields."""
from __future__ import annotations

import argparse
import json
import types
from collections.abc import Sequence
from dataclasses import MISSING, Field, fields, replace
from enum import Enum
from typing import Any, Callable, ClassVar, Self, get_args, get_origin, get_type_hints

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec


class JsonFlagRecord:
    """A record supplies its JSON-value flag; FieldCodec owns its representation."""

    __slots__ = ()
    value_flag: ClassVar[str]

    def to_json(self) -> str:
        return json.dumps(FieldCodec.encode(self))

    @classmethod
    def from_json(cls, encoded: str | bytes) -> Self:
        return FieldCodec.decode(cls, json.loads(encoded))

    @property
    def cli_arguments(self) -> tuple[str, str]:
        return self.value_flag, self.to_json()

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser, *, required: bool, dest: str) -> None:
        parser.add_argument(
            cls.value_flag, type=cls.from_json, dest=dest, metavar="JSON",
            required=required, default=argparse.SUPPRESS,
        )

    @classmethod
    def from_arguments(cls, arguments: Sequence[str], default: Self | None = None) -> Self:
        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        cls.add_cli_arguments(parser, required=False, dest="record")
        parser.set_defaults(record=default)
        namespace, _ = parser.parse_known_args(arguments)
        if namespace.record is None:
            raise ValueError(f"Missing required {cls.value_flag}")
        return namespace.record


class CliRecord:
    """Compose record-valued CLI fields through their original declarations."""

    @classmethod
    def argument_type(cls):
        return cls

    @classmethod
    def attach_fields(cls, option, parser, settings) -> None:
        declaration = cls.argument_type()
        hints = get_type_hints(declaration)
        for declared in fields(declaration):
            if isinstance(declared, CliOption):
                nested = declared.parser_options(hints[declared.name], settings)
                nested.update(dest=option.name, action=RecordArgument, record_field=declared,
                              settings=settings, default=argparse.SUPPRESS, required=False)
                parser.add_argument(*declared.metadata["flags"], **nested)

    def replace_cli(self, declaration, value, settings):
        return declaration.replace_value(self, value)


class CliOption(Field):
    def parser_options(self, annotation, settings) -> dict[str, Any]:
        metadata = self.metadata
        options = dict(metadata["parser_options"])
        scalar = get_args(annotation)[0] if get_origin(annotation) is types.UnionType else annotation
        if scalar is bool:
            options.setdefault("action", "store_true")
        elif scalar in (int, float):
            options.setdefault("type", scalar)
        elif isinstance(scalar, type) and issubclass(scalar, Enum):
            options["choices"] = [kind.value for kind in scalar]
        elif isinstance(scalar, type) and issubclass(scalar, DeclaredFamily) and "type" not in options:
            options.setdefault("choices", scalar.names())
        elif (get_origin(scalar) is type and get_args(scalar)
              and issubclass(get_args(scalar)[0], DeclaredFamily)):
            options.setdefault("choices", get_args(scalar)[0].names())
        if metadata["setting"] is not MISSING:
            options["default"] = metadata["setting"](settings)
        elif metadata["parser_default"] is not MISSING:
            options["default"] = metadata["parser_default"]
        elif metadata["parser_default_factory"] is not MISSING:
            options["default"] = metadata["parser_default_factory"]()
        elif self.default is not MISSING:
            options["default"] = self.default
        elif self.default_factory is not MISSING:
            options["default"] = self.default_factory()
        else:
            options["required"] = True
        default = options.get("default")
        if isinstance(default, DeclaredFamily) and "type" not in options:
            options["default"] = default.declared_name
        elif isinstance(default, type) and issubclass(default, DeclaredFamily):
            options["default"] = default.declared_name
        if any(flag.startswith("-") for flag in metadata["flags"]):
            options["dest"] = metadata.get("wire_name", self.name)
        else:
            options.pop("required", None)
        return options

    def attach(self, parser, annotation, settings) -> None:
        options = self.parser_options(annotation, settings)
        parser.add_argument(*self.metadata["flags"], **options)
        if isinstance(annotation, type) and issubclass(annotation, CliRecord):
            annotation.attach_fields(self, parser, settings)

    def replace_value(self, record, value):
        normalize = self.metadata["normalize"]
        return replace(record, **{self.name: normalize(value) if normalize is not None else value})


class RecordArgument(argparse.Action):
    def __init__(self, *, record_field: CliOption, settings, **options):
        self.record_field = record_field
        self.settings = settings
        super().__init__(**options)

    def __call__(self, parser, namespace, value, option_string):
        setattr(namespace, self.dest,
                getattr(namespace, self.dest).replace_cli(self.record_field, value, self.settings))


def option(
    *flags: str,
    default: Any = MISSING,
    default_factory: Any = MISSING,
    group: str | None = None,
    normalize: Callable[..., Any] | None = None,
    wire_name: str | None = None,
    parser_default: Any = MISSING,
    parser_default_factory: Any = MISSING,
    setting: Any = MISSING,
    multiline: bool = False,
    target_bound: bool = False,
    editor_format: Callable[[Any], str] | None = None,
    **parser_options: Any,
) -> Any:
    """One field owns its parser, editor and JSON boundary declarations."""
    metadata = {
        "multiline": multiline, "target_bound": target_bound, "editor_format": editor_format,
        "flags": flags, "parser_options": parser_options, "group": group,
        "normalize": normalize, "parser_default": parser_default,
        "parser_default_factory": parser_default_factory, "setting": setting,
    }
    if wire_name is not None:
        metadata["wire_name"] = wire_name
    return CliOption(default=default, default_factory=default_factory, init=True, repr=True,
                     hash=None, compare=True, metadata=metadata, kw_only=MISSING, doc=None)
