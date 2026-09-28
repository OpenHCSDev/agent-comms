"""Opt-in local SDK fixture using the same typed native launch boundary."""

import os
import sys
from dataclasses import replace
from pathlib import Path

from agent_comms.native_package import verify_native_package
from agent_comms.native_pi import NativePiRpcLaunch


def install_event_host(monkeypatch, launcher, origin, *, delay_settlement=None, comms_tools=False):
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    verify_native_package(package)
    host = Path(__file__).with_suffix(".mjs")
    resolve = NativePiRpcLaunch.package_for_command
    managed = NativePiRpcLaunch.managed

    def package_for_command(command):
        return package if command == launcher else resolve(command)

    def fixture_launch(command, arguments, **kwargs):
        launch = managed(command, arguments, **kwargs)
        if command != launcher:
            return launch
        env = dict(
            launch.env,
            S1_NATIVE_PACKAGE=str(package),
            S1_LOCAL_ORIGIN=origin,
            S1_PYTHON=sys.executable,
        )
        if delay_settlement is not None:
            env["S1_DELAY_SETTLEMENT"] = str(delay_settlement)
        if comms_tools:
            env["S1_COMMS_TOOLS"] = "1"
        options = launch.argv[launch.argv.index(str(package / "dist/cli.js")) + 1 :]
        return replace(launch, argv=("node", str(host), *options), env=env)

    monkeypatch.setattr(NativePiRpcLaunch, "package_for_command", package_for_command)
    monkeypatch.setattr(NativePiRpcLaunch, "managed", fixture_launch)
