"""The context verb and its option-selected implementation family."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import CoreContextInspection
from agent_comms.cli_commands import CoreSegmentText
from agent_comms.cli_commands import NativeContextInspection
from agent_comms.cli_commands import NativeSourceSpans
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.context_store import ContextManifestCursor
from agent_comms.context_store import LATEST_CONTEXT
from agent_comms.coordination_errors import Refused
from agent_comms.field_codec import Argument
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_payloads import NativeContextData
from agent_comms.owner_connection import OwnerConnection
from agent_comms.owner_connection import socket_path
from agent_comms.turn_context import ContextManifest
from agent_comms.turn_context import NextContextTurn
from agent_comms.turn_context import TurnContext
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated
import argparse
import asyncio



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
                    return CoreContextInspection("current-core-contributors; before future input",
                                                 False, context, segments, str(error))
            finally:
                await connection.close()
            native_context = native.for_turn(owner.incarnation, NextContextTurn())
            return NativeContextInspection(
                "next-native-base-and-core-contributors; before future input and provider hooks",
                False, context, segments, native_context.segments, native_context.render().provider,
                tuple(NativeSourceSpans(position, segment.measured_manifest().sha256,
                                        segment.source_ranges())
                      for position, segment in enumerate(native.segments)))

        return asyncio.run(inspect())


@dataclass(frozen=True)
class ContextSelection:
    imported: bool = False
    diff: bool = False
    turn: bool = False
