"""Start the native OpenCode TUI on a loopback port chosen by the OS."""
import os
import shutil
import sys


def main() -> None:
    # Search the system path: the user's `opencode` launcher may point here.
    native = shutil.which('opencode', path=os.defpath)
    if native is None:
        raise FileNotFoundError('Install OpenCode before starting its comms integration')
    os.execv(native, [native, '--hostname', '127.0.0.1', '--port', '0', *sys.argv[1:]])
