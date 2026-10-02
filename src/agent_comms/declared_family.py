"""Declaration-owned names and membership, using the shared registry mechanism."""

from __future__ import annotations

import re
from abc import ABC, ABCMeta
from typing import Any, ClassVar, Self, cast

from metaclass_registry import AutoRegisterMeta, RegistryConfig  # type: ignore[import-untyped]


class _FamilyMeta(AutoRegisterMeta, ABCMeta):
    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        *,
        affix: str | None = None,
        declared_name: str | None = None,
    ) -> _FamilyMeta:
        roots = {
            base._family_root
            for base in bases
            if isinstance(base, _FamilyMeta) and base._family_root is not None
        }
        if len(roots) > 1:
            raise TypeError("A declaration cannot belong to two families.")
        root = next(iter(roots), None)
        if affix is not None and root is not None:
            raise TypeError("A member cannot start another family.")
        is_root = affix is not None or (
            root is None and any(isinstance(base, _FamilyMeta) for base in bases)
        )
        if is_root:
            namespace["_family_affix"] = affix or ""
            namespace["__registry__"] = {}
            namespace["declared_name"] = None
        elif root is not None:
            stem = name.removesuffix(root._family_affix) if root._family_affix else name
            derived = re.sub(
                r"([a-z0-9])([A-Z])", r"\1_\2", re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", stem)
            ).lower()
            # A slots dataclass recreates its declaration with the derived name in its namespace.
            namespace["declared_name"] = declared_name or namespace.get("declared_name") or derived
            if not namespace["declared_name"]:
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
            )
            if is_root or root is not None
            else None
        )
        cls: Any = super().__new__(mcs, name, bases, namespace, registry_config=config)
        cls._family_root = cls if is_root else root
        return cast(_FamilyMeta, cls)

    @staticmethod
    def _register_class(cls: Any, key: str, config: RegistryConfig) -> None:
        previous = config.registry_dict.get(key)
        if previous is not None:
            # dataclass(slots=True) creates a replacement class, not a new member.
            slot_replacement = (
                "__slots__" in cls.__dict__
                and "__slots__" not in previous.__dict__
                and "__dataclass_fields__" in cls.__dict__
                and cls.__dict__["__dataclass_fields__"]
                is previous.__dict__.get("__dataclass_fields__")
                and (cls.__module__, cls.__qualname__)
                == (previous.__module__, previous.__qualname__)
            )
            if not slot_replacement:
                raise TypeError(
                    f"Duplicate family name {key!r}: {previous.__name__}, {cls.__name__}"
                )
        AutoRegisterMeta._register_class(cls, key, config)


class DeclaredFamily(ABC, metaclass=_FamilyMeta):
    """A family root owns one registry; names and capability views derive from members.

    Declare a root with ``affix=\"Scope\"`` (or omit it for unstripped names).
    Abstract intermediate classes are excluded by AutoRegisterMeta. Override a
    stored spelling only with ``declared_name=`` at the member declaration.
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
    ) -> None:
        # The metaclass consumes these declaration options before ABC creation.
        super().__init_subclass__()

    @classmethod
    def decode(cls, name: str) -> type[Self]:
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
    def members_with(cls, capability: type) -> tuple[type[Self], ...]:
        return tuple(
            cast(type[Self], member)
            for member in cls.__registry__.values()
            if issubclass(member, cls) and issubclass(member, capability)
        )
