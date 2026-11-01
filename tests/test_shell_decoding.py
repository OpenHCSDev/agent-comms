"""Data-only shell boundary controls: no commands, subprocesses or filesystem writes."""
from __future__ import annotations

from pathlib import Path
import shlex
import unittest

from agent_comms.shell_effects import (
    ForbiddenStorageEffect, ShellInvocation, StorageDenied, WriteStorageEffect,
    shell_storage_effects,
)
from agent_comms_hooks.bounded_waits import WaitCommand
from agent_comms_hooks.shell_commands import ConnectCommand, HereDocument, ShellCommand


CWD = Path('/shell-decoder-control')


class ShellDecoding(unittest.TestCase):
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
            ('echo file{brace}', ('echo', 'file{brace}')),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                commands = tuple(ShellCommand.parsed(text))
                self.assertEqual(commands[0].words, expected)
                self.assertEqual(shell_storage_effects(text, CWD), ())

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
            "cat <<'EOF'\n>>> '''not shell\nEOF\nmkdir target",
            "cat <<'EOF'; echo header\n>& 'not shell\nEOF\nmkdir target",
            shlex.join(('bash', '-c', "python - <<'PY'\nprint(\"it's > picked\")\nPY\nmkdir target")),
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(shell_storage_effects(text, CWD), (WriteStorageEffect(CWD / 'target'),))

    def test_document_scope_and_unsupported_expansion_are_explicit(self):
        for text in (
            'cat <<EOF\n$(uv sync)\nEOF\n',
            'cat <<EOF\n`uv sync`\nEOF\n',
            "cat <<'EOF'\nno terminator",
            "cat <<'EOF'\n\tEOF\n",
            'echo >',
        ):
            with self.subTest(text=text):
                with self.assertRaises(StorageDenied):
                    shell_storage_effects(text, CWD)

    def test_reserved_brace_groups_cannot_hide_the_first_executable(self):
        for text in (
            '{ mkdir target; }',
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
            retained = (f"'{number}'", f'"{number}"', '\\' + number, number + ' ')
            for operator in ('&>', '&>>'):
                cases += tuple((f'mkdir {prefix}{operator}/owned/log', ('mkdir', number),
                                ('/owned/log', number)) for prefix in (number, *retained))
            for operator in ('>', '>>', '>|', '<>'):
                cases += ((f'mkdir {number}{operator}/owned/log', ('mkdir',), ('/owned/log',)),)
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
                     'echo ok\necho no', '(echo ok)'):
            with self.subTest(text=text):
                self.assertEqual(tuple(ShellCommand.standalone(text)), ())
        self.assertTrue(ConnectCommand.permits('agent-comms-claude-bridge connect'))
        self.assertFalse(ConnectCommand.permits('agent-comms-claude-bridge connect > file'))

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
            ('(' + script + ')', False),
            (shlex.join(('bash', '-o', 'pipefail', '-c', script)), False),
            (shlex.join(('timeout', '-s', 'INT', '5', 'bash', '-c', script)), True),
            (shlex.join(('env', '-u', 'OLD', 'timeout', '5', 'bash', '-c', '(' + script + ')')), True),
        ):
            with self.subTest(text=text):
                waits = tuple(WaitCommand(text).waits())
                self.assertEqual(len(waits), 1)
                self.assertEqual(waits[0].bounded, bounded)
        self.assertEqual(tuple(WaitCommand('"while" kill -0 123').waits()), ())
        polling = shlex.join(('timeout', '5', 'bash', '-c',
                             'while env -u OLD pgrep -f worker; do sleep 1; done'))
        self.assertTrue(tuple(WaitCommand(polling).waits())[0].polls_pgrep_f())

    def test_document_capabilities_compose(self):
        parsed, = ShellCommand.parsed("cat <<'EOF'\nliteral\nEOF\n")
        self.assertEqual(parsed.redirections, (HereDocument('EOF'),))


if __name__ == '__main__':
    unittest.main()
