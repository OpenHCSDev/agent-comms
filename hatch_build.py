"""Select the package's Core stamp using the build target."""

import json
import os
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from shutil import copyfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CoreBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        if version == "editable":
            return
        root = Path(self.root)
        relative = "src/agent_comms/package_stamp.py"
        source = root / relative
        # Load the stamp under its package name, so the generated value imports its type
        # from the same place at runtime.
        spec = spec_from_file_location(
            "agent_comms.core_stamp", root / "src/agent_comms/core_stamp.py"
        )
        types = module_from_spec(spec)
        spec.loader.exec_module(types)
        # Never overwrite a source checkout's stamp while building its wheel.
        path = Path(self.directory) / ".core_stamp.py"
        if (root / ".git").exists():
            # Repo.git cannot load here, so the build reads its commit directly.
            commit = subprocess.check_output(
                ("git", "-C", str(root.resolve()), "rev-parse", "HEAD"),
                text=True,
            ).strip()
            types.WheelStamp(commit, self.producer_digests(root)).write(path)
        elif not (commit := "$Format:%H$").startswith("$Format"):
            types.WheelStamp(commit, self.producer_digests(root)).write(path)
        else:
            # A source distribution already carries its generated wheel value.
            path.parent.mkdir(parents=True, exist_ok=True)
            copyfile(source, path)
        destination = relative.removeprefix("src/") if self.target_name == "wheel" else relative
        self.build_config.build_reserved_paths.add(destination)
        build_data["force_include"][str(path)] = destination

    def producer_digests(self, root):
        """Import the bus's declared schema once while building its immutable wheel."""
        return json.loads(subprocess.check_output(
            (sys.executable, "-c",
             "import json; from agent_comms.bus import Bus; "
             "from agent_comms.typed_table import ProducedTable; "
             "print(json.dumps({row.declared_name: row.producer_digest() "
             "for row in ProducedTable.members_with(ProducedTable)}, sort_keys=True))"),
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root / "src")},
            text=True,
        ))

    def finalize(self, version, build_data, artifact_path):
        (Path(self.directory) / ".core_stamp.py").unlink(missing_ok=True)
