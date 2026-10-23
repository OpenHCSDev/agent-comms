"""Select the package's Core stamp using the build target."""
import subprocess
from pathlib import Path
from runpy import run_path
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
        # These definitions need no application dependencies in the build env.
        types = run_path(str(root / "src/agent_comms/core_stamp.py"))
        # Never overwrite a source checkout's stamp while building its wheel.
        path = Path(self.directory) / ".core_stamp.py"
        if (root / ".git").exists():
            # Repo.git cannot load here, so the build reads its commit directly.
            commit = subprocess.check_output(
                ("git", "-C", str(root.resolve()), "rev-parse", "HEAD"), text=True,
            ).strip()
            types["WheelStamp"](commit).write(path)
        elif not (commit := "$Format:%H$").startswith("$Format"):
            types["WheelStamp"](commit).write(path)
        else:
            # A source distribution already carries its generated wheel value.
            path.parent.mkdir(parents=True, exist_ok=True)
            copyfile(source, path)
        destination = relative.removeprefix("src/") if self.target_name == "wheel" else relative
        self.build_config.build_reserved_paths.add(destination)
        build_data["force_include"][str(path)] = destination

    def finalize(self, version, build_data, artifact_path):
        (Path(self.directory) / ".core_stamp.py").unlink(missing_ok=True)
