"""Use the existing test runner with the granted thin installation first."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import agent_comms

def main():
    repository = Path(__file__).resolve().parents[2]
    installed = Path(agent_comms.__file__).resolve().parent
    wheel = repository / ".artifacts/native-source640-wheels-20261004/agent_comms-0.1.0-py3-none-any.whl"
    assert installed == Path(sys.prefix) / "lib/python3.14/site-packages/agent_comms"
    with zipfile.ZipFile(wheel) as archive:
        members = [name for name in archive.namelist() if name.startswith("agent_comms/")]
        for member in members:
            assert (installed.parent / member).read_bytes() == archive.read(member), member
    proof = {
        "python": sys.executable,
        "installed_package": str(installed),
        "wheel": str(wheel),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "installed_members_equal": len(members),
        "distributions": {item.metadata["Name"]: item.version
                          for item in importlib.metadata.distributions()},
        "direct_url": json.loads(importlib.metadata.distribution("agent-comms")
                                 .read_text("direct_url.json")),
    }
    # The application stays first on this interpreter's normal installed path.
    # Only the already installed system-Python test runner is added at the end.
    # No src/PYTHONPATH overlay, dependency install or new environment.
    runner_library = subprocess.check_output([
        "/usr/bin/python", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))",
    ], text=True).strip()
    sys.path.append(runner_library)
    sys.path.insert(0, str(repository / "tests"))
    import pytest
    import pytest_asyncio

    proof["test_runner"] = {"library": runner_library,
                            "pytest": pytest.__file__, "asyncio": pytest_asyncio.__file__}
    Path(os.environ["SELECTED_NATIVE_PROOF"]).write_text(json.dumps(proof, indent=2) + "\n")
    raise SystemExit(pytest.main(sys.argv[1:]))


if __name__ == "__main__":
    main()
