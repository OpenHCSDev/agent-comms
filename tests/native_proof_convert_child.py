"""Kill the real one-shot converter around atomic publication, in a private fixture."""

import os
import signal
import sys
from pathlib import Path

from tools.cutover.native_proof_journal import convert

publish = os.replace
boundary, session, backup = sys.argv[1:]


def interrupted_publish(source, target):
    if boundary == "after":
        publish(source, target)
    os.kill(os.getpid(), signal.SIGKILL)


os.replace = interrupted_publish
convert(Path(session), Path(backup))
