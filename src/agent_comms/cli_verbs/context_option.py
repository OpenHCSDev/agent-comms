"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from abc import abstractmethod
from agent_comms.cli_result import CliResult
from agent_comms.cli_verbs.context import ContextCliCommand
from agent_comms.comms import Comms
import argparse


class ContextOptionCliCommand(ContextCliCommand):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        """These commands are selected by the context parser's options."""

    @abstractmethod
    def apply(self, ctx: Comms) -> CliResult:
        """Inspect the context selected at the CLI boundary."""
