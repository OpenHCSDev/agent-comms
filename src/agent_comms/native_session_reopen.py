"""Read-only strict native session validation before a discarded idle Pi reopens.

Never use SessionManager.open for this preflight: it can migrate a legacy file.
The pinned disk entry owner enforces its actual strict v3 parse.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

from .native_package import MANIFEST, verify_native_package

_READ_ONLY_SESSION = r"""
import {realpathSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const [root, file] = process.argv.slice(1);
const {DiskEntryStore} = await import(pathToFileURL(join(root, 'dist/core/session-entry-store.js')));
const store = new DiskEntryStore(file);
try {
  store.assertCurrent();
  console.log(JSON.stringify({sessionId:store.header.id, sessionFile:realpathSync(file)}));
} finally {store.close();}

"""


class NativeReopenError(ValueError):
    """Saved session cannot be safely reloaded; no provider input may start."""


def package_for_launcher(launcher: str) -> Path:
    """Bind a canonical launcher to the wheel/source-owned complete-tree pin."""
    executable = Path(shutil.which(launcher) or launcher).resolve(strict=True)
    if executable.name == "pi-comms-native":
        from .coordination_store import PublicationActivationBlocked
        from .native_pi import NativePiUnavailable
        from .private_nk_entrypoint import private_nk_from_environment

        try:
            launch = private_nk_from_environment()
        except (NativePiUnavailable, PublicationActivationBlocked) as error:
            raise NativeReopenError("Native owner route is unavailable") from error
        if launch is None:
            raise NativeReopenError("Native owner backend requires a configured private route")
        # Use the launcher's route owner, then apply the same complete-tree
        # commitment as the legacy launcher. A path or seven-file probe alone
        # cannot select different preparation/commit helpers after compaction.
        verify_native_package(launch.native_package)
        return launch.native_package
    if executable.name != "pi-native" or executable.parent.name != "bin":
        raise NativeReopenError("Canonical native launcher required for saved-session reopen")
    stack = executable.parent.parent
    manifest = stack / "pi-native.sha256"
    if manifest.read_bytes() != MANIFEST.read_bytes():
        raise NativeReopenError("Launcher and Python native commitments differ")
    build = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()[:16]
    package = stack / f".pi-native-{build}" / "node_modules/@earendil-works/pi-coding-agent"
    verify_native_package(package)
    return package


def validate_native_reopen(
    launcher: str, session_file: str, *, expected_session_id: str | None = None
) -> str:
    """Return the strict saved session ID; never mutate/recover an invalid file."""
    try:
        package = package_for_launcher(launcher)
        file = Path(session_file).absolute()
        if file != file.resolve(strict=True):
            raise NativeReopenError("Saved native session path is not canonical")
        before = file.stat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in (0, os.getuid())
            or before.st_size <= 0
        ):
            raise NativeReopenError("Saved native session is not a bounded regular file")
        node = shutil.which("node")
        if node is None:
            raise NativeReopenError("Native session validator unavailable")
        environment = dict(os.environ)
        for key in ("NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE"):
            environment.pop(key, None)
        environment["NODE_DISABLE_COMPILE_CACHE"] = "1"
        environment["PI_OFFLINE"] = "1"
        result = subprocess.run(
            [
                node,
                "--no-global-search-paths",
                "--import",
                str(package / "dist/agent-comms-import-fence.mjs"),
                "--input-type=module",
                "--eval",
                _READ_ONLY_SESSION,
                str(package),
                str(file),
            ],
            env=environment,
            cwd=file.parent,
            capture_output=True,
            timeout=10,
        )
        after = file.stat()

        def revision(info: os.stat_result) -> tuple[int, ...]:
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        if result.returncode or revision(before) != revision(after) or len(result.stdout) > 4096:
            raise NativeReopenError("Saved native session validation failed or changed")
        data = json.loads(result.stdout)
        if (
            not isinstance(data, dict)
            or set(data) != {"sessionId", "sessionFile"}
            or type(data["sessionId"]) is not str
            or not data["sessionId"]
            or data["sessionFile"] != str(file)
            or (expected_session_id is not None and data["sessionId"] != expected_session_id)
        ):
            raise NativeReopenError("Saved native session identity changed")
        return data["sessionId"]
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, NativeReopenError):
            raise
        raise NativeReopenError("Saved native session cannot be validated") from error
