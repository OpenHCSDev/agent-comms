"""Declaration-owned names and membership, using the shared registry mechanism."""

from __future__ import annotations

import pkgutil
import re
from abc import ABC, ABCMeta, abstractmethod
from collections.abc import Hashable, Iterable, Mapping
from functools import cache
from types import ModuleType
from typing import ClassVar, Self, cast

from metaclass_registry import (
    AutoRegisterMeta,
    DuplicateRegistryKeyError,
    LazyDiscoveryDict,
    RegistryConfig,
)
from metaclass_registry.discovery import import_module_preserving_root_logging

from .mro_dispatch import MroDispatch, handles


def declared_words(stem: str) -> str:
    """A class-name stem as its declared name: ``StallWatcher`` -> ``stall_watcher``."""
    return re.sub(
        r"([a-z0-9])([A-Z])", r"\1_\2", re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", stem)
    ).lower()


class FamilyMeta(AutoRegisterMeta, ABCMeta):
    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, object],
        *,
        affix: str | None = None,
        discovery_package: str | None = None,
        **class_options: object,
    ) -> FamilyMeta:
        family_bases = FamilyBases.of(bases)
        roots = {
            base._family_root
            for base in family_bases
            if base is not DeclaredFamily
        }
        if len(roots) > 1:
            raise TypeError("A declaration cannot belong to two families.")
        root = next(iter(roots), None)
        if root is not None and "family_discriminator" in namespace:
            raise TypeError("Only the family root may declare its discriminator.")
        # Explicit child roots reuse family behavior; a slots-rebuilt root
        # carries its own registry in the copied class namespace.
        is_root = affix is not None or "__registry__" in namespace or (
            not roots and bool(family_bases)
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
            derived = declared_words(stem)
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
        cls: type[DeclaredFamily] = super().__new__(
            mcs, name, bases, namespace, registry_config=config, **class_options
        )
        if is_root:
            cls._family_root = cls
        else:
            for family_root in roots:
                cls._family_root = family_root
        return cast(FamilyMeta, cls)

    @staticmethod
    def _register_class(
        cls: type[DeclaredFamily], key: Hashable, config: RegistryConfig,
        replaces: object = None,
    ) -> type | None:
        # Check constructed declarations; lazy discovery cannot run inside class construction.
        cls.validate_selection(dict.values(config.registry_dict))
        return AutoRegisterMeta._register_class(cls, key, config, replaces=replaces)


class FamilyBases(MroDispatch):
    """What each base contributes to its family, by the base's metaclass."""

    @handles(FamilyMeta)
    @staticmethod
    def declared(base: FamilyMeta) -> tuple[FamilyMeta, ...]:
        return (base,)

    @handles(type)
    @staticmethod
    def plain(base: type) -> tuple[FamilyMeta, ...]:
        return ()

    @classmethod
    def of(cls, bases: tuple[type, ...]) -> tuple[FamilyMeta, ...]:
        return tuple(family for base in bases for family in cls.handler_for(type(base))(base))


class UnknownFamilyMember(ValueError):
    """The selected name is not a declared member of the requested family."""


class DeclaredOrder(ABC):
    """Plain rank capabilities ordered by their direct declarations."""

    @classmethod
    def position_of(cls, member: type) -> int:
        declarations = cls.__subclasses__()
        for base in member.__mro__:
            if base in declarations:
                return declarations.index(base)
        raise ValueError(f"{member.__name__} has no {cls.__name__} rank base")


class DeclaredFamily(ABC, metaclass=FamilyMeta):
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
    _family_root: ClassVar[type[DeclaredFamily]]
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

    @classmethod
    def selection_key(cls) -> Hashable:
        """The declaration's unique selector at its external boundary."""
        return cls.declared_name

    @classmethod
    def validate_selection(cls, members: Iterable[type[DeclaredFamily]]) -> None:
        """Named declarations use the registry's existing duplicate-name check."""

    def record_tag(self) -> dict[str, str]:
        """Derive the wire selector from the family's decoding behavior."""
        return {self.family_discriminator: self.declared_name} if self.decodes_tagged_records() else {}

    @classmethod
    def member_named(cls, name: object) -> type[Self]:
        cls._member_declarations()
        try:
            member = cast(Mapping[object, type[DeclaredFamily]], cls.__registry__)[name]
        except (KeyError, TypeError):
            raise UnknownFamilyMember(f"Unknown {cls.__name__} name: {name!r}") from None
        if not issubclass(member, cls):
            raise UnknownFamilyMember(f"{name!r} is not a member of {cls.__name__}")
        return cast(type[Self], member)

    @classmethod
    def member_or_default(cls, name: object, capability: type) -> type[Self]:
        """An absent or unknown external name selects one declared default member."""
        try:
            return cls.member_named(name)
        except UnknownFamilyMember:
            member, = cls.members_with(capability)
            return member

    @classmethod
    def member_selected(cls, selector: Hashable) -> type[Self]:
        """Resolve an external selector through the original member declarations."""
        for member in cls.members_with(cls):
            if member.selection_key() == selector:
                return member
        raise UnknownFamilyMember(f"Unknown {cls.__name__} selector: {selector!r}")

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
    def discover_members(cls, package: ModuleType, capability: type) -> tuple[type[Self], ...]:
        """Acquire a producer package before deriving its declared capability members."""
        DeclaredFamily.discover_package(package)
        return cls.members_with(capability)

    @staticmethod
    @cache
    def discover_package(package: ModuleType) -> None:
        """Import each module of the package once, so its members register themselves."""
        for module in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
            if not module.ispkg:
                import_module_preserving_root_logging(module.name)

    @classmethod
    def members_with(cls, capability: type) -> tuple[type[Self], ...]:
        return tuple(
            cast(type[Self], member)
            for member in cls._member_declarations()
            if issubclass(member, cls) and issubclass(member, capability)
        )

    @classmethod
    def family_root(cls) -> type[DeclaredFamily]:
        """The nominal root recorded when this declaration was constructed."""
        return cls._family_root

    @classmethod
    def declared_position(cls) -> int:
        root = cls.family_root()
        return root.members_with(root).index(cls)


class UniqueExternalSelector(ABC):
    """An external selector has one concrete owner among the family's declarations."""

    declared_name: ClassVar[str]

    @classmethod
    @abstractmethod
    def selection_key(cls) -> Hashable: ...

    @classmethod
    def validate_selection(cls, members: Iterable[type[DeclaredFamily]]) -> None:
        for member in members:
            if (
                member.declared_name != cls.declared_name
                and member.selection_key() == cls.selection_key()
            ):
                raise DuplicateRegistryKeyError(
                    f"{cls.__name__} and {member.__name__} declare the same "
                    f"external selector {cls.selection_key()!r}"
                )
