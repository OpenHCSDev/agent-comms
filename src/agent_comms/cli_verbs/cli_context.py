"""The context verb and its option-selected implementation family."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_verb import CliBus, CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.context_store import LATEST_CONTEXT, ContextManifestCursor
from agent_comms.coordination.errors import Refused
from agent_comms.execution.context_provenance import Provenance, Sha256Digest
from agent_comms.execution.turn_context import (
    ContextManifest,
    ContextSegment,
    ContributionCoordinates,
    NextContextTurn,
    ProviderContext,
    TurnContext,
)
from agent_comms.nominal.field_codec import Argument, FieldCodec
from agent_comms.pi_payloads import NativeContextData
from agent_comms.transport.owner_connection import OwnerConnection, socket_path


@dataclass(frozen=True)
class CoreSegmentText:
    kind: type[ContextSegment]
    provenance: tuple[Provenance, ...]
    text: str


@dataclass(frozen=True, kw_only=True)
class ContextInspectionInput:
    """The supplied input, core context and segments a context inspection reads."""

    input_supplied: bool
    core_context: TurnContext
    segments: tuple[CoreSegmentText, ...]


@dataclass(frozen=True, kw_only=True)
class CoreContextInspection(ContextInspectionInput, CliResult):
    """Current core contributors; native_error says why the native base is absent."""
    scope: str
    native_error: str


@dataclass(frozen=True)
class NativeSourceSpans(Sha256Digest):
    segment: int
    spans: tuple[ContributionCoordinates, ...]


@dataclass(frozen=True, kw_only=True)
class NativeContextInspection(ContextInspectionInput, CliResult):
    """The next native base together with the current core contributors."""
    scope: str
    native_manifest: tuple[ContextSegment, ...]
    native_provider_context: ProviderContext
    native_source_spans: tuple[NativeSourceSpans, ...]


@dataclass(frozen=True, kw_only=True)
class CurrentContextCliCommand(CliCommand, declared_name=None):
    help = "Inspect original context contributors without sending an input"
    thread: Annotated[str, Argument(flags=("thread",))]

    @classmethod
    def add_parser[P: argparse.ArgumentParser](cls, subparsers: argparse._SubParsersAction[P]) -> None:
        super().add_parser(subparsers)
        parser = subparsers.choices[cls.declared_name]
        parser.add_argument("--turn", type=int, default=argparse.SUPPRESS)
        parser.add_argument("--diff", action="store_true")
        parser.add_argument("--imported", action="store_true")


    def manifests(self, ctx: CliBus) -> Iterator[ContextManifest]:
        cursor: ContextManifestCursor = LATEST_CONTEXT
        while True:
            page = ctx.contexts.page(self.thread, ctx.registry, cursor=cursor)
            yield from (resource.value for resource in reversed(page.resources))
            if not page.has_older():
                return
            cursor = page.older

    def apply(self, ctx: CliBus) -> CliResult:
        owner = ctx.registry.require(self.thread)
        connection = OwnerConnection(ctx, owner.incarnation, socket_path(ctx.root, owner.require_process().pid))

        async def inspect() -> CliResult:
            try:
                context = FieldCodec.decode(TurnContext, await connection.request("context_core"))
                if context.thread != owner.incarnation:
                    raise ValueError("Core context belongs to another owner incarnation")
                segments = tuple(CoreSegmentText(type(segment), segment.provenance, segment.text())
                                 for segment in context.segments)
                try:
                    native = FieldCodec.decode(
                        NativeContextData, await connection.request("context")
                    ).require_session_file(owner.runtime.path)
                except Refused as error:
                    return CoreContextInspection(scope="current-core-contributors; before future input",
                                                 input_supplied=False, core_context=context, segments=segments, native_error=str(error))
            finally:
                await connection.close()
            native_context = native.for_turn(owner.incarnation, NextContextTurn())
            return NativeContextInspection(
                scope="next-native-base-and-core-contributors; before future input and provider hooks",
                input_supplied=False, core_context=context, segments=segments, native_manifest=native_context.segments, native_provider_context=native_context.render().provider,
                native_source_spans=tuple(NativeSourceSpans(segment=position, sha256=segment.measured_manifest().sha256,
                                        spans=segment.source_ranges())
                      for position, segment in enumerate(native.segments)))

        return asyncio.run(inspect())


@dataclass(frozen=True)
class ContextSelection:
    imported: bool = False
    diff: bool = False
    turn: bool = False
