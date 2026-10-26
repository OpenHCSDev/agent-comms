"""The context verb and its option-selected implementation family."""

from __future__ import annotations

from abc import abstractmethod
from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import CoreContextInspection
from agent_comms.cli_commands import CoreSegmentText
from agent_comms.cli_commands import ImportedContextSources
from agent_comms.cli_commands import NativeContextInspection
from agent_comms.cli_commands import NativeSourceSpans
from agent_comms.cli_commands import RecordedContextManifests
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.context_store import ContextManifestCursor
from agent_comms.context_store import LATEST_CONTEXT
from agent_comms.coordination_errors import Refused
from agent_comms.field_codec import Argument
from agent_comms.field_codec import FieldCodec
from agent_comms.importing import ImportedSessionMetadata
from agent_comms.native_turn_context import NativeContextData
from agent_comms.owner_connection import OwnerConnection
from agent_comms.owner_connection import socket_path
from agent_comms.turn_context import ContextManifest
from agent_comms.turn_context import ContextManifestChange
from agent_comms.turn_context import NextContextTurn
from agent_comms.turn_context import TurnContext
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import dropwhile
from itertools import takewhile
from typing import Annotated
import argparse
import asyncio



from agent_comms.cli_context import ContextOptionCliCommand, SelectedContextCliCommand
from agent_comms.cli_verbs.diff_context import DiffContextCliCommand

class TurnDiffContextCliCommand(SelectedContextCliCommand, DiffContextCliCommand):
    """Start the shared diff walk at the requested turn."""

