"""Declaration-owned names and membership, using the shared registry mechanism."""

from __future__ import annotations

import pkgutil
import re
from abc import ABC, ABCMeta, abstractmethod
from collections.abc import Hashable, Iterable
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
from metaclass_registry.mro_dispatch import (
    MroDispatch,
    handles,
)

from agent_comms.nominal.class_fact import ClassFact, Declarations


def declared_words(stem: str) -> str:
    """A class-name stem as its declared name: ``StallWatcher`` -> ``stall_watcher``."""
    return re.sub(
        r"([a-z0-9])([A-Z])", r"\1_\2", re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", stem)
    ).lower()


class FamilyMeta(AutoRegisterMeta, ABCMeta):
    """A family takes its members by declaration, so membership is the member's MRO.

    ABCMeta answers a negative check by asking every subclass in the tree, looking for
    virtual members ``register`` added outside a class body; a family has none, and its
    tree is thousands of classes deep. Abstract methods stay enforced by ABCMeta.
    """

    __instancecheck__ = type.__instancecheck__
    __subclasscheck__ = type.__subclasscheck__

    def register[S](cls, subclass: type[S]) -> type[S]:
        raise TypeError(
            f"{cls.__name__} takes members only by declaration; {subclass.__name__} must "
            f"inherit it to belong."
        )

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
            base.family_root
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
            registry = namespace["__registry__"] = (
                LazyDiscoveryDict(enable_cache=False) if discovery_package else {}
            )
            namespace["declared_name"] = None
            config = namespace["__registry_config__"] = RegistryConfig(
                registry_dict=registry,
                key_attribute="declared_name",
                skip_if_no_key=True,
                discovery_package=discovery_package,
                # A family's package is its whole tree: members in a subpackage are members.
                discovery_recursive=True,
            )
        elif root is not None:
            config = root.__registry_config__
            stem = name.removesuffix(root._family_affix) if root._family_affix else name
            derived = declared_words(stem)
            # A slots dataclass recreates its declaration with the derived name in its namespace.
            namespace["declared_name"] = class_options.pop(
                "declared_name", namespace.get("declared_name", derived)
            )
            if namespace["declared_name"] == "":
                raise TypeError("A family member must have a nonempty name.")
        else:
            config = None
        cls: type[DeclaredFamily] = super().__new__(
            mcs, name, bases, namespace, registry_config=config, **class_options
        )
        if is_root:
            cls.family_root = cls
        else:
            for family_root in roots:
                cls.family_root = family_root
        # Registered now: facts derived from membership are derived again on their next read.
        Declarations.changed()
        return cast(FamilyMeta, cls)

    @staticmethod
    def _register_class(
        cls: type[DeclaredFamily], key: Hashable, config: RegistryConfig,
        replaces: object = None,
    ) -> type | None:
        # Check constructed declarations; lazy discovery cannot run inside class construction.
        cls.validate_module_name()
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


class FamilyMember(ABC):
    """The nominal family root and the name declared by each of its members."""

    declared_name: ClassVar[str]
    family_root: ClassVar[type[DeclaredFamily]]


class DeclaredFamily(FamilyMember, ABC, metaclass=FamilyMeta):
    """A family root owns one registry; names and capability views derive from members.

    Declare a root with ``affix=\"Scope\"`` (or omit it for unstripped names).
    Abstract intermediate classes are excluded by AutoRegisterMeta. An explicit
    ``declared_name=None`` in the class header uses its unnamed-class exclusion.
    Override a
    stored spelling only with ``declared_name=`` at the member declaration.
    A distributed family declares its member package with ``discovery_package``;
    the existing registry acquires those declarations on decode or schema reads.
    """

    family_discriminator: ClassVar[str] = "kind"
    _family_affix: ClassVar[str]
    __registry__: ClassVar[dict[str, type[DeclaredFamily]]]
    __registry_config__: ClassVar[RegistryConfig]

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
    def withdraw(cls) -> None:
        """Remove this declaration from its family, as declaring added it: facts derived from
        membership (a table's CHECK choices) derive again on their next read."""
        del cls.__registry__[cls.declared_name]
        Declarations.changed()

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
    def validate_module_name(cls) -> None:
        """Ordinary families may group several declarations in one module."""

    @classmethod
    def _named_member(cls, name: object) -> type[DeclaredFamily]:
        cls._member_declarations()
        return cls.__registry__[cast(Hashable, name)]

    @classmethod
    def recorded_member(cls, name: object) -> type[Self]:
        """The member a stored or wire name selects; only a declared name selects one."""
        return cls.member_named(name)

    @classmethod
    def member_named(cls, name: object) -> type[Self]:
        # Resolution acquires declarations the same way members_with does. A family whose
        # declarations arrive from a producer package (tool_declaration imports `tools` here)
        # would otherwise answer UnknownFamilyMember for a name that is merely not imported yet,
        # and for_name turns that miss into an UndeclaredTool terminal.
        cls._member_declarations()
        try:
            member = cls._named_member(name)
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
    def sql_choices(cls) -> str:
        """The members' names as an SQL ``IN`` list, derived from the declarations.

        SQL IN is an unordered set: stored schema text must not depend on import order."""
        return "(" + ",".join(sorted("'" + name.replace("'", "''") + "'" for name in cls.names())) + ")"

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
        """Import each module of the package's tree once, so its members register themselves."""
        for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            if not module.ispkg:
                import_module_preserving_root_logging(module.name)

    @ClassFact
    @staticmethod
    def _members(cls: type[DeclaredFamily]) -> tuple[type[DeclaredFamily], ...]:
        """This class's members in declaration order, derived once per set of declarations:
        acquiring them (a discovery import, an entry point) declares more, which re-derives it."""
        return tuple(member for member in cls._member_declarations() if issubclass(member, cls))

    @ClassFact
    @staticmethod
    def _capability_members(cls: type[DeclaredFamily]) -> dict[type, tuple[type[DeclaredFamily], ...]]:
        """Each capability's members of this class, filled as capabilities are asked for and
        dropped with the declarations they were derived from."""
        return {}

    @classmethod
    def members_with(cls, capability: type) -> tuple[type[Self], ...]:
        views = cls._capability_members
        if capability not in views:
            views[capability] = tuple(
                member for member in cls._members if issubclass(member, capability))
        return cast(tuple[type[Self], ...], views[capability])


    @classmethod
    def declared_position(cls) -> int:
        root = cls.family_root
        return root.members_with(root).index(cls)


class ModuleMembers(FamilyMember):
    """A family whose declared names select exactly one Python module each."""

    @staticmethod
    def module_stem(name: str) -> str:
        return name.replace("-", "_")

    @classmethod
    def validate_module_name(cls) -> None:
        actual = cls.__module__.rpartition(".")[2]
        expected = cls.module_stem(cls.declared_name)
        if actual != expected:
            raise TypeError(f"{cls.__qualname__} declares {cls.declared_name!r}; "
                            f"its module stem must be {expected!r}, not {actual!r}")


class RegisteredFamily(DeclaredFamily):
    """Instances are members of their family's declared registry, never unnamed helpers."""

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        # Construction uses declarations already registered by FamilyMeta. Discovery
        # must not run here: it may itself be constructing another family member.
        if dict.get(cls.__registry__, cls.declared_name) is not cls:
            raise TypeError(f"{cls.__name__} is not a registered {cls.family_root.__name__} member")
        return super().__new__(cls)


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
