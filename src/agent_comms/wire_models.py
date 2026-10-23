"""Validated wire models compile their schemas when their boundary first needs them."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, TypeAdapter
from pydantic._internal._model_construction import ModelMetaclass

from .declared_family import DeclaredFamily


class WireModel(BaseModel):
    """Strict fields at an external protocol boundary."""

    model_config = ConfigDict(strict=True, defer_build=True)


class _WireFamilyMeta(type(DeclaredFamily), ModelMetaclass):
    """Compose the family's existing metaclass with Pydantic model construction."""


class DeclaredWireFamily(DeclaredFamily, BaseModel, metaclass=_WireFamilyMeta):
    """A validated wire model whose family owns declaration discovery."""

    declared_name: ClassVar[str]
    family_discriminator: ClassVar[str]
    _family_root: ClassVar[type[DeclaredFamily] | None]
    _family_affix: ClassVar[str]
    __registry__: ClassVar[dict[str, type[DeclaredFamily]]]


class WireAdapter[Value](TypeAdapter[Value]):
    """A wire boundary owns its compiled adapter from its first validation onward."""

    def __init__(self, declaration: object) -> None:
        # Account for this constructor when Pydantic captures the declaring namespace.
        super().__init__(declaration, config=ConfigDict(defer_build=True), _parent_depth=3)
