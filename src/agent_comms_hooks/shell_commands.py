from __future__ import annotations

import getopt
import io
import re
import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from itertools import islice
from pathlib import Path
from typing import ClassVar, Never, Self

from agent_comms_hooks import HookRefused


class ShellText(io.StringIO):
    """shlex skips comments with readline; the shell still needs their newline."""

    def readline(self, size: int | None = -1) -> str:
        line = super().readline(size)
        if line.endswith('\n'):
            self.seek(self.tell() - 1)
        return line

    def document_lines(self) -> Iterator[str]:
        while line := super().readline():
            yield line

    def read_at(self, position: int, size: int) -> str:
        """Read only the requested raw span, without invoking the lexer or moving it."""
        cursor = self.tell()
        try:
            self.seek(position)
            return super().read(size)
        finally:
            self.seek(cursor)

    def single_line(self) -> bool:
        position = 0
        while chunk := self.read_at(position, io.DEFAULT_BUFFER_SIZE):
            if '\n' in chunk:
                return False
            position += len(chunk)
        return True


@dataclass(frozen=True)
class Redirection:
    target: str

    def written_paths(self) -> tuple[str, ...]:
        return ()

    def defer(self, lexer: ShellLexer) -> None:
        pass

    def read_body(self, lexer: ShellLexer) -> None:
        pass


class OutputRedirection(Redirection):
    def written_paths(self) -> tuple[str, ...]:
        return (self.target,)


class HereDocument(Redirection):
    def defer(self, lexer: ShellLexer) -> None:
        lexer.expect_document(self)

    def line_value(self, line: str) -> str:
        return line.rstrip('\r\n')

    def check_line(self, line: str) -> None:
        pass

    def body_lines(self, lexer: ShellLexer) -> Iterator[str]:
        return lexer.document_lines()

    def read_body(self, lexer: ShellLexer) -> None:
        lexer.resume_at_boundary()
        for line in self.body_lines(lexer):
            if self.line_value(line) == self.target:
                return
            self.check_line(line)
        raise NotStandalone(f'Unterminated here-document {self.target!r}')


class ExpandedHereDocument(HereDocument):
    def body_lines(self, lexer: ShellLexer) -> Iterator[str]:
        """Join unquoted continuations before both termination and expansion checks."""
        parts: list[str] = []
        for line in super().body_lines(lexer):
            content = line.removesuffix('\n')
            backslashes = len(content) - len(content.rstrip('\\'))
            if line.endswith('\n') and backslashes % 2:
                parts.append(line[:-2])
                continue
            parts.append(line)
            yield ''.join(parts)
            parts.clear()
        if parts:
            yield ''.join(parts)

    def check_line(self, line: str) -> None:
        if '$(' in line or '`' in line:
            raise NotStandalone('Command substitution in an expanded here-document is not admitted')


class TabStrippedHereDocument(HereDocument):
    def line_value(self, line: str) -> str:
        return super().line_value(line).lstrip('\t')


class TabStrippedExpandedHereDocument(TabStrippedHereDocument, ExpandedHereDocument):
    pass


@dataclass(frozen=True)
class ShellWord:
    """Logical spelling retains quotes/escapes; start/end retain physical offsets."""

    value: str
    start: int
    end: int
    spelling: str
    members: ClassVar[dict[str, type[ShellWord]]] = {}

    def __init_subclass__(cls, *, spellings: tuple[str, ...] = ()) -> None:
        super().__init_subclass__()
        for spelling in spellings:
            cls.members[spelling] = cls

    @classmethod
    def decoded(cls, value: str, lexer: ShellLexer, end: int) -> ShellWord:
        source = lexer.read_at(lexer.word_start, end - lexer.word_start)
        spelling = ''.join(source[position - lexer.word_start]
                           for position in lexer.word_positions(end))
        kind = cls.members.get(spelling, cls)
        return kind(value, lexer.word_start, end, spelling)

    def consume(self, parser: ShellParser) -> Iterator[ShellCommand]:
        parser.word(self)
        return iter(())

    def argument(self, parser: ShellParser) -> None:
        parser.words.append(self)

    descriptor_syntax: ClassVar[re.Pattern[str]] = re.compile(r'[0-9]+\Z')

    def descriptor_before(self, position: int) -> bool:
        return (self.end == position and self.descriptor_syntax.fullmatch(self.value) is not None
                and self.spelling == self.value)

    def descriptor_value(self) -> bool:
        return self.value == '-' or self.descriptor_syntax.fullmatch(self.value.removesuffix('-')) is not None

    assignment_syntax: ClassVar[re.Pattern[str]] = re.compile(r'[a-zA-Z_][a-zA-Z_0-9]*=')

    def assignment(self) -> bool:
        return self.assignment_syntax.match(self.spelling) is not None

    def command_type(self) -> type[ShellCommand]:
        return SourceCommand

    def document(self) -> HereDocument:
        return ExpandedHereDocument(self.value)

    def tabbed_document(self) -> HereDocument:
        return TabStrippedExpandedHereDocument(self.value)


class QuotedWord(ShellWord):
    def command_type(self) -> type[ShellCommand]:
        return ShellCommand

    def document(self) -> HereDocument:
        return HereDocument(self.value)

    def tabbed_document(self) -> HereDocument:
        return TabStrippedHereDocument(self.value)


class ShellLexer(shlex.shlex, ShellText):
    """Retain shlex's own punctuation decision, before POSIX quote removal erases it.

    No second quoting scanner: native state transitions distinguish punctuation from
    quoted/escaped words. The pushback queue determines the actual source boundary.
    """

    def __init__(self, command: str):
        ShellText.__init__(self, command)
        self._state: str | None = None
        self.word_start = 0
        self.word_type: type[ShellWord] = ShellWord
        self.continuations: list[int] = []
        self.documents: list[HereDocument] = []
        self.target = ArgumentTarget()
        super().__init__(self, posix=True, punctuation_chars=';&|><()\n')
        self.whitespace = ' \t\r'
        self.whitespace_split = True

    def continuation_end(self, position: int) -> int:
        """Skip active continuations; the caller's native state establishes activity."""
        while self.read_at(position, 2) == '\\\n':
            position += 2
        return position

    def read(self, size: int | None = -1) -> str:
        # Let native shlex see the joined input before it classifies words or
        # punctuation. Escaped backslashes and single-quoted text remain literal.
        if self.state not in ("'", '\\'):
            start = self.tell()
            end = self.continuation_end(start)
            if end != start:
                self.continuations.extend(range(start, end, 2))
                self.seek(end)
        character = super().read(size)
        # Punctuation lookahead belongs to the next word, whose target is not
        # known yet. Its first character is checked on native word entry below.
        if self.state in ('a', '"'):
            self.check_substitution(character, self.tell())
        return character

    def check_substitution(self, character: str, following: int) -> None:
        """Inspect active source characters, before native quote/escape removal."""
        if character == '`':
            self.target.require_expansion()
        elif character == '$' and self.read_at(self.continuation_end(following), 1) == '(':
            self.target.require_expansion()

    @property
    def state(self) -> str | None:
        return self._state

    @state.setter
    def state(self, value: str | None) -> None:
        if self._state == '\\':
            self.word_type = QuotedWord
            # shlex retains the escape before $/` inside double quotes; an inner
            # -c must receive the actual shell text, not that retained backslash.
            if value == '"' and self.token[-1:] in ('$', '`'):
                self.token = self.token[:-2] + self.token[-1:]
        if self._state == ' ' and value not in (' ', None):
            self.word_start = self.position - 1
            self.check_substitution(self.read_at(self.word_start, 1),
                                    self.word_start + 1)
        if value == 'c':
            self.word_type = Operator
        elif value in ("'", '"'):
            self.word_type = QuotedWord
        self._state = value

    @property
    def position(self) -> int:
        return self.tell() - len(self._pushback_chars)

    def word_positions(self, end: int) -> Iterator[int]:
        """Physical positions of the logical spelling native shlex just consumed."""
        start = self.word_start
        for continuation in self.continuations:
            if continuation < start:
                continue
            if continuation >= end:
                break
            yield from range(start, continuation)
            start = continuation + 2
        yield from range(start, end)

    def take_adjacent(self, character: str) -> bool:
        position = self.position
        self.resume_at_boundary(position)
        if self.read(1) == character:
            return True
        self.resume_at_boundary(position)
        return False

    def resume_at_boundary(self, position: int | None = None) -> None:
        self.seek(self.position if position is None else position)
        self._pushback_chars.clear()
        self.state = ' '

    def expect_document(self, document: HereDocument) -> None:
        self.documents.append(document)

    def read_documents(self) -> None:
        documents, self.documents = self.documents, []
        for document in documents:
            document.read_body(self)

    def next_word(self, target: ArgumentTarget) -> ShellWord | None:
        self.target = target
        self.word_type = ShellWord
        self.continuations.clear()
        try:
            value = super().read_token()
        except ValueError as unreadable:
            # shlex is foreign: this is where its plain error enters the decoder's family.
            raise NotStandalone(str(unreadable)) from unreadable
        if value is None:
            return None
        end = self.position
        if self.state == ' ' and self.read_at(end - 1, 1) in self.whitespace:
            end -= 1
        return self.word_type.decoded(value, self, end)


class Operator(ShellWord):
    """The grammar's declarations own operator membership and behavior."""

    @classmethod
    def decoded(cls, value: str, lexer: ShellLexer, end: int) -> ShellWord:
        spelling = next((key for key in sorted(cls.members, key=len, reverse=True)
                         if value.startswith(key)), '')
        if not spelling:
            raise NotStandalone(f'Unsupported shell operator {value!r}')
        end = next(islice(lexer.word_positions(end), len(spelling) - 1, None)) + 1
        selected = cls.members[spelling](spelling, lexer.word_start, end, spelling)
        # Admit one operator before recursive parsing; native lookahead stays here.
        lexer.resume_at_boundary(end)
        return selected

    def consume(self, parser: ShellParser) -> Iterator[ShellCommand]:
        return parser.operator(self)

    def apply(self, parser: ShellParser) -> Iterator[ShellCommand]:
        parser.finish()
        return iter(())


class Separator(Operator, spellings=(';', ';;', ';&', ';;&', '&', '&&', '|', '||', '|&')):
    pass


class LoopBodyWord(ShellWord, spellings=('do',)):
    """At command position, ``do`` introduces the loop body, not an executable.

    Keeping it as the program name concealed the first body's storage effects. As an
    argument (or when quoted) it is still ordinary text.
    """

    def argument(self, parser: ShellParser) -> None:
        if parser.words:
            super().argument(parser)


class BraceWord(ShellWord, spellings=('{', '}')):
    """Reserved at command position; an argument or redirection still owns its word."""

    def command_type(self) -> Never:
        raise NotStandalone('Brace groups are not admitted by the bounded shell grammar')

    def argument(self, parser: ShellParser) -> None:
        if not parser.words:
            self.command_type()
        super().argument(parser)


class OpenGroup(Operator, spellings=('(',)):
    def apply(self, parser: ShellParser) -> Iterator[ShellCommand]:
        parser.open_group()
        return iter(())


class CloseGroup(Operator, spellings=(')',)):
    def apply(self, parser: ShellParser) -> Iterator[ShellCommand]:
        parser.close_group()


class Newline(Operator, spellings=('\n',)):
    def apply(self, parser: ShellParser) -> Iterator[ShellCommand]:
        parser.finish()
        parser.lexer.read_documents()
        return parser.flush()


class Redirect(Operator):
    redirection: ClassVar[type[Redirection]] = Redirection

    def bind(self, word: ShellWord) -> Redirection:
        return self.redirection(word.value)

    def consume_descriptor(self, parser: ShellParser) -> None:
        pass

    def operand(self) -> RedirectionTarget:
        return RedirectionTarget(self)

    def apply(self, parser: ShellParser) -> Iterator[ShellCommand]:
        parser.target.require_word()
        self.consume_descriptor(parser)
        parser.target = self.operand()
        return iter(())


class NumberedRedirect(Redirect, spellings=('<', '<<<')):
    """This grammar admits an optional adjacent, unquoted ASCII IO number."""

    def consume_descriptor(self, parser: ShellParser) -> None:
        if parser.words and parser.words[-1].descriptor_before(self.start):
            parser.words.pop()


class WriteRedirect(Redirect):
    redirection = OutputRedirection


class NumberedWriteRedirect(NumberedRedirect, WriteRedirect, spellings=('>', '>>', '>|', '<>')):
    pass


class CombinedWriteRedirect(WriteRedirect, spellings=('&>', '&>>')):
    pass


class ReadDescriptorRedirect(NumberedRedirect, spellings=('<&',)):
    def bind(self, word: ShellWord) -> Redirection:
        if not word.descriptor_value():
            raise NotStandalone('Input descriptor redirection requires a literal descriptor')
        return super().bind(word)


class WriteDescriptorRedirect(NumberedRedirect, spellings=('>&',)):
    def bind(self, word: ShellWord) -> Redirection:
        if word.descriptor_value():
            return Redirection(word.value)
        return OutputRedirection(word.value)


class DocumentRedirect(NumberedRedirect, spellings=('<<',)):
    def bind(self, word: ShellWord) -> Redirection:
        return word.document()

    def operand(self) -> HereDocumentTarget:
        return HereDocumentTarget(self)

    def apply(self, parser: ShellParser) -> Iterator[ShellCommand]:
        super().apply(parser)
        if parser.lexer.take_adjacent('-'):
            parser.target = TabbedDocumentRedirect(
                '<<-', self.start, parser.lexer.position, '<<-').operand()
        return iter(())


class TabbedDocumentRedirect(DocumentRedirect):
    def bind(self, word: ShellWord) -> Redirection:
        return word.tabbed_document()


class ArgumentTarget:
    def require_expansion(self) -> None:
        raise NotStandalone('Command substitution is not admitted by the bounded shell grammar')

    def take(self, parser: ShellParser, word: ShellWord) -> None:
        word.argument(parser)

    def require_word(self) -> None:
        pass


@dataclass(frozen=True)
class RedirectionTarget(ArgumentTarget):
    operator: Redirect

    def take(self, parser: ShellParser, word: ShellWord) -> None:
        redirection = self.operator.bind(word)
        parser.redirections.append(redirection)
        redirection.defer(parser.lexer)
        parser.target = ArgumentTarget()

    def require_word(self) -> None:
        raise NotStandalone('Shell redirection requires a word operand')


class HereDocumentTarget(RedirectionTarget):
    """The shell removes delimiter quotes but never expands its word."""

    def require_expansion(self) -> None:
        pass


class ShellParser:
    def __init__(self, lexer: ShellLexer):
        self.lexer = lexer
        self.pending = ShellCommand(())
        self.words: list[ShellWord] = []
        self.redirections: list[Redirection] = []
        self.target = ArgumentTarget()
        self.line: list[ShellCommand] = []

    def word(self, word: ShellWord) -> None:
        self.target.take(self, word)

    def operator(self, operator: Operator) -> Iterator[ShellCommand]:
        return operator.apply(self)

    def open_group(self) -> None:
        self.target.require_word()
        if self.words or self.redirections:
            raise NotStandalone('A parenthesized command requires its own command position')
        self.pending.require_group_boundary()
        self.pending = CommandGroup((), body=tuple(GroupParser(self.lexer).commands()))

    def close_group(self) -> Never:
        raise NotStandalone('A closing parenthesis outside a group, such as a case pattern, '
                            'is not admitted by the bounded shell grammar')

    def finish(self) -> None:
        self.target.require_word()
        while self.words and self.words[0].assignment():
            self.words.pop(0)
        self.line.extend(self.pending.completed(tuple(self.words), tuple(self.redirections)))
        self.pending = ShellCommand(())
        self.words.clear()
        self.redirections.clear()

    def flush(self) -> Iterator[ShellCommand]:
        completed = tuple(self.line)
        self.line.clear()
        return iter(completed)

    def commands(self) -> Iterator[ShellCommand]:
        while word := self.lexer.next_word(self.target):
            yield from word.consume(self)
        self.finish()
        self.lexer.read_documents()
        yield from self.flush()


class GroupClosed(Exception):
    """The group's own closing delimiter ends its shared-source parse."""


class GroupParser(ShellParser):
    def close_group(self) -> Never:
        raise GroupClosed()

    def commands(self) -> Iterator[ShellCommand]:
        try:
            yield from super().commands()
        except GroupClosed:
            self.finish()
            yield from self.flush()
            return
        raise NotStandalone('Unterminated parenthesized command')


class NotStandalone(HookRefused):
    """This shell syntax is outside the decoder's admitted grammar; every Bash refusal
    answers it as its reason, and it is still the decoder's ValueError."""


class LiteralShellParser(ShellParser):
    def __init__(self, lexer: ShellLexer):
        if not lexer.single_line():
            raise NotStandalone('Recovery requires literal, single-line words')
        super().__init__(lexer)

    def word(self, word: ShellWord) -> None:
        if set(word.spelling) & {'$', '`'}:
            raise NotStandalone('Recovery requires literal, single-line words')
        if not self.words and word.assignment():
            raise NotStandalone('Recovery does not admit environment assignments: '
                                'LD_PRELOAD= or PYTHONPATH= would run other code as the command')
        super().word(word)

    def operator(self, operator: Operator) -> Iterator[ShellCommand]:
        raise NotStandalone('Recovery does not admit shell operators')


@dataclass(frozen=True)
class Program:
    """An external program, named once by the executable name it is invoked by."""

    name: str
    aliases: ClassVar[tuple[str, ...]] = ()

    def runs(self, command: ShellCommand) -> bool:
        return command.program in (self.name, *self.aliases)


class BusListener:
    """A long-running job that is how a session hears the bus, not work that ends: a stop hook
    that waited for one could never let the session stop."""

    listening: ClassVar[str]

    @classmethod
    def running_in(cls, command_line: str) -> bool:
        return any(member.listening in command_line for member in cls.__subclasses__())


class ConnectCommand(BusListener):
    program: ClassVar[Program] = Program('agent-comms-claude-bridge')
    listening = f'{program.name} watch'

    @classmethod
    def command_name(cls) -> str:
        return ConnectCommand.__name__.removesuffix('Command').lower()


class InboxWatchCommand(BusListener):
    """A cloud session's listener, which holds the socket its bridge-less session cannot."""

    program: ClassVar[Program] = Program('agent-comms-inbox-watch')
    listening = program.name


class EmergencyHookStop:
    """The one command a gate that refuses every tool must still let run. A session a gate has
    locked out has no other way to stop it, so the way out is declared here and named in the
    refusal. It runs only as the whole command line, so it can carry nothing else past a gate."""

    program: ClassVar[Program] = Program('agent-comms-hooks')
    verb: ClassVar[str] = 'stop'
    way_out: ClassVar[str] = (
        f"If this gate is what's wrong, `{program.name} {verb} --from YOUR_NAME --why WHY` turns "
        "every hook off and posts that to #comms under your name.")

    @classmethod
    def is_whole_of(cls, command_line: str) -> bool:
        """By its bare name: a path could name any file, and PATH finds the real one."""
        commands = tuple(ShellCommand.standalone(command_line))
        return len(commands) == 1 and commands[0].words[:2] == (cls.program.name, cls.verb)


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
            yield from LiteralShellParser(ShellLexer(command)).commands()
        except NotStandalone:
            return

    @classmethod
    def of(cls, words: Iterable[str]) -> Iterator[Self]:
        words = tuple(words)
        if words:
            yield cls(words)

    @classmethod
    def parsed(cls, command: str) -> Iterator[ShellCommand]:
        yield from ShellParser(ShellLexer(command)).commands()

    @classmethod
    def split(cls, command: str) -> Iterator[ShellCommand]:
        for simple in cls.parsed(command):
            yield from simple.run_commands()

    def source_commands(self) -> Iterator[ShellCommand]:
        """Arguments launched as an executable are not shell control syntax."""
        return iter(())

    @property
    def source_words(self) -> tuple[str, ...]:
        return tuple(word for command in self.source_commands() for word in command.words)

    def completed(self, words: tuple[ShellWord, ...], redirections: tuple[Redirection, ...]) -> Iterator[ShellCommand]:
        if words or redirections:
            command_type = words[0].command_type() if words else ShellCommand
            yield command_type(tuple(word.value for word in words), redirections=redirections)

    def require_group_boundary(self) -> None:
        pass

    def nested(self) -> Iterator[ShellCommand]:
        runner = Runner.named(self.program)
        yield from runner.inline(self.args)
        yield from runner.started(self.args)

    def run_commands(self) -> Iterator[ShellCommand]:
        yield self
        for command in self.nested():
            yield from command.run_commands()


class SourceCommand(ShellCommand):
    def source_commands(self) -> Iterator[ShellCommand]:
        return iter((self,))


@dataclass(frozen=True)
class CommandGroup(ShellCommand):
    body: tuple[ShellCommand, ...] = field(kw_only=True)

    def completed(self, words: tuple[ShellWord, ...], redirections: tuple[Redirection, ...]) -> Iterator[Self]:
        if words or not self.body:
            raise NotStandalone('A parenthesized command requires a nonempty body and no trailing words')
        yield replace(self, redirections=redirections)

    def require_group_boundary(self) -> None:
        raise NotStandalone('Adjacent groups require a command separator')

    def nested(self) -> Iterator[ShellCommand]:
        return iter(self.body)


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
            raise NotStandalone(f'Incomplete or unknown runner option: {error}') from error

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
            raise NotStandalone('Shell +options are not admitted; use explicit -options')
        for script in scripts:
            yield from ShellCommand.parsed(script)
