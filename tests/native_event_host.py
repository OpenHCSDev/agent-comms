"""Opt-in local SDK fixture using the same typed native launch boundary."""

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from agent_comms.native_package import verify_native_package
from agent_comms.native_pi import NativePiRpcLaunch


def install_event_host(
    monkeypatch,
    launcher,
    origin,
    *,
    delay_settlement=None,
    comms_tools=False,
    native_settings=None,
    ui_probe=None,
):
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
        if native_settings is not None:
            # Exercise Pi's own excursions explicitly; managed owners disable
            # automatic retry/compaction and remain covered by CLI acceptance.
            env["PI_CODING_AGENT_DIR"] = env.pop("AGENT_COMMS_NATIVE_CONFIG_DIR")
            env["S1_NATIVE_SETTINGS"] = json.dumps(native_settings)
        if delay_settlement is not None:
            env["S1_DELAY_SETTLEMENT"] = str(delay_settlement)
        if ui_probe is not None:
            env["S1_UI_PROBE"] = str(ui_probe)
        if comms_tools:
            env["S1_COMMS_TOOLS"] = "1"
        options = launch.argv[launch.argv.index(str(package / "dist/cli.js")) + 1 :]
        return replace(launch, argv=("node", str(host), *options), env=env)

    monkeypatch.setattr(NativePiRpcLaunch, "package_for_command", package_for_command)
    monkeypatch.setattr(NativePiRpcLaunch, "managed", fixture_launch)
