"""Launch-only diagnostics for the one authorized isolated installer case."""
import atexit
import faulthandler
import os
from pathlib import Path

STACK = Path(__file__).resolve().parent.parent / f'process-{os.getpid()}.stack.log'
stack = STACK.open('w')
faulthandler.dump_traceback_later(12, file=stack, repeat=False)


def close_stack():
    faulthandler.cancel_dump_traceback_later()
    stack.close()


atexit.register(close_stack)
