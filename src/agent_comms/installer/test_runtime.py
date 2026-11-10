"""Run source-checkout tests in a private built runtime.

Usage: python -m agent_comms.installer.test_runtime [--home DIR] PYTEST_ARGS
The default home is disposable; --home retains the private test build for inspection.
Neither path publishes a runtime or changes the live installation.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from agent_comms.execution.test_lease import InheritedTestRun, TestLease
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.runtime_home import TestHome
from agent_comms.installer.runtime_stack import RuntimeStack
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.storage_rules import StorageRule


def main() -> None:
    scratch = StorageRule().scratch_base
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="test-runtime-") as temporary:
        parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
        parser.add_argument(
            "--home",
            type=Path,
            default=Path(temporary) / "home",
            help="private runtime home to retain for inspection",
        )
        parser.add_argument(InheritedTestRun.option, type=InheritedTestRun.argument,
                            default=TestLease.default_start)
        options, arguments = parser.parse_known_args()
        home = options.home.expanduser().resolve()
        if home == Path.home().resolve():
            parser.error("--home must not name the live home")
        TestHome(home).prepare()
        with InstallCommands() as commands:
            built = RuntimeStack.build(Checkout.prepared(Path.cwd()), commands, home)
            built.complete()
            built.test(*arguments, start=options.integration_lease)


if __name__ == "__main__":
    main()
