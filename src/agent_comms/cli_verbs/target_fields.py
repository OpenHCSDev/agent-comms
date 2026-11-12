from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import cast

from pydantic import JsonValue

from agent_comms.config_options import CatalogChoice, CatalogConfigOption, ConfigOptions
from agent_comms.nominal import field_codec
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.thread_model import CatalogSelection


@dataclass(frozen=True)
class TargetField:
    """The declared field owns parsing, editor hints and requiredness."""

    plan: field_codec.FieldPlan
    value: object

    @classmethod
    def of(cls, plan: field_codec.FieldPlan, value: object) -> TargetField:
        editor: type[TargetField] = (
            CatalogTargetField
            if field_codec.Catalog.single(plan.annotation)
            else plan.setting(field_codec.Editor, plan.value.select_editor(cls, ChoiceTargetField))
        )
        return editor(plan, value)

    @property
    def annotation(self) -> object:
        return self.plan.annotation

    @property
    def name(self) -> str:
        return self.plan.key

    @property
    def multiline(self) -> bool:
        return self.plan.setting(field_codec.Multiline, False)

    @property
    def label(self) -> str:
        return self.plan.label

    @property
    def description(self) -> str:
        return self.plan.setting(field_codec.Description, self.plan.description)

    @property
    def editor_default(self) -> JsonValue:
        return self.plan.editor_default(self.value)

    def editor_hints(self, choices: Callable[[ChoiceTargetField], object]) -> tuple[object, ...]:
        """An unrestricted field does not declare a finite-choice editor."""
        return ()

    async def prepare(self, catalogs: ConfigOptions, selection: object) -> TargetField:
        return self

    def offered(self) -> tuple[CatalogChoice, ...]:
        """The choices preparation found; an unrestricted field offers none."""
        return ()

    def with_offered(self, options: tuple[CatalogChoice, ...]) -> TargetField:
        """A field prepared elsewhere keeps the choices it was offered there."""
        return self

    @property
    def required(self) -> bool:
        return self.plan.constructor_required

    def value_schema(self) -> dict[str, JsonValue]:
        return FieldCodec.value_schema(self.annotation)

    def json_schema(self) -> dict[str, JsonValue]:
        return {**self.value_schema(), 'multiline': self.multiline,
                'label': self.label, 'description': self.description, 'editor_default': self.editor_default}


@dataclass(frozen=True)
class ChoiceTargetField(TargetField):
    """A prepared finite declaration, from its annotation or configuration catalog."""
    options: tuple[CatalogChoice, ...] = ()

    @property
    def editor_declarations(self) -> tuple[object, ...]:
        return ()

    @property
    def choices(self) -> tuple[tuple[str, str], ...]:
        return tuple((choice.name, choice.editor_value) for choice in self.options)

    def offered(self) -> tuple[CatalogChoice, ...]:
        return self.options

    def with_offered(self, options: tuple[CatalogChoice, ...]) -> ChoiceTargetField:
        return replace(self, options=options)

    async def prepare(self, catalogs: ConfigOptions,
                      selection: object) -> ChoiceTargetField:
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
        return CatalogConfigOption.implementing(self.plan.setting(field_codec.Catalog, CatalogConfigOption))

    @property
    def label(self) -> str:
        return self.catalog.declared_field().label

    @property
    def description(self) -> str:
        return self.catalog.declared_field().setting(field_codec.Description, "")

    @property
    def editor_declarations(self) -> tuple[object, ...]:
        return (self.catalog,)

    async def prepare(self, catalogs: ConfigOptions,
                      selection: object) -> CatalogTargetField:
        catalog = catalogs.catalog_for(self.catalog)
        options = await catalog.display_choices(cast(CatalogSelection, selection))
        decoder = field_codec.compile_decoder(self.annotation)
        return replace(self, options=tuple(CatalogChoice(name=item.name,
            value=decoder.encode_editor(decoder.decode(item.value))) for item in options))

    def value_schema(self) -> dict[str, JsonValue]:
        return {**FieldCodec.value_schema(self.annotation),
                'configuration': FieldCodec.encode(self.catalog)}
