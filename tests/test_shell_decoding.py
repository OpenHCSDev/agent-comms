"""Data-only shell boundary controls: no commands, subprocesses or filesystem writes."""
from __future__ import annotations

import io
import shlex
import unittest
from pathlib import Path

from agent_comms.projects.shell_effects import (
    ForbiddenStorageEffect,
    ShellInvocation,
    WriteStorageEffect,
    shell_storage_effects,
)
from agent_comms.transport.tool_denial import StorageDenied
from agent_comms_hooks.bounded_waits import WaitCommand
from agent_comms_hooks.shell_commands import HereDocument, ShellCommand, ShellText

CWD = Path('/shell-decoder-control')


class ShellDecoding(unittest.TestCase):
    def test_raw_source_reads_preserve_the_cursor(self):
        source = ShellText('before λ\\\nafter')
        source.seek(3)
        self.assertEqual(source.read_at(7, 3), 'λ\\\n')
        self.assertEqual(source.tell(), 3)
        self.assertEqual(source.read_at(100, 2), '')
        self.assertEqual(source.tell(), 3)
        with self.assertRaises(ValueError):
            source.read_at(-1, 1)
        self.assertEqual(source.tell(), 3)

    def test_physical_single_line_scan_restores_the_cursor(self):
        for text, expected in (
            ('', True),
            ('literal', True),
            ('x' * (io.DEFAULT_BUFFER_SIZE * 2 + 1), True),
            ('x' * (io.DEFAULT_BUFFER_SIZE - 1) + '\n', False),
            ('x' * io.DEFAULT_BUFFER_SIZE + '\n', False),
            ('\\\ncontinued', False),
        ):
            with self.subTest(length=len(text), expected=expected):
                source = ShellText(text)
                source.seek(min(3, len(text)))
                self.assertEqual(source.single_line(), expected)
                self.assertEqual(source.tell(), min(3, len(text)))

    def test_words_are_never_promoted_to_operators(self):
        cases = (
            ("echo '>' picked", ('echo', '>', 'picked')),
            ('echo ">>" picked', ('echo', '>>', 'picked')),
            (r'echo \> picked', ('echo', '>', 'picked')),
            ("echo '<<' EOF", ('echo', '<<', 'EOF')),
            ("echo ';' '|' '&' '(' ')'; echo done", ('echo', ';', '|', '&', '(', ')')),
            ('echo "a\\\"b"', ('echo', 'a"b')),
            ("echo prefix'<<'suffix", ('echo', 'prefix<<suffix')),
            ("echo ''", ('echo', '')),
            ("echo '{' '}'", ('echo', '{', '}')),
            (r'echo \{ \}', ('echo', '{', '}')),
            ('echo { }', ('echo', '{', '}')),
            ("'{' literal", ('{', 'literal')),
            ("'{'\\\n literal", ('{', 'literal')),
            ('"\\\n{" literal', ('{', 'literal')),
            ('echo ">\\\n>"', ('echo', '>>')),
            ("echo '>\\\n>'", ('echo', '>\\\n>')),
            ('echo \\>\\\n\\>', ('echo', '>>')),
            ('echo file{brace}', ('echo', 'file{brace}')),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                commands = tuple(ShellCommand.parsed(text))
                self.assertEqual(commands[0].words, expected)
                self.assertEqual(shell_storage_effects(text, CWD), ())

    def test_active_substitutions_refuse_before_effects_at_every_shell_depth(self):
        cases = (
            'printf "%s" "$(touch target)"',
            'printf %s `touch target`',
            'printf "%s" "`touch target`"',
            'printf %s before"$(touch target)"after',
            "printf %s 'literal'$(touch target)",
            'printf done;$(touch target)',
            'printf done;`touch target`',
            'printf %s >"$(touch target)"',
            'printf %s "${unused:-$(touch target)}"',
            'printf %s "$\\\n(touch target)"',
            r'printf %s "\\$(touch target)"',
        )
        for script in cases:
            for text in (script, shlex.join(('bash', '-c', script))):
                with self.subTest(text=text), self.assertRaises(StorageDenied):
                    shell_storage_effects(text, CWD)
        for text in (
            r'bash -c "printf %s \$(touch target)"',
            r'bash -c "printf %s \`touch target\`"',
        ):
            with self.subTest(text=text), self.assertRaises(StorageDenied):
                shell_storage_effects(text, CWD)

    def test_literal_substitution_text_retains_native_quote_and_escape_meaning(self):
        cases = (
            ("printf %s '$(touch target)'", '$(touch target)'),
            ("printf %s '`touch target`'", '`touch target`'),
            (r'printf %s "\$(touch target)"', '$(touch target)'),
            (r'printf %s "\`touch target\`"', '`touch target`'),
            (r'printf %s \`literal\`', '`literal`'),
            ("printf %s '$'\"(touch target)\"", '$(touch target)'),
            (r'printf %s "one\q"', r'one\q'),
            ('printf %s fi\\\nle', 'file'),
            ('printf %s "fi\\\nle"', 'file'),
            ("printf %s '$\\\n(literal)'", '$\\\n(literal)'),
        )
        for text, value in cases:
            with self.subTest(text=text):
                command, = ShellCommand.parsed(text)
                self.assertEqual(command.words, ('printf', '%s', value))
                self.assertEqual(shell_storage_effects(text, CWD), ())

    def test_document_delimiter_and_opaque_body_do_not_execute_substitutions(self):
        for text in (
            "cat <<'EOF'\n$(touch target)\nEOF\nmkdir after",
            "cat <<'EOF'\n`touch target`\nEOF\nmkdir after",
            'cat <<"$(tag)"\nopaque\n$(tag)\nmkdir after',
            'cat <<-"`tag`"\n\topaque\n\t`tag`\nmkdir after',
        ):
            with self.subTest(text=text):
                self.assertEqual(shell_storage_effects(text, CWD), (WriteStorageEffect(CWD / 'after'),))

    def test_quoted_operator_cannot_hide_a_forbidden_following_command(self):
        for text in (
            "echo '<<' 'EOF'\npython -m venv env",
            "echo '<<' 'EOF'\nuv sync",
            "timeout -s INT 30 bash -lc \"echo '<<' 'EOF'; python -m venv env\"",
            'env -u SOME_VARIABLE stdbuf -oL timeout --signal INT 30 python -m venv env',
        ):
            with self.subTest(text=text):
                effects = shell_storage_effects(text, CWD)
                self.assertEqual(len(effects), 1)
                self.assertIsInstance(effects[0], ForbiddenStorageEffect)

    def test_real_documents_remain_opaque_and_following_commands_survive(self):
        cases = (
            "python - <<'PY'\nprint('> picked it off the wheel')\nPY\nmkdir target",
            "python - <<'PY' # a header comment\nprint('> picked')\nPY\nmkdir target",
            "cat <<'A' <<'B'\nuv sync\nA\npython -m venv env\nB\nmkdir target",
            "cat <<-'A'\n\t'not shell\n\tA\nmkdir target",
            "cat <<A\nordinary text\nA\nmkdir target",
            "cat <<E\"OF\"\n$(uv sync)\nEOF\nmkdir target",
            "cat <<E\"\"\\\nOF\n$(touch hidden)\nEOF\nmkdir target",
            "cat <<\\E\\\nOF\n$(touch hidden)\nEOF\nmkdir target",
            'cat <<"E\\\nOF"\n$(touch hidden)\nEOF\nmkdir target',
            "cat <<'EOF'\n>>> '''not shell\nEOF\nmkdir target",
            "cat <<'EOF'; echo header\n>& 'not shell\nEOF\nmkdir target",
            shlex.join(('bash', '-c', "python - <<'PY'\nprint(\"it's > picked\")\nPY\nmkdir target")),
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(shell_storage_effects(text, CWD), (WriteStorageEffect(CWD / 'target'),))

    def test_document_logical_termination_preserves_following_commands(self):
        for text in (
            'cat <<EOF\nE\\\nOF\nmkdir target',
            'cat <<-EOF\n\tE\\\nOF\nmkdir target',
            "cat <<'EOF'\n$\\\n(touch hidden)\nEOF\nmkdir target",
            "cat <<-'EOF'\n\t$\\\n(touch hidden)\n\tEOF\nmkdir target",
            "cat <<'EOF'\nE\\\nOF\nEOF\nmkdir target",
            'cat <<EOF\nliteral' + '\\' * 2 + '\nEOF\nmkdir target',
            'cat <<EOF\nnot-end\\\nEOF\nstill body\nEOF\nmkdir target',
        ):
            with self.subTest(text=text):
                self.assertEqual(tuple(command.words for command in ShellCommand.parsed(text)),
                                 (('cat',), ('mkdir', 'target')))
                self.assertEqual(shell_storage_effects(text, CWD), (WriteStorageEffect(CWD / 'target'),))

    def test_document_scope_and_unsupported_expansion_are_explicit(self):
        for text in (
            'cat <<EOF\n$(uv sync)\nEOF\n',
            'cat <<EOF\n`uv sync`\nEOF\n',
            'cat <<EOF\n$\\\n(touch target)\nEOF\n',
            'cat <<-EOF\n\t$\\\n(touch target)\n\tEOF\n',
            'cat <<EOF\n$\\\n\\\n(touch target)\nEOF\n',
            "cat <<EOF\n'$\\\n(touch target)'\nEOF\n",
            'cat <<EOF\nnot-end\\\nEOF\n$\\\n(touch target)\nEOF\n',
            'cat <<E\\\nOF\n$(touch target)\nEOF\n',
            'cat <<-E\\\nOF\n\t`touch target`\n\tEOF\n',
            "cat <<'EOF'\nno terminator",
            "cat <<'EOF'\n\tEOF\n",
            'echo >',
        ):
            with self.subTest(text=text):
                with self.assertRaises(StorageDenied):
                    shell_storage_effects(text, CWD)

    def test_assignments_use_logical_spelling_without_erasing_real_quoting(self):
        for prefix in (
            'FO\\\nO=value',
            'FOO=va\\\nlue',
            'FO\\\nO="va\\\nlue"',
            "FO\\\nO='va\\\nlue'",
            '\\\nFOO=value',
            "FOO=va''lue",
        ):
            text = prefix + ' mkdir target'
            with self.subTest(text=text):
                command, = ShellCommand.parsed(text)
                self.assertEqual(command.words, ('mkdir', 'target'))
                self.assertEqual(shell_storage_effects(text, CWD), (WriteStorageEffect(CWD / 'target'),))
        for prefix in (r'F\OO=value', r'FOO\=value', "'FOO'=value", "FOO''=value"):
            text = prefix + ' mkdir target'
            with self.subTest(text=text):
                command, = ShellCommand.parsed(text)
                self.assertEqual(command.words, ('FOO=value', 'mkdir', 'target'))

    def test_continued_operators_keep_one_physical_source_boundary(self):
        for text in (
            'echo value >\\\n> target',
            'echo value &\\\n> target',
            'echo value &>\\\n> target',
            'echo value >\\\n| target',
        ):
            with self.subTest(text=text):
                command, = ShellCommand.parsed(text)
                self.assertEqual(command.words, ('echo', 'value'))
                self.assertEqual(shell_storage_effects(text, CWD), (WriteStorageEffect(CWD / 'target'),))
        text = 'cat <\\\n<\\\n-EOF\n\tliteral\n\tEOF\nmkdir target'
        self.assertEqual(tuple(command.words for command in ShellCommand.parsed(text)),
                         (('cat',), ('mkdir', 'target')))
        self.assertEqual(shell_storage_effects(text, CWD), (WriteStorageEffect(CWD / 'target'),))

    def test_reserved_brace_groups_cannot_hide_the_first_executable(self):
        for text in (
            '{ mkdir target; }',
            '\\\n{ mkdir target; \\\n}',
            '{\\\n mkdir target; }',
            'A=literal \\\n{ mkdir target; }',
            '{ python -m venv env; }',
            'mkdir before && { (mkdir inner) & printf ready; }',
            'A=literal { mkdir target; }',
            '}',
            shlex.join(('bash', '-c', '{ mkdir target; }')),
        ):
            with self.subTest(text=text), self.assertRaises(StorageDenied):
                shell_storage_effects(text, CWD)
        literal, = ShellCommand.standalone('echo { }')
        self.assertEqual(literal.words, ('echo', '{', '}'))
        self.assertEqual(tuple(ShellCommand.standalone('{ mkdir target; }')), ())

    def test_redirection_operands_are_not_arguments(self):
        cases = (
            ('echo value > target', ('echo', 'value'), ('target',)),
            ('echo 2> target', ('echo',), ('target',)),
            ("echo '2'> target", ('echo', '2'), ('target',)),
            ('echo 2 > target', ('echo', '2'), ('target',)),
            ('echo value 2>&1', ('echo', 'value'), ()),
            ('echo ٢> target', ('echo', '٢'), ('target',)),
            ('echo >&٢', ('echo',), ('٢',)),
            ('echo value >& target', ('echo', 'value'), ('target',)),
            ('cat < input', ('cat',), ()),
            ('cat 2<input', ('cat',), ()),
            ('cat 2<&0', ('cat',), ()),
            ('cat <> target', ('cat',), ('target',)),
            ('cat <<< text', ('cat',), ()),
            ('cat 2<<<text', ('cat',), ()),
            ("cat 2<<'EOF'\nopaque body\nEOF\n", ('cat',), ()),
            ("cat 2<<-'EOF'\n\topaque body\n\tEOF\n", ('cat',), ()),
            ('> target', (), ('target',)),
            ('> {', (), ('{',)),
            ('echo > }', ('echo',), ('}',)),
            ('echo &>> target', ('echo',), ('target',)),
        )
        for number in ('2', '700'):
            continued = number[:1] + '\\\n' + number[1:]
            retained = (f"'{number}'", f'"{number}"', '\\' + number, number + ' ',
                        f"'{number}'\\\n", number[:1] + "''\\\n" + number[1:])
            for operator in ('&>', '&>>'):
                cases += tuple((f'mkdir {prefix}{operator}/owned/log', ('mkdir', number),
                                ('/owned/log', number)) for prefix in (number, continued, *retained))
            for operator in ('>', '>>', '>|', '<>'):
                cases += tuple((f'mkdir {prefix}{operator}/owned/log', ('mkdir',), ('/owned/log',))
                               for prefix in (number, continued))
                cases += tuple((f'mkdir {prefix}{operator}/owned/log', ('mkdir', number),
                                ('/owned/log', number)) for prefix in retained)
        for text, words, paths in cases:
            with self.subTest(text=text):
                command, = ShellCommand.parsed(text)
                self.assertEqual(command.words, words)
                self.assertEqual(shell_storage_effects(text, CWD), tuple(
                    WriteStorageEffect(CWD / path) for path in paths))

    def test_option_operands_belong_to_the_declaring_program(self):
        cases = (
            ('mkdir -m 700 target', ('target',)),
            ('mkdir target --mode=700', ('target',)),
            ('mkdir -pm700 target other', ('target', 'other')),
            ('mkdir -- -m 700', ('-m', '700')),
            ('mkdir --context target', ('target',)),
            ('mkdir --context=label target', ('target',)),
            ('touch -d tomorrow -r reference target', ('target',)),
            ('touch -t 202611010100 target', ('target',)),
            ('touch -', ()),
            ('tee --output-error=warn -a target', ('target',)),
            ('cp --reflink=auto --sparse always source target', ('target',)),
            ('cp source -t target', ('target',)),
            ('mv -t target source', ('target', 'source')),
            ('install -m 700 -g group source target', ('target',)),
            ('install -dm700 one two', ('one', 'two')),
            ('ln -s source', ('.',)),
            ('ln -s -t target source', ('target',)),
            ('rsync -avt -e "ssh -p 2222" --exclude pattern source target', ('target',)),
            ('rsync --log-file=log source target', ('target', 'log')),
            ('mkdir --help', ()),
            ('mkdir -- --help', ('--help',)),
        )
        for text, paths in cases:
            with self.subTest(text=text):
                self.assertEqual(shell_storage_effects(text, CWD), tuple(
                    WriteStorageEffect((CWD / path).resolve()) for path in paths))

    def test_bad_or_unknown_option_operands_are_not_guessed(self):
        for text in ('mkdir -m', 'touch -r', 'cp --invented path source target',
                     'rsync source host:target', 'rsync --partial-dir relative source target',
                     'bash +e -c "uv sync"', 'env -C elsewhere mkdir target'):
            with self.subTest(text=text):
                with self.assertRaises(StorageDenied):
                    shell_storage_effects(text, CWD)

    def test_redirections_use_execution_directory_before_cd(self):
        text = 'cd next > before; mkdir -m 700 after'
        self.assertEqual(shell_storage_effects(text, CWD), (
            WriteStorageEffect(CWD / 'before'), WriteStorageEffect(CWD / 'next' / 'after')))
        self.assertEqual([invocation.cwd for invocation in ShellInvocation.parse(text, CWD)],
                         [CWD, CWD / 'next'])

    def test_literal_recovery_uses_the_same_operator_boundary(self):
        valid = 'agent-comms-claude-bridge watch --state "quoted;path"'
        command, = ShellCommand.standalone(valid)
        self.assertEqual(command.args[-1], 'quoted;path')
        for text in ('echo ok; echo no', 'echo "$HOME"', 'echo `date`', 'echo ok > file',
                     'echo ok\necho no', '(echo ok)', '\\\n' + valid,
                     valid.replace('watch', 'wa\\\ntch')):
            with self.subTest(text=text):
                self.assertEqual(tuple(ShellCommand.standalone(text)), ())

    def test_child_execution_owns_its_directory_lifetime(self):
        for text, paths in (
            ('bash -c "cd child; mkdir inner"; mkdir outer', ('child/inner', 'outer')),
            ('(cd child; mkdir inner); mkdir outer', ('child/inner', 'outer')),
            ('((cd child; mkdir inner)); mkdir outer', ('child/inner', 'outer')),
            ('timeout -s INT 5 env -u OLD bash -c "cd child; mkdir inner"; mkdir outer', ('child/inner', 'outer')),
            ('(cd child && mkdir inner) > group.log; mkdir outer', ('group.log', 'child/inner', 'outer')),
            ("(cat <<'EOF') > group.log\n> not shell\nEOF\nmkdir outer", ('group.log', 'outer')),
        ):
            with self.subTest(text=text):
                self.assertEqual(shell_storage_effects(text, CWD), tuple(
                    WriteStorageEffect(CWD / path) for path in paths))
        for text in ('(mkdir target', ')', '()', '(mkdir a)(mkdir b)', '(mkdir a) trailing'):
            with self.subTest(text=text), self.assertRaises(StorageDenied):
                shell_storage_effects(text, CWD)

    def test_waits_follow_source_scope_and_runner_ownership(self):
        script = 'while kill -0 123; do sleep 1; done'
        for text, bounded in (
            (script, False),
            ('whi\\\nle kill -0 123; do sleep 1; done', False),
            ('un\\\ntil kill -0 123; do sleep 1; done', False),
            ('(' + script + ')', False),
            (shlex.join(('bash', '-o', 'pipefail', '-c', script)), False),
            (shlex.join(('timeout', '-s', 'INT', '5', 'bash', '-c', script)), True),
            (shlex.join(('env', '-u', 'OLD', 'timeout', '5', 'bash', '-c', '(' + script + ')')), True),
        ):
            with self.subTest(text=text):
                waits = tuple(WaitCommand(text).waits())
                self.assertEqual(len(waits), 1)
                self.assertEqual(waits[0].bounded, bounded)
        for text in (
            '"while" kill -0 123',
            'whi""\\\nle kill -0 123',
            '\\whi\\\nle kill -0 123',
        ):
            with self.subTest(text=text):
                self.assertEqual(tuple(WaitCommand(text).waits()), ())
        polling = shlex.join(('timeout', '5', 'bash', '-c',
                             'while env -u OLD pgrep -f worker; do sleep 1; done'))
        self.assertTrue(tuple(WaitCommand(polling).waits())[0].polls_pgrep_f())

    def test_document_capabilities_compose(self):
        parsed, = ShellCommand.parsed("cat <<'EOF'\nliteral\nEOF\n")
        self.assertEqual(parsed.redirections, (HereDocument('EOF'),))
