"""`python -m agent_comms.cli_native_package VERB ...`: the stack scripts' native package verbs."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import typing
from abc import abstractmethod
from dataclasses import dataclass

from agent_comms import field_codec

from .cli_commands import DeclaredOptions
from .command import Command
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
class NativePackageVerb(DeclaredFamily, DeclaredOptions, Command, affix="Verb"):
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
        return cls.member_named(args.command).from_arguments(args)


@dataclass(frozen=True, kw_only=True)
class VerifyVerb(NativePackageVerb):
    help = "Verify a copied package against the source pin"
    package: typing.Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("package",), options={"help": "the pi-coding-agent package directory"}
        ),
    ]

    def apply(self, ctx: None = None) -> None:
        verify_native_package(self.package)


@dataclass(frozen=True, kw_only=True)
class DigestVerb(NativePackageVerb):
    help = "Print a package tree's digest"
    package: typing.Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("package",), options={"help": "the pi-coding-agent package directory"}
        ),
    ]

    def apply(self, ctx: None = None) -> None:
        print(package_tree_digest(self.package))


@dataclass(frozen=True, kw_only=True)
class WriteManifestVerb(NativePackageVerb):
    help = "Write the source pin from a built package"
    package: typing.Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("package",), options={"help": "the pi-coding-agent package directory"}
        ),
    ]

    def apply(self, ctx: None = None) -> None:
        write_native_manifest(self.package)


@dataclass(frozen=True, kw_only=True)
class ShareResourcesVerb(NativePackageVerb):
    help = "Share a new package's resources with the existing deployments"
    package: typing.Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("package",), options={"help": "the new pi-coding-agent package directory"}
        ),
    ]
    deployments: typing.Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("deployments",), options={"help": "the directory holding the deployments"}
        ),
    ]

    def apply(self, ctx: None = None) -> None:
        print(json.dumps(share_native_resources(self.package, self.deployments)), file=sys.stderr)


@dataclass(frozen=True, kw_only=True)
class InstallSourcesVerb(NativePackageVerb):
    help = "Install the declared agent-comms sources into a package"
    package: typing.Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("package",), options={"help": "the pi-coding-agent package directory"}
        ),
    ]
    snapshot: typing.Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("snapshot",), options={"help": "the checkout the sources come from"}
        ),
    ]

    def apply(self, ctx: None = None) -> None:
        verify_native_package(self.package)
        install_declared_sources(self.package, self.snapshot)


@dataclass(frozen=True, kw_only=True)
class EnvironmentVerb(NativePackageVerb):
    help = "Print this pin's test environment directory"
    cache: typing.Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("--cache",), options={"help": "the root holding one test environment per pin"}
        ),
    ] = dataclasses.field(default=StorageRule.native_cache)

    def apply(self, ctx: None = None) -> None:
        print(native_environment(self.cache))


@dataclass(frozen=True, kw_only=True)
class PackageVerb(NativePackageVerb):
    help = "Print where this pin's built package sits under a root laid out as stack/ is"
    root: typing.Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("root",), options={"help": "a stack directory or test environment"}
        ),
    ]

    def apply(self, ctx: None = None) -> None:
        print(pinned_package_in(self.root))


@dataclass(frozen=True, kw_only=True)
class BuildVerb(NativePackageVerb):
    help = "Print the directory holding this pin's built package under a root laid out as stack/ is"
    root: typing.Annotated[
        StoragePath,
        field_codec.Argument(
            flags=("root",), options={"help": "a stack directory or test environment"}
        ),
    ]

    def apply(self, ctx: None = None) -> None:
        print(pinned_package_in(self.root).parents[2])


if __name__ == "__main__":
    # The family registers its members under this module's import name, not __main__.
    from agent_comms import cli_native_package

    cli_native_package.NativePackageVerb.parse(sys.argv[1:]).apply()
