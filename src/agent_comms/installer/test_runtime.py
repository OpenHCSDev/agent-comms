"""Run source-checkout tests in a private built runtime.

Usage: python -m agent_comms.installer.test_runtime [--home DIR] PYTEST_ARGS
The default home is disposable; --home retains a private build for reuse.
Neither path publishes a runtime or changes the live installation.
"""
from __future__ import annotations

import argparse
import tempfile
from contextlib import ExitStack
from pathlib import Path

from agent_comms.installer.practice import prepare_runtime_home
from agent_comms.installer.upgrade import Stack
from agent_comms.storage_rules import StorageRule


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--home", type=Path, help="private runtime home to retain for reuse")
    options, arguments = parser.parse_known_args()
    with ExitStack() as lifetime:
        if options.home is None:
            scratch = StorageRule().scratch_base
            scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = lifetime.enter_context(
                tempfile.TemporaryDirectory(dir=scratch, prefix="test-runtime-"))
            home = Path(temporary) / "home"
        else:
            home = options.home.expanduser().resolve()
            if home == Path.home().resolve():
                parser.error("--home must not name the live home")
        prepare_runtime_home(home)
        Stack.build(Path.cwd(), home).test(*arguments)


if __name__ == "__main__":
    main()
