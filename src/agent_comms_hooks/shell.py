"""The shell's words, decoded once for Core's storage rules and for the hooks.

Standard library only, because every hook imports it on every tool call.
"""

from __future__ import annotations

import shlex
from itertools import groupby


def shell_commands(command):
    """Shell words, not matches in quoted code or here-document bodies."""
    body = []
    lines = iter(command.splitlines(keepends=True))
    for line in lines:
        body.append(line)
        lex = shlex.shlex(line, posix=False, punctuation_chars='<>')
        lex.whitespace_split = True
        words = list(lex)
        for index, word in enumerate(words[:-1]):
            if word in ('<<', '<<-'):
                delimiter = shlex.split(words[index + 1])[0]
                for line in lines:
                    if line.rstrip('\r\n').lstrip('\t') == delimiter:
                        break
    lex = shlex.shlex(''.join(body), posix=True, punctuation_chars=';&|><()\n')
    lex.whitespace = ' \t\r'
    lex.whitespace_split = True
    separators = set(';&|()\n')
    for separator, words in groupby(lex, key=lambda word: bool(word) and set(word) <= separators):
        if not separator:
            yield list(words)
