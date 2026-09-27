"""Content provenance for the complete, immutable-by-policy copied Pi package.

Includes dependencies, package metadata, hidden files and native resources; no
mtime cache or caller-provided success marker. Not a sandbox against arbitrary
same-UID code or an installer mutating code after verification. Preparation must
publish new build directories, never edit a package used by a running process.
This stdlib-only file is also invoked directly by the preparation/launch scripts.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

TREE_PREFIX = "# agent-comms-native-tree-v1 "
MAX_ENTRIES = 30_000
MAX_BYTES = 512 * 1024**2
MAX_DEPTH = 32


class NativePackageError(ValueError):
    """Package bytes or filesystem shape are not the pinned native artifact."""


def _resource_root() -> Path:
    module = Path(__file__).resolve()
    packaged = module.with_name("_native")
    if packaged.is_dir():
        return packaged
    if module.parent.name == "agent_comms" and module.parent.parent.name == "src":
        return module.parents[2] / "stack"
    raise NativePackageError("Installed native package resources are unavailable")


MANIFEST = _resource_root() / "pi-native.sha256"
COMPACTION_HELPER = _resource_root() / "native-compaction-commit-child.mjs"


def package_tree_digest(root: Path) -> str:
    """Hash sorted path/type/execute-mode/size/content records with bounded reads."""
    if os.name != "posix":
        raise NativePackageError("Native package filesystem verification requires POSIX")
    digest = hashlib.sha256()
    count, total = 1, 0

    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_uid,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def visit(path: Path, relative: str, depth: int) -> None:
        nonlocal count, total
        if count > MAX_ENTRIES or depth > MAX_DEPTH:
            raise NativePackageError("Native package inventory limit exceeded")
        before = path.lstat()
        if before.st_uid not in (0, os.getuid()) or before.st_mode & 0o022:
            raise NativePackageError("Native package is not owner-controlled")
        if stat.S_ISDIR(before.st_mode):
            record = ["directory", relative, before.st_mode & 0o111]
            digest.update((json.dumps(record, ensure_ascii=True) + "\n").encode())
            names = []
            with os.scandir(path) as entries:
                for entry in entries:
                    count += 1  # Include pending siblings before recursive descent.
                    if count > MAX_ENTRIES:
                        raise NativePackageError("Native package inventory limit exceeded")
                    names.append(entry.name)
            for name in sorted(names):
                visit(path / name, f"{relative}/{name}", depth + 1)
        elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
            total += before.st_size
            if total > MAX_BYTES:
                raise NativePackageError("Native package byte limit exceeded")
            content = hashlib.sha256()
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if identity(os.fstat(fd)) != identity(before):
                    raise NativePackageError("Native package changed while opening")
                remaining = before.st_size
                while remaining:
                    block = os.read(fd, min(remaining, 1024 * 1024))
                    if not block:
                        raise NativePackageError("Native package truncated while hashing")
                    content.update(block)
                    remaining -= len(block)
                if os.read(fd, 1) or identity(os.fstat(fd)) != identity(before):
                    raise NativePackageError("Native package changed while hashing")
            finally:
                os.close(fd)
            record = ["file", relative, before.st_mode & 0o111, before.st_size, content.hexdigest()]
            digest.update((json.dumps(record, ensure_ascii=True) + "\n").encode())
        else:
            raise NativePackageError("Native package contains links or special files")
        if identity(path.lstat()) != identity(before):
            raise NativePackageError("Native package changed during verification")

    try:
        visit(root, ".", 0)
    except OSError as error:
        raise NativePackageError("Native package could not be verified") from error
    return digest.hexdigest()


def verify_native_package(root: Path) -> None:
    """Verify the entire copied package against the repository-owned commitment."""
    pins = [
        line[len(TREE_PREFIX) :]
        for line in MANIFEST.read_text().splitlines()
        if line.startswith(TREE_PREFIX)
    ]
    if len(pins) != 1 or len(pins[0]) != 64 or any(c not in "0123456789abcdef" for c in pins[0]):
        raise NativePackageError("Pinned native package tree commitment unavailable")
    if package_tree_digest(root) != pins[0]:
        raise NativePackageError("Native package tree differs from pinned artifact")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--digest":
        print(package_tree_digest(Path(sys.argv[2])))
    elif len(sys.argv) == 2:
        verify_native_package(Path(sys.argv[1]))
    else:
        raise SystemExit("Usage: native_package.py [--digest] PACKAGE_DIR")
