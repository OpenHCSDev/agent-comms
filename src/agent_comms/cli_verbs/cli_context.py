"""The context verb and its option-selected implementation family."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import (
    CoreContextInspection,
    CoreSegmentText,
    NativeContextInspection,
    NativeSourceSpans,
)
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.context_store import LATEST_CONTEXT, ContextManifestCursor
from agent_comms.coordination.errors import Refused
from agent_comms.execution.turn_context import ContextManifest, NextContextTurn, TurnContext
from agent_comms.nominal.field_codec import Argument, FieldCodec
from agent_comms.pi_payloads import NativeContextData
from agent_comms.transport.owner_connection import OwnerConnection, socket_path


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


    def manifests(self, ctx: Comms) -> Iterator[ContextManifest]:
        cursor: ContextManifestCursor = LATEST_CONTEXT
        while True:
            page = ctx.contexts.page(self.thread, ctx.registry, cursor=cursor)
            yield from (resource.value for resource in reversed(page.resources))
            if not page.has_older():
                return
            cursor = page.older

    def apply(self, ctx: Comms) -> CliResult:
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
                native_source_spans=tuple(NativeSourceSpans(position, segment.measured_manifest().sha256,
                                        segment.source_ranges())
                      for position, segment in enumerate(native.segments)))

        return asyncio.run(inspect())


@dataclass(frozen=True)
class ContextSelection:
    imported: bool = False
    diff: bool = False
    turn: bool = False
