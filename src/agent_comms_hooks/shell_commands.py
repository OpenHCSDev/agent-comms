from __future__ import annotations

import getopt
import io
import re
import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Self


class ShellText(io.StringIO):
    """shlex skips comments with readline; the shell still needs their newline."""

    def readline(self, size: int = -1) -> str:
        line = super().readline(size)
        if line.endswith('\n'):
            self.seek(self.tell() - 1)
        return line

    def document_lines(self) -> Iterator[str]:
        while line := super().readline():
            yield line


@dataclass(frozen=True)
class Redirection:
    target: str

    def written_paths(self) -> tuple[str, ...]:
        return ()

    def read_body(self, lexer: ShellLexer) -> None:
        pass


class OutputRedirection(Redirection):
    def written_paths(self) -> tuple[str, ...]:
        return (self.target,)


class HereDocument(Redirection):
    def line_value(self, line: str) -> str:
        return line.rstrip('\r\n')

    def check_line(self, line: str) -> None:
        pass

    def read_body(self, lexer: ShellLexer) -> None:
        lexer.resume_at_boundary()
        for line in lexer.text.document_lines():
            if self.line_value(line) == self.target:
                return
            self.check_line(line)
        raise ValueError(f'Unterminated here-document {self.target!r}')


class ExpandedHereDocument(HereDocument):
    def check_line(self, line: str) -> None:
        if '$(' in line or '`' in line:
            raise ValueError('Command substitution in an expanded here-document is not admitted')


class TabStrippedHereDocument(HereDocument):
    def line_value(self, line: str) -> str:
        return super().line_value(line).lstrip('\t')


class TabStrippedExpandedHereDocument(TabStrippedHereDocument, ExpandedHereDocument):
    pass


@dataclass(frozen=True)
class ShellWord:
    value: str
    start: int
    end: int
    spelling: str

    @classmethod
    def decoded(cls, value: str, lexer: ShellLexer, end: int) -> ShellWord:
        return cls(value, lexer.word_start, end, lexer.command[lexer.word_start:end])

    def consume(self, parser: ShellParser) -> Iterator[ShellCommand]:
        parser.word(self)
        return iter(())

    def descriptor_before(self, position: int) -> bool:
        return self.end == position and self.value.isdecimal() and self.spelling == self.value

    assignment_syntax: ClassVar[re.Pattern[str]] = re.compile(r'[a-zA-Z_][a-zA-Z_0-9]*=')

    def assignment(self) -> bool:
        return self.assignment_syntax.match(self.spelling) is not None

    def document(self) -> HereDocument:
        return ExpandedHereDocument(self.value)

    def tabbed_document(self) -> HereDocument:
        return TabStrippedExpandedHereDocument(self.value)


class QuotedWord(ShellWord):
    def document(self) -> HereDocument:
        return HereDocument(self.value)

    def tabbed_document(self) -> HereDocument:
        return TabStrippedHereDocument(self.value)


class ShellOperators(ShellWord):
    @classmethod
    def decoded(cls, value: str, lexer: ShellLexer, end: int) -> ShellWord:
        boundary = value.find('\n')
        if boundary >= 0:
            value = value[:boundary + 1]
            end = lexer.word_start + len(value)
        return super().decoded(value, lexer, end)

    def consume(self, parser: ShellParser) -> Iterator[ShellCommand]:
        position = 0
        while position < len(self.value):
            spelling = next((value for value in sorted(Operator.members, key=len, reverse=True)
                             if self.value.startswith(value, position)), '')
            if not spelling:
                raise ValueError(f'Unsupported shell operator {self.value[position:]!r}')
            operator = Operator.members[spelling]()
            position += len(spelling)
            yield from parser.operator(operator, self.start + position - len(spelling))


class ShellLexer(shlex.shlex):
    """Retain shlex's own punctuation decision, before POSIX quote removal erases it.

    No second quoting scanner: native state transitions distinguish punctuation from
    quoted/escaped words. The pushback queue determines the actual source boundary.
    """

    def __init__(self, command: str):
        self.text = ShellText(command)
        self.command = command
        self._state: str | None = None
        self.word_start = 0
        self.word_type: type[ShellWord] = ShellWord
        super().__init__(self.text, posix=True, punctuation_chars=';&|><()\n')
        self.whitespace = ' \t\r'
        self.whitespace_split = True

    @property
    def state(self) -> str | None:
        return self._state

    @state.setter
    def state(self, value: str | None) -> None:
        if self._state == ' ' and value not in (' ', None):
            self.word_start = self.position - 1
        if value == 'c':
            self.word_type = ShellOperators
        elif value in ("'", '"', '\\'):
            self.word_type = QuotedWord
        self._state = value

    @property
    def position(self) -> int:
        return self.text.tell() - len(self._pushback_chars)

    def take_adjacent(self, character: str) -> bool:
        if self.command[self.position:self.position + 1] != character:
            return False
        if self._pushback_chars:
            self._pushback_chars.pop()
        else:
            self.text.read(1)
        return True

    def resume_at_boundary(self, position: int | None = None) -> None:
        self.text.seek(self.position if position is None else position)
        self._pushback_chars.clear()
        self.state = ' '

    def next_word(self) -> ShellWord | None:
        self.word_type = ShellWord
        value = super().read_token()
        if value is None:
            return None
        end = self.position
        if self.state == ' ' and self.command[end - 1:end] in self.whitespace:
            end -= 1
        return self.word_type.decoded(value, self, end)


class Operator:
    """The grammar's declarations own operator membership and behavior."""

    members: ClassVar[dict[str, type[Operator]]] = {}

    def __init_subclass__(cls, *, spellings: tuple[str, ...] = ()) -> None:
        super().__init_subclass__()
        for spelling in spellings:
            cls.members[spelling] = cls

    def consume(self, parser: ShellParser, position: int) -> Iterator[ShellCommand]:
        parser.finish()
        return iter(())


class Separator(Operator, spellings=(';', ';;', ';&', ';;&', '&', '&&', '|', '||', '|&', '(', ')')):
    pass


class Newline(Operator, spellings=('\n',)):
    def consume(self, parser: ShellParser, position: int) -> Iterator[ShellCommand]:
        parser.finish()
        # The header newline owns the boundary, not native punctuation lookahead.
        parser.lexer.resume_at_boundary(position + 1)
        parser.read_documents()
        return parser.flush()


class Redirect(Operator, spellings=('<', '<<<')):
    redirection: ClassVar[type[Redirection]] = Redirection

    def bind(self, word: ShellWord) -> Redirection:
        return self.redirection(word.value)

    def consume(self, parser: ShellParser, position: int) -> Iterator[ShellCommand]:
        parser.target.require_word()
        if parser.words and parser.words[-1].descriptor_before(position):
            parser.words.pop()
        parser.target = RedirectionTarget(self)
        return iter(())


class WriteRedirect(Redirect, spellings=('>', '>>', '>|', '&>', '&>>', '<>')):
    redirection = OutputRedirection


class ReadDescriptorRedirect(Redirect, spellings=('<&',)):
    def bind(self, word: ShellWord) -> Redirection:
        if not (word.value == '-' or word.value.rstrip('-').isdecimal()):
            raise ValueError('Input descriptor redirection requires a literal descriptor')
        return super().bind(word)


class WriteDescriptorRedirect(Redirect, spellings=('>&',)):
    def bind(self, word: ShellWord) -> Redirection:
        if word.value == '-' or word.value.rstrip('-').isdecimal():
            return Redirection(word.value)
        return OutputRedirection(word.value)


class DocumentRedirect(Redirect, spellings=('<<',)):
    def bind(self, word: ShellWord) -> Redirection:
        return word.document()

    def consume(self, parser: ShellParser, position: int) -> Iterator[ShellCommand]:
        super().consume(parser, position)
        if parser.lexer.take_adjacent('-'):
            parser.target = RedirectionTarget(TabbedDocumentRedirect())
        return iter(())


class TabbedDocumentRedirect(DocumentRedirect):
    def bind(self, word: ShellWord) -> Redirection:
        return word.tabbed_document()


class ArgumentTarget:
    def take(self, parser: ShellParser, word: ShellWord) -> None:
        parser.words.append(word)

    def require_word(self) -> None:
        pass


@dataclass(frozen=True)
class RedirectionTarget(ArgumentTarget):
    operator: Redirect

    def take(self, parser: ShellParser, word: ShellWord) -> None:
        parser.redirections.append(self.operator.bind(word))
        parser.target = ArgumentTarget()

    def require_word(self) -> None:
        raise ValueError('Shell redirection requires a word operand')


class ShellParser:
    def __init__(self, command: str):
        self.lexer = ShellLexer(command)
        self.words: list[ShellWord] = []
        self.redirections: list[Redirection] = []
        self.target = ArgumentTarget()
        self.line: list[ShellCommand] = []

    def word(self, word: ShellWord) -> None:
        self.target.take(self, word)

    def operator(self, operator: Operator, position: int) -> Iterator[ShellCommand]:
        return operator.consume(self, position)

    def finish(self) -> None:
        self.target.require_word()
        while self.words and self.words[0].assignment():
            self.words.pop(0)
        if self.words or self.redirections:
            self.line.append(ShellCommand(tuple(word.value for word in self.words),
                                          redirections=tuple(self.redirections)))
        self.words.clear()
        self.redirections.clear()

    def flush(self) -> Iterator[ShellCommand]:
        completed = tuple(self.line)
        self.line.clear()
        return iter(completed)

    def read_documents(self) -> None:
        for command in self.line:
            for redirection in command.redirections:
                redirection.read_body(self.lexer)

    def commands(self) -> Iterator[ShellCommand]:
        while word := self.lexer.next_word():
            yield from word.consume(self)
        self.finish()
        self.read_documents()
        yield from self.flush()


class NotStandalone(ValueError):
    """Literal recovery does not admit this shell syntax."""


class LiteralShellParser(ShellParser):
    def word(self, word: ShellWord) -> None:
        if set(word.spelling) & {'$', '`', '\n'}:
            raise NotStandalone('Recovery requires literal, single-line words')
        super().word(word)

    def operator(self, operator: Operator, position: int) -> Iterator[ShellCommand]:
        raise NotStandalone('Recovery does not admit shell operators')


@dataclass(frozen=True)
class Program:
    """An external program, named once by the executable name it is invoked by."""

    name: str
    aliases: ClassVar[tuple[str, ...]] = ()

    def runs(self, command: ShellCommand) -> bool:
        return command.program in (self.name, *self.aliases)


class ConnectCommand:
    program: ClassVar[Program] = Program('agent-comms-claude-bridge')

    @classmethod
    def command_name(cls) -> str:
        return ConnectCommand.__name__.removesuffix('Command').lower()

    @classmethod
    def permits(cls, command: str) -> bool:
        commands = tuple(ShellCommand.split(command))
        return (len(commands) == 1 and commands[0].invokes(cls.program, cls.command_name())
                and not commands[0].args[1:] and not commands[0].redirections)


@dataclass(frozen=True)
class ShellCommand:
    """Executable words and syntax operands remain distinct after boundary decoding."""

    words: tuple[str, ...]
    redirections: tuple[Redirection, ...] = field(default=(), kw_only=True)

    @property
    def program(self) -> str:
        return Path(self.words[0]).name if self.words else ''

    @property
    def args(self) -> list[str]:
        return list(self.words[1:])

    def invokes(self, program: Program, verb: str) -> bool:
        return program.runs(self) and self.args[:1] == [verb]

    def value_options(self, allowed: frozenset[str]) -> bool:
        options = self.args[1:]
        return len(options) % 2 == 0 and set(options[::2]) <= allowed

    @classmethod
    def standalone(cls, command: str) -> Iterator[ShellCommand]:
        try:
            yield from LiteralShellParser(command).commands()
        except NotStandalone:
            return

    @classmethod
    def of(cls, words: Iterable[str]) -> Iterator[Self]:
        words = tuple(words)
        if words:
            yield cls(words)

    @classmethod
    def parsed(cls, command: str) -> Iterator[ShellCommand]:
        yield from ShellParser(command).commands()

    @classmethod
    def split(cls, command: str) -> Iterator[ShellCommand]:
        for simple in cls.parsed(command):
            yield from simple.run_commands()

    def run_commands(self) -> Iterator[ShellCommand]:
        yield self
        runner = Runner.named(self.program)
        yield from runner.inline(self.args)
        for command in runner.started(self.args):
            yield from command.run_commands()


class Runner(Program):
    members: ClassVar[dict[str, type[Runner]]] = {}
    names: ClassVar[tuple[str, ...]] = ()
    short_options: ClassVar[str] = ''
    long_options: ClassVar[tuple[str, ...]] = ('help', 'version')
    operands: ClassVar[int] = 0

    def __init_subclass__(cls, *, names: tuple[str, ...] = ()) -> None:
        super().__init_subclass__()
        cls.names = names
        for name in names:
            cls.members[name] = cls

    @classmethod
    def named(cls, name: str) -> Runner:
        return cls.members.get(name, Runner)(name)

    @classmethod
    def options(cls, args: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
        try:
            return getopt.getopt(args, cls.short_options, cls.long_options)
        except getopt.GetoptError as error:
            raise ValueError(f'Incomplete or unknown runner option: {error}') from error

    @classmethod
    def arguments(cls, args: list[str]) -> list[str]:
        return cls.options(args)[1]

    def started(self, args: list[str]) -> Iterator[ShellCommand]:
        return iter(())

    def inline(self, args: list[str]) -> Iterator[ShellCommand]:
        return iter(())


class ArgumentRunner(Runner):
    def started(self, args: list[str]) -> Iterator[ShellCommand]:
        yield from ShellCommand.of(self.arguments(args)[self.operands:])


class TimeoutRunner(ArgumentRunner, names=('timeout',)):
    short_options = 'k:s:v'
    long_options = ('kill-after=', 'signal=', 'foreground', 'preserve-status', 'verbose', 'help', 'version')
    operands = 1


class EnvironmentRunner(ArgumentRunner, names=('env',)):
    short_options = 'i0u:'
    long_options = ('ignore-environment', 'null', 'unset=', 'help', 'version')

    def started(self, args: list[str]) -> Iterator[ShellCommand]:
        arguments = self.arguments(args)
        while arguments and ShellWord.assignment_syntax.match(arguments[0]):
            arguments.pop(0)
        yield from ShellCommand.of(arguments)


class NoHangupRunner(ArgumentRunner, names=('nohup',)):
    pass


class BufferRunner(ArgumentRunner, names=('stdbuf',)):
    short_options = 'i:o:e:'
    long_options = ('input=', 'output=', 'error=', 'help', 'version')


class ShellRunner(Runner, names=('bash', 'sh', 'zsh')):
    short_options = 'c:o:O:abefhiklmnprstuvxBCEHPT'
    long_options = ('noprofile', 'norc', 'posix', 'restricted', 'verbose', 'login', 'help', 'version')

    def inline(self, args: list[str]) -> Iterator[ShellCommand]:
        options, remaining = self.options(args)
        scripts = tuple(value for option, value in options if option == '-c')
        if not scripts and remaining and remaining[0].startswith('+'):
            raise ValueError('Shell +options are not admitted; use explicit -options')
        for script in scripts:
            yield from ShellCommand.split(script)
