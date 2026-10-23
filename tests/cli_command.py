"""The command lines run in this process: their real parsers, dispatch and exit codes, without
an interpreter's start and Core's imports for every invocation.

A test about the process itself (its identity, signals, environment read at import, or two
holders racing for one lock) launches the real command instead.
"""

import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from agent_comms import cli
from agent_comms.commander import cli as commander_cli


def core(*argv: str) -> subprocess.CompletedProcess[str]:
    """``agent-comms ARGV``."""
    output, error = StringIO(), StringIO()
    with redirect_stdout(output), redirect_stderr(error):
        code = cli.main(list(argv))
    return subprocess.CompletedProcess(argv, code, output.getvalue(), error.getvalue())


def commander(*argv: str) -> subprocess.CompletedProcess[str]:
    """``agent-comms-commander ARGV``, which reads its arguments from sys.argv."""
    argv = ('agent-comms-commander', *argv)
    output, error = StringIO(), StringIO()
    code = 0
    with patch.object(sys, 'argv', list(argv)), redirect_stdout(output), redirect_stderr(error):
        try:
            commander_cli.main()
        except SystemExit as exited:
            # CPython prints a non-integer SystemExit value and returns 1.
            if isinstance(exited.code, str):
                print(exited.code, file=error)
                code = 1
            else:
                code = exited.code or 0
    return subprocess.CompletedProcess(argv, code, output.getvalue(), error.getvalue())
