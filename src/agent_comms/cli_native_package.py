"""`python -m agent_comms.cli_native_package VERB ...`: the stack scripts' native package verbs."""

from __future__ import annotations

import json
import sys
from abc import abstractmethod
from dataclasses import dataclass
from typing import Annotated

from agent_comms import field_codec
from agent_comms.command import Command
from agent_comms.declared_verb import DeclaredVerb
from agent_comms.native_build_inputs import built_tree_pin
from agent_comms.native_package import (
    MANIFEST,
    VerifiedNativePackage,
    install_declared_sources,
    package_tree_digest,
    pinned_native_package,
    share_native_resources,
    write_native_manifest,
)
from agent_comms.storage_rules import StoragePath
from agent_comms.unit_instructions import UnitInstructions


@dataclass(frozen=True, kw_only=True)
class NativePackageVerb(DeclaredVerb, Command[None, None], affix="Verb"):
    """One verb: its fields declare its parser, and apply() does the work."""

    @abstractmethod
    def apply(self, ctx: None = None) -> None: ...

    @classmethod
    def tool(cls) -> str:
        return cls.__module__


@dataclass(frozen=True, kw_only=True)
class PackageVerb(NativePackageVerb):
    """A command operating on the explicitly supplied package directory."""

    package: Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("package",), options={"help": "the pi-coding-agent package directory"}
        ),
    ]


@dataclass(frozen=True, kw_only=True)
class QualifiedPackageVerb(PackageVerb):
    """A build/publication boundary qualifies the mutable package before using it."""

    def _native_package(self) -> VerifiedNativePackage:
        package = VerifiedNativePackage.qualify(self.package)
        print(UnitInstructions.prepare(), file=sys.stderr)
        return package


@dataclass(frozen=True, kw_only=True)
class VerifyVerb(QualifiedPackageVerb):
    """Qualify a copied build without publishing it."""

    help = "Verify a copied package against the source pin"

    def apply(self, ctx: None = None) -> None:
        self._native_package()


@dataclass(frozen=True, kw_only=True)
class BuildIdVerb(NativePackageVerb):
    help = "Print which built tree this checkout's manifest pins"

    def apply(self, ctx: None = None) -> None:
        print(built_tree_pin(MANIFEST))


@dataclass(frozen=True, kw_only=True)
class DigestVerb(PackageVerb):
    help = "Print a package tree's digest"

    def apply(self, ctx: None = None) -> None:
        print(package_tree_digest(self.package))


@dataclass(frozen=True, kw_only=True)
class WriteManifestVerb(PackageVerb):
    help = "Write the source pin from a built package"

    def apply(self, ctx: None = None) -> None:
        write_native_manifest(self.package)


@dataclass(frozen=True, kw_only=True)
class ShareResourcesVerb(PackageVerb):
    help = "Share a new package's resources with the existing deployments"
    deployments: Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("deployments",), options={"help": "the directory holding the deployments"}
        ),
    ]

    def apply(self, ctx: None = None) -> None:
        print(json.dumps(share_native_resources(self.package, self.deployments)), file=sys.stderr)


@dataclass(frozen=True, kw_only=True)
class SourceInstallationVerb(NativePackageVerb):
    """Install declared sources after the command's package boundary accepts them."""

    snapshot: Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("snapshot",), options={"help": "the checkout the sources come from"}
        ),
    ]

    @abstractmethod
    def _native_package(self) -> VerifiedNativePackage: ...

    def apply(self, ctx: None = None) -> None:
        package = self._native_package()
        install_declared_sources(package.path, self.snapshot)
        self.answer(package)

    def answer(self, package: VerifiedNativePackage) -> None:
        """Publication answers nothing: its output is the preparation script's."""


@dataclass(frozen=True, kw_only=True)
class InstallSourcesVerb(SourceInstallationVerb):
    """Runtime source installation acquires the package its manifest pins, and answers
    that package's location for the launcher."""

    help = "Install declared sources into this runtime's pinned package and print it"

    def _native_package(self) -> VerifiedNativePackage:
        return VerifiedNativePackage.acquire(pinned_native_package())

    def answer(self, package: VerifiedNativePackage) -> None:
        print(package.path)


@dataclass(frozen=True, kw_only=True)
class PublishSourcesVerb(QualifiedPackageVerb, SourceInstallationVerb):
    """Build publication qualifies a mutable package before installing its sources."""

    help = "Qualify a build package and install its declared sources before publication"


if __name__ == "__main__":
    # The family registers its members under this module's import name, not __main__.
    from agent_comms import cli_native_package

    cli_native_package.NativePackageVerb.parse(sys.argv[1:]).apply()
