"""`python -m agent_comms.cli_native_package VERB ...`: the stack scripts' native package verbs."""

from __future__ import annotations

import argparse
import json
import sys
from abc import abstractmethod
from dataclasses import dataclass

from .cli_commands import DeclaredOptions, option
from .declared_family import DeclaredFamily
from .native_package import (
    install_declared_sources,
    native_environment,
    package_tree_digest,
    pinned_package_in,
    share_native_resources,
    verify_native_package,
    write_native_manifest,
)
from .storage_rules import StoragePath, StorageRule


@dataclass(frozen=True, kw_only=True)
class NativePackageVerb(DeclaredFamily, DeclaredOptions, affix="Verb"):
    """One verb: its options are its fields, its parser comes from them, apply() does the work."""

    @abstractmethod
    def apply(self, ctx: None = None) -> None: ...

    @classmethod
    def parse(cls, argv: list[str]) -> NativePackageVerb:
        parser = argparse.ArgumentParser(prog="agent_comms.cli_native_package", description=__doc__)
        verbs = parser.add_subparsers(dest="command", required=True)
        for member in cls.members_with(cls):
            member.add_parser(verbs)
        args = parser.parse_args(argv)
        return cls.decode(args.command).from_namespace(args)


@dataclass(frozen=True, kw_only=True)
class VerifyVerb(NativePackageVerb):
    help = "Verify a copied package against the source pin"
    package: StoragePath = option("package", help="the pi-coding-agent package directory")

    def apply(self, ctx: None = None) -> None:
        verify_native_package(self.package)


@dataclass(frozen=True, kw_only=True)
class DigestVerb(NativePackageVerb):
    help = "Print a package tree's digest"
    package: StoragePath = option("package", help="the pi-coding-agent package directory")

    def apply(self, ctx: None = None) -> None:
        print(package_tree_digest(self.package))


@dataclass(frozen=True, kw_only=True)
class WriteManifestVerb(NativePackageVerb):
    help = "Write the source pin from a built package"
    package: StoragePath = option("package", help="the pi-coding-agent package directory")

    def apply(self, ctx: None = None) -> None:
        write_native_manifest(self.package)


@dataclass(frozen=True, kw_only=True)
class ShareResourcesVerb(NativePackageVerb):
    help = "Share a new package's resources with the existing deployments"
    package: StoragePath = option("package", help="the new pi-coding-agent package directory")
    deployments: StoragePath = option("deployments", help="the directory holding the deployments")

    def apply(self, ctx: None = None) -> None:
        print(json.dumps(share_native_resources(self.package, self.deployments)), file=sys.stderr)


@dataclass(frozen=True, kw_only=True)
class InstallSourcesVerb(NativePackageVerb):
    help = "Install the declared agent-comms sources into a package"
    package: StoragePath = option("package", help="the pi-coding-agent package directory")
    snapshot: StoragePath = option("snapshot", help="the checkout the sources come from")

    def apply(self, ctx: None = None) -> None:
        verify_native_package(self.package)
        install_declared_sources(self.package, self.snapshot)


@dataclass(frozen=True, kw_only=True)
class EnvironmentVerb(NativePackageVerb):
    help = "Print this pin's test environment directory"
    cache: StoragePath = option(
        "--cache",
        default=StorageRule.native_cache,
        help="the root holding one test environment per pin",
    )

    def apply(self, ctx: None = None) -> None:
        print(native_environment(self.cache))


@dataclass(frozen=True, kw_only=True)
class PackageVerb(NativePackageVerb):
    help = "Print where this pin's built package sits under a root laid out as stack/ is"
    root: StoragePath = option("root", help="a stack directory or test environment")

    def apply(self, ctx: None = None) -> None:
        print(pinned_package_in(self.root))


@dataclass(frozen=True, kw_only=True)
class BuildVerb(NativePackageVerb):
    help = "Print the directory holding this pin's built package under a root laid out as stack/ is"
    root: StoragePath = option("root", help="a stack directory or test environment")

    def apply(self, ctx: None = None) -> None:
        print(pinned_package_in(self.root).parents[2])


if __name__ == "__main__":
    # The family registers its members under this module's import name, not __main__.
    from agent_comms import cli_native_package

    cli_native_package.NativePackageVerb.parse(sys.argv[1:]).apply()
