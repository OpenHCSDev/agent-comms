"""The debt ratchet's command line, imported once and run for each request in turn.

A comparison run as ``tools/debt_ratchet.py`` pays the interpreter's start and the ratchet's
imports (about 2.6 s) before its first git call; this serves the same ``main()`` from one
interpreter per test worker. Each comparison still forks its own measuring pool from here, as
the command line does. Its caches are keyed by repository and commit, so one comparison's
entries never answer another's.

One JSON request per connection, ``{"cwd", "argv", "env", "stdout", "stderr"}``; the reply is
``{"returncode"}`` once the command's output is in the named files.
"""

import importlib.util
import json
import os
import socket
import sys
import traceback
from pathlib import Path

RATCHET = Path(__file__).resolve().parents[2] / "tools/debt_ratchet.py"


def imported():
    """The ratchet with ``tools/`` first on the path, as running the script puts it."""
    sys.path.insert(0, str(RATCHET.parent))
    spec = importlib.util.spec_from_file_location("debt_ratchet", RATCHET)
    module = importlib.util.module_from_spec(spec)
    # Its pool pickles work by module name, so the forked workers must find it by that name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run(ratchet, request: dict) -> int:
    os.chdir(request["cwd"])
    os.environ.clear()
    os.environ.update(request["env"])
    sys.argv = [str(RATCHET), *request["argv"]]
    saved = os.dup(1), os.dup(2)
    with open(request["stdout"], "wb") as stdout, open(request["stderr"], "wb") as stderr:
        os.dup2(stdout.fileno(), 1)
        os.dup2(stderr.fileno(), 2)
        try:
            return ratchet.main()
        except SystemExit as exit:
            return exit.code if isinstance(exit.code, int) else 1
        except Exception:
            traceback.print_exc()
            return 1
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            for descriptor, original in zip((1, 2), saved, strict=True):
                os.dup2(original, descriptor)
                os.close(original)


def serve(path: str) -> None:
    ratchet = imported()
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        server.listen()
        print("ready", flush=True)
        while True:
            connection, _ = server.accept()
            with connection, connection.makefile("rwb") as stream:
                returncode = run(ratchet, json.loads(stream.readline()))
                stream.write(json.dumps({"returncode": returncode}).encode() + b"\n")


if __name__ == "__main__":
    serve(sys.argv[1])
