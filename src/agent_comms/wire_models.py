"""Validated wire models compile their schemas when their boundary first needs them."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, TypeAdapter
from pydantic._internal._model_construction import ModelMetaclass
from typing_extensions import TypeForm

from .declared_family import DeclaredFamily, FamilyMeta


class WireModel(BaseModel):
    """Strict fields at an external protocol boundary."""

    model_config = ConfigDict(strict=True, defer_build=True)

    @staticmethod
    def adapter[Value](declaration: TypeForm[Value]) -> TypeAdapter[Value]:
        """Construct a deferred wire adapter in its declaration's namespace."""
        return TypeAdapter[Value](
            declaration,
            config=ConfigDict(defer_build=True),
            _parent_depth=3,
        )


class _WireFamilyMeta(FamilyMeta, ModelMetaclass):
    """Compose the family's existing metaclass with Pydantic model construction."""


class DeclaredWireFamily(DeclaredFamily, BaseModel, metaclass=_WireFamilyMeta):
    """A validated wire model whose family owns declaration discovery."""

    declared_name: ClassVar[str]
