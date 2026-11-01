"""Data-only shell boundary controls: no commands, subprocesses or filesystem writes."""
from __future__ import annotations

from pathlib import Path
import shlex
import unittest

from agent_comms.shell_effects import (
    ForbiddenStorageEffect, ShellInvocation, StorageDenied, WriteStorageEffect,
    shell_storage_effects,
)
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

    def test_redirection_operands_are_not_arguments(self):
        cases = (
            ('echo value > target', ('echo', 'value'), ('target',)),
            ('echo 2> target', ('echo',), ('target',)),
            ("echo '2'> target", ('echo', '2'), ('target',)),
            ('echo 2 > target', ('echo', '2'), ('target',)),
            ('echo value 2>&1', ('echo', 'value'), ()),
            ('echo value >& target', ('echo', 'value'), ('target',)),
            ('cat < input', ('cat',), ()),
            ('cat <> target', ('cat',), ('target',)),
            ('cat <<< text', ('cat',), ()),
            ('> target', (), ('target',)),
            ('echo &>> target', ('echo',), ('target',)),
        )
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
                     'echo ok\necho no'):
            with self.subTest(text=text):
                self.assertEqual(tuple(ShellCommand.standalone(text)), ())
        self.assertTrue(ConnectCommand.permits('agent-comms-claude-bridge connect'))
        self.assertFalse(ConnectCommand.permits('agent-comms-claude-bridge connect > file'))

    def test_document_capabilities_compose(self):
        parsed, = ShellCommand.parsed("cat <<'EOF'\nliteral\nEOF\n")
        self.assertEqual(parsed.redirections, (HereDocument('EOF'),))


if __name__ == '__main__':
    unittest.main()
