"""The shell's words and commands, decoded once for Core's storage rules and for the hooks.

Standard library only, because every hook imports it on every tool call.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import ClassVar, Self


def shell_commands(command: str) -> Iterator[list[str]]:
    """Shell words, not matches in quoted code or here-document bodies."""
    body = []
    lines = iter(command.splitlines(keepends=True))
    for line in lines:
        body.append(line)
        lex = shlex.shlex(line, posix=False, punctuation_chars="<>")
        lex.whitespace_split = True
        words = list(lex)
        for index, word in enumerate(words[:-1]):
            if word in ("<<", "<<-"):
                delimiter = shlex.split(words[index + 1])[0]
                for line in lines:
                    if line.rstrip("\r\n").lstrip("\t") == delimiter:
                        break
    lex = shlex.shlex("".join(body), posix=True, punctuation_chars=";&|><()\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    separators = set(";&|()\n")
    for separator, group in groupby(lex, key=lambda word: bool(word) and set(word) <= separators):
        if not separator:
            yield list(group)


@dataclass(frozen=True)
class Program:
    """An external program, named once by the executable name it is invoked by."""

    name: str

    def runs(self, command: ShellCommand) -> bool:
        return command.program == self.name


@dataclass(frozen=True)
class ShellCommand:
    """One simple command: its words after any leading `NAME=value` assignments, so its
    program is the first of them, named by its basename however its path is written."""

    words: tuple[str, ...]

    @property
    def program(self) -> str:
        return Path(self.words[0]).name

    @property
    def args(self) -> list[str]:
        return list(self.words[1:])

    @classmethod
    def of(cls, words: Iterable[str]) -> Iterator[Self]:
        """The command these words run; none when they only assign variables."""
        words = list(words)
        while words and "=" in words[0]:
            words.pop(0)
        if words:
            yield cls(tuple(words))

    shells: ClassVar[frozenset[str]] = frozenset({"bash", "sh", "zsh"})
    # Programs that run their arguments as a command, after their flags, env's assignments
    # and this many operands (timeout's duration): each tool's own command-line contract.
    runners: ClassVar[dict[str, int]] = {"timeout": 1, "env": 0, "nohup": 0, "stdbuf": 0}

    @classmethod
    def split(cls, command: str) -> Iterator[Self]:
        """Every simple command the text runs, including those a shell runs from its `-c`
        string, so `bash -c '...'` hides nothing from a reader of the commands."""
        for words in shell_commands(command):
            for simple in cls.of(words):
                yield from simple.run_commands()

    def run_commands(self) -> Iterator[Self]:
        """This command and every command it runs: a `-c` string, or the command a runner
        such as `timeout` or `env` starts."""
        yield self
        yield from self.inline_commands()
        for command in self.started():
            yield from command.run_commands()

    def started(self) -> Iterator[Self]:
        """The command a runner starts; none for any other program."""
        if self.program not in self.runners:
            return
        words = list(self.args)
        for _ in range(len(words)):
            if not words or not (words[0].startswith("-") or "=" in words[0]):
                break
            words.pop(0)
        yield from self.of(words[self.runners[self.program] :])

    def inline_commands(self) -> Iterator[Self]:
        """The commands this shell runs from its `-c` string; none for any other program."""
        args = self.args
        if self.program in self.shells and "-c" in args[:-1]:
            yield from self.split(args[args.index("-c") + 1])
