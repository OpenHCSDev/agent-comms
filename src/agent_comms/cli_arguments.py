"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

import argparse


class CliArguments(argparse.Namespace):
    command: str
    binding_thread: BindingThread

class CliArgumentParser(argparse.ArgumentParser):
    """Argument errors use Core's JSON failure boundary, including subcommands."""

    def error(self, message: str) -> None:
        raise ValueError(message)
