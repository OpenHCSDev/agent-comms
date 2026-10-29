from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from pydantic import JsonValue

from agent_comms import field_codec
from agent_comms.field_codec import FieldCodec, boundary_decoder
from agent_comms.config_options import CatalogChoice, CatalogConfigOption, ConfigOptions
from agent_comms.thread_model import CatalogSelection


@dataclass(frozen=True)
class TargetField(ABC):
    """The declared field owns parsing, editor hints and input presence."""

    plan: field_codec.FieldPlan

    @classmethod
    def of(cls, plan: field_codec.FieldPlan, value: object) -> TargetField:
        editor: type[SuppliedTargetField] = (
            CatalogTargetField
            if field_codec.Catalog.single(plan.annotation)
            else plan.setting(field_codec.Editor, SuppliedTargetField)
        )
        return editor(plan, value)

    @classmethod
    def omitted(cls, plan: field_codec.FieldPlan) -> TargetField:
        if plan.constructor_required:
            return UnsuppliedTargetField(plan)
        return cls.of(plan, plan.default())

    @property
    def annotation(self) -> object:
        return self.plan.annotation

    @property
    def editor_annotation(self) -> object:
        return field_codec.compile_decoder(self.annotation).editor_annotation

    @property
    def name(self) -> str:
        return self.plan.key

    @property
    def multiline(self) -> bool:
        return self.plan.setting(field_codec.Multiline, False)

    @property
    def description(self) -> str:
        return self.plan.description

    @property
    @abstractmethod
    def editor_default(self) -> JsonValue: ...

    @abstractmethod
    def default_schema(self) -> dict[str, JsonValue]: ...

    def argument(self, value: object) -> Mapping[str, object]:
        return {self.name: value}

    def editor_hints(self, choices: Callable[[ChoiceTargetField], object]) -> tuple[object, ...]:
        """An unrestricted field does not declare a finite-choice editor."""
        return ()

    async def prepare(self, catalogs: ConfigOptions) -> TargetField:
        return self

    async def prepare_catalog(self, catalogs: ConfigOptions,
                              selection: CatalogSelection) -> TargetField:
        return await self.prepare(catalogs)

    @property
    def required(self) -> bool:
        return self.plan.constructor_required

    def value_schema(self) -> dict[str, JsonValue]:
        return FieldCodec.value_schema(self.annotation)

    def json_schema(self) -> dict[str, JsonValue]:
        return {**self.value_schema(), 'multiline': self.multiline,
                'description': self.description, **self.default_schema()}


@dataclass(frozen=True)
class SuppliedTargetField(TargetField):
    value: object

    @property
    def editor_default(self) -> JsonValue:
        return self.plan.editor_default(self.value)

    def default_schema(self) -> dict[str, JsonValue]:
        return {'editor_default': self.editor_default}


@dataclass(frozen=True)
class UnsuppliedTargetField(TargetField):
    """A required answer has no value; the optional editor owns its unset control."""

    @property
    def editor_annotation(self) -> object:
        return field_codec.compile_decoder(self.annotation).editor_annotation | type(None)

    @property
    def editor_default(self) -> JsonValue:
        return None

    def default_schema(self) -> dict[str, JsonValue]:
        return {}

    @boundary_decoder
    def argument(self, value: object) -> Mapping[str, object]:
        """Decode the optional control's raw presence once, not the field's wire value."""
        return {} if value is None else super().argument(value)


@dataclass(frozen=True)
class ChoiceTargetField(SuppliedTargetField):
    """A prepared finite declaration, from its annotation or configuration catalog."""
    options: tuple[CatalogChoice, ...] = ()

    @property
    def editor_declarations(self) -> tuple[object, ...]:
        return ()

    @property
    def choices(self) -> tuple[tuple[str, str], ...]:
        return tuple((choice.name, choice.editor_value) for choice in self.options)

    async def prepare(self, catalogs: ConfigOptions) -> ChoiceTargetField:
        decoder = field_codec.compile_decoder(self.annotation)
        options = (CatalogChoice(
                       name=label, value=decoder.encode_editor(decoder.decode(value)))
                   for label, value in decoder.editor_choices())
        return replace(self, options=tuple(options))

    def editor_hints(self, choices: Callable[[ChoiceTargetField], object]) -> tuple[object, ...]:
        return (choices(self),)


class CatalogTargetField(ChoiceTargetField):
    @property
    def catalog(self) -> type[CatalogConfigOption]:
        return self.plan.setting(field_codec.Catalog, CatalogConfigOption)

    @property
    def editor_declarations(self) -> tuple[object, ...]:
        return (self.catalog,)

    async def prepare_catalog(self, catalogs: ConfigOptions,
                              selection: CatalogSelection) -> CatalogTargetField:
        catalog = catalogs.catalog_for(self.catalog)
        options = await catalog.display_choices(selection)
        decoder = field_codec.compile_decoder(self.annotation)
        return replace(self, options=tuple(CatalogChoice(name=item.name,
            value=decoder.encode_editor(decoder.decode(item.value))) for item in options))

    def value_schema(self) -> dict[str, JsonValue]:
        return {**FieldCodec.value_schema(self.annotation),
                'configuration': FieldCodec.encode(self.catalog)}
