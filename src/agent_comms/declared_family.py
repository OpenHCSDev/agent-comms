"""Declaration-owned names and membership, using the shared registry mechanism."""

from __future__ import annotations

import re
from abc import ABC, ABCMeta
from typing import Any, ClassVar, Self, cast

from metaclass_registry import (  # type: ignore[import-untyped]
    AutoRegisterMeta,
    LazyDiscoveryDict,
    RegistryConfig,
)


class _FamilyMeta(AutoRegisterMeta, ABCMeta):
    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        *,
        affix: str | None = None,
        discovery_package: str | None = None,
        **class_options: Any,
    ) -> _FamilyMeta:
        roots = {
            base._family_root
            for base in bases
            if isinstance(base, _FamilyMeta) and base._family_root is not None
        }
        if len(roots) > 1:
            raise TypeError("A declaration cannot belong to two families.")
        root = next(iter(roots), None)
        if root is not None and "family_discriminator" in namespace:
            raise TypeError("Only the family root may declare its discriminator.")
        # Explicit child roots reuse family behavior; a slots-rebuilt root
        # carries its own registry in the copied class namespace.
        is_root = affix is not None or "__registry__" in namespace or (
            root is None and any(isinstance(base, _FamilyMeta) for base in bases)
        )
        if is_root:
            # slots dataclasses recreate the root without its class-header keywords.
            namespace["_family_affix"] = affix if affix is not None else namespace.get("_family_affix", "")
            namespace["__registry__"] = (
                LazyDiscoveryDict(enable_cache=False) if discovery_package else {}
            )
            namespace["declared_name"] = None
        elif root is not None:
            stem = name.removesuffix(root._family_affix) if root._family_affix else name
            derived = re.sub(
                r"([a-z0-9])([A-Z])", r"\1_\2", re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", stem)
            ).lower()
            # A slots dataclass recreates its declaration with the derived name in its namespace.
            namespace["declared_name"] = class_options.pop(
                "declared_name", namespace.get("declared_name", derived)
            )
            if namespace["declared_name"] == "":
                raise TypeError("A family member must have a nonempty name.")
        config = (
            RegistryConfig(
                registry_dict=(
                    namespace["__registry__"]
                    if is_root
                    else cast(type[DeclaredFamily], root).__registry__
                ),
                key_attribute="declared_name",
                skip_if_no_key=True,
                discovery_package=discovery_package,
            )
            if is_root or root is not None
            else None
        )
        cls: Any = super().__new__(
            mcs, name, bases, namespace, registry_config=config, **class_options
        )
        cls._family_root = cls if is_root else root
        return cast(_FamilyMeta, cls)


class DeclaredFamily(ABC, metaclass=_FamilyMeta):
    """A family root owns one registry; names and capability views derive from members.

    Declare a root with ``affix=\"Scope\"`` (or omit it for unstripped names).
    Abstract intermediate classes are excluded by AutoRegisterMeta. An explicit
    ``declared_name=None`` in the class header uses its unnamed-class exclusion.
    Override a
    stored spelling only with ``declared_name=`` at the member declaration.
    A distributed family declares its member package with ``discovery_package``;
    the existing registry acquires those declarations on decode or schema reads.
    """

    declared_name: ClassVar[str]
    family_discriminator: ClassVar[str] = "kind"
    _family_root: ClassVar[type[DeclaredFamily] | None] = None
    _family_affix: ClassVar[str]
    __registry__: ClassVar[dict[str, type[DeclaredFamily]]]

    def __init_subclass__(
        cls,
        *,
        affix: str | None = None,
        declared_name: str | None = None,
        discovery_package: str | None = None,
    ) -> None:
        # The metaclass consumes these declaration options before ABC creation.
        super().__init_subclass__()

    @classmethod
    def decodes_tagged_records(cls) -> bool:
        return True

    def record_tag(self) -> dict[str, str]:
        """Derive the wire selector from the family's decoding behavior."""
        return {self.family_discriminator: self.declared_name} if self.decodes_tagged_records() else {}

    @classmethod
    def decode(cls, name: str) -> type[Self]:
        cls._member_declarations()
        try:
            member = cls.__registry__[name]
        except (KeyError, TypeError):
            raise ValueError(f"Unknown {cls.__name__} name: {name!r}") from None
        if not issubclass(member, cls):
            raise ValueError(f"{name!r} is not a member of {cls.__name__}")
        return cast(type[Self], member)

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(member.declared_name for member in cls.members_with(cls))

    @classmethod
    def field_value(cls, value) -> type[Self]:
        """Coerce one declared selector spelling to its member class."""
        if isinstance(value, type) and issubclass(value, cls):
            return value
        from .field_codec import FieldCodec

        return FieldCodec.decode(type[cls], value)

    @classmethod
    def sql_choices(cls) -> str:
        """The members' names as an SQL ``IN`` list, derived from the declarations."""
        return "(" + ",".join("'" + name.replace("'", "''") + "'" for name in cls.names()) + ")"

    @classmethod
    def _member_declarations(cls) -> tuple[type[DeclaredFamily], ...]:
        """Acquire this family's declarations before the shared capability query."""
        return tuple(cls.__registry__.values())

    @classmethod
    def members_with(cls, capability: type) -> tuple[type[Self], ...]:
        return tuple(
            cast(type[Self], member)
            for member in cls._member_declarations()
            if issubclass(member, cls) and issubclass(member, capability)
        )
