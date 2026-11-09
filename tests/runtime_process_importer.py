"""A system interpreter importing from a runtime on its import path, kept alive.

Its PYTHONPATH names a built runtime's library directory and nothing it was invoked with
does, which is the borrower a command-only scan cannot see.
"""

import importlib
import sys
import time

if __name__ == "__main__":
    importlib.import_module(sys.argv[1])
    time.sleep(120)
