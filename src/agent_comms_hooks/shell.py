"""The shell's words and commands, decoded once for Core's storage rules and for the hooks.

Standard library only, because every hook imports it on every tool call.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Self


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

    @classmethod
    def split(cls, command: str) -> Iterator[Self]:
        for words in shell_commands(command):
            yield from cls.of(words)
