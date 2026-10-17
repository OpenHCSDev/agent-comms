"""Start native OpenCode with one parsed listener address and native arguments."""
import argparse
import os
import shutil
import sys


def main() -> None:
    # Search the system path: the user's `opencode` launcher may point here.
    native = shutil.which('opencode', path=os.defpath)
    if native is None:
        raise FileNotFoundError('Install OpenCode before starting its comms integration')
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--hostname', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=0)
    listener, remaining = parser.parse_known_args(sys.argv[1:])
    os.execv(native, [native, '--hostname', listener.hostname, '--port', str(listener.port), *remaining])
