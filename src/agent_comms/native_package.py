"""Content provenance for the complete, immutable-by-policy copied Pi package.

Includes dependencies, package metadata, hidden files and native resources; no
mtime cache or caller-provided success marker. Not a sandbox against arbitrary
same-UID code or an installer mutating code after verification. Preparation must
publish new build directories, never edit a package used by a running process.
This stdlib-only file is also invoked directly by the preparation/launch scripts.
Shared resources must be read-only. Their content commitment is unchanged when
another deployment acquires an inode name; private mutable state is unrelated.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

TREE_PREFIX = "# agent-comms-native-tree-v1 "
MAX_ENTRIES = 30_000
MAX_BYTES = 512 * 1024**2
MAX_DEPTH = 32


class NativePackageError(ValueError):
    """Package bytes or filesystem shape are not the pinned native artifact."""


def _resource_root() -> Path:
    module = Path(__file__).resolve()
    # A checkout owns its resources in stack/, even if build artifacts exist
    # beside the source. Only an installed package owns _native resources.
    if module.parent.name == "agent_comms" and module.parent.parent.name == "src":
        return module.parents[2] / "stack"
    packaged = module.with_name("_native")
    if packaged.is_dir():
        return packaged
    raise NativePackageError("Installed native package resources are unavailable")


MANIFEST = _resource_root() / "pi-native.sha256"
OWNER_INSTRUCTIONS = _resource_root() / "APPEND_SYSTEM.md"


def pinned_location() -> Path:
    build = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()[:16]
    return MANIFEST.parent / f".pi-native-{build}" / "node_modules/@earendil-works/pi-coding-agent"


def pinned_native_package() -> Path:
    if not (package := pinned_location()).is_dir():
        raise NativePackageError(f"Pinned native package directory is missing: {package}")
    return package


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
            info.st_nlink if info.st_mode & 0o222 else 0,
            info.st_uid,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns if info.st_mode & 0o222 else 0,
        )

    def visit(path: str, relative: str, depth: int) -> None:
        nonlocal count, total
        if count > MAX_ENTRIES or depth > MAX_DEPTH:
            raise NativePackageError("Native package inventory limit exceeded")
        before = os.lstat(path)
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
                visit(os.path.join(path, name), f"{relative}/{name}", depth + 1)
        elif stat.S_ISREG(before.st_mode):
            if before.st_nlink != 1 and before.st_mode & 0o222:
                raise NativePackageError("Native package shared resource is writable")
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
            raise NativePackageError("Native package contains symlinks or special files")
        if identity(os.lstat(path)) != identity(before):
            raise NativePackageError("Native package changed during verification")

    try:
        visit(os.fspath(root), ".", 0)
    except OSError as error:
        raise NativePackageError("Native package could not be verified") from error
    return digest.hexdigest()


def share_native_resources(root: Path, deployments: Path) -> dict[str, int]:
    """Seal a NEW patched package, then reuse matching frozen resource inodes.

    The existing build lifetime calls this only after every patch. Old handed
    packages have a different import fence and are never changed or borrowed.
    No donor metadata becomes authority: every shared byte/mode must equal the
    already built resource, and the resulting tree still requires its own pin.
    """
    root = root.resolve(strict=True)
    package_tree_digest(root)
    fence = Path("dist/agent-comms-import-fence.mjs")
    current_fence = (root / fence).read_bytes()
    donors = []
    for deployment in sorted(deployments.glob(".pi-native-*")):
        donor = deployment / "node_modules/@earendil-works/pi-coding-agent"
        candidate = donor / fence
        if not candidate.is_file() or candidate.is_symlink() or donor == root:
            continue
        info = candidate.lstat()
        if info.st_mode & 0o222 or candidate.read_bytes() != current_fence:
            continue
        # Reject all foreign, mutable-shared and aliased filesystem shapes before
        # any resource borrow. This remains content verification, not a cache.
        package_tree_digest(donor)
        donors.append(donor)
    shared_files = shared_bytes = 0
    for path in sorted(root.rglob("*")):
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            continue
        path.chmod(stat.S_IMODE(before.st_mode) & ~0o222)
        for donor in donors:
            source = donor / path.relative_to(root)
            if not source.is_file() or source.is_symlink():
                continue
            observed = source.lstat()
            if observed.st_mode & 0o222 or observed.st_dev != before.st_dev:
                continue
            if observed.st_size != before.st_size or observed.st_mode & 0o111 != before.st_mode & 0o111:
                continue
            if source.read_bytes() != path.read_bytes():
                continue
            # The build owns this unpublished path. Create the replacement first;
            # failure cannot discard its independent compiled resource.
            replacement = path.with_name(path.name + ".sharing")
            os.link(source, replacement, follow_symlinks=False)
            try:
                if replacement.read_bytes() != path.read_bytes():
                    raise NativePackageError("Native resource changed while sharing")
                os.replace(replacement, path)
            finally:
                replacement.unlink(missing_ok=True)
            shared_files += 1
            shared_bytes += before.st_size
            break
    package_tree_digest(root)
    return {"shared_files": shared_files, "shared_content_bytes": shared_bytes}


def write_native_manifest(root: Path) -> None:
    """Commit an assembled package through the existing diagnostic/tree format.

    Preparation owns this new, sealed package. This does not update an existing
    deployment or use an old package record as permission to start a runtime.
    """
    tree = package_tree_digest(root)
    lines = []
    for line in MANIFEST.read_text().splitlines():
        if line.startswith(TREE_PREFIX):
            lines.append(TREE_PREFIX + tree)
        elif line and not line.startswith("#"):
            _, path = line.split("  ", 1)
            lines.append(hashlib.sha256((root / path).read_bytes()).hexdigest() + "  " + path)
        else:
            lines.append(line)
    with tempfile.NamedTemporaryFile(dir=MANIFEST.parent, prefix=".pi-native-pin-",
                                     delete=False) as stream:
        replacement = Path(stream.name)
        try:
            stream.write(("\n".join(lines) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
            os.chmod(replacement, stat.S_IMODE(MANIFEST.stat().st_mode))
            os.replace(replacement, MANIFEST)
        finally:
            replacement.unlink(missing_ok=True)


@dataclass(frozen=True)
class NativeSource:
    """One manifest-owned deployment source and its verified snapshot bytes."""

    destination: Path
    content: bytes

    @classmethod
    def declared(cls, value: object, snapshot_root: Path) -> NativeSource:
        if not isinstance(value, dict):
            raise NativePackageError("Native extension source declaration is invalid")
        path = value.get("path")
        snapshot = value.get("snapshot")
        digest = value.get("sha256")
        byte_count = value.get("bytes")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise NativePackageError("Native extension source declaration is invalid")
        if not isinstance(snapshot, str) or Path(snapshot).is_absolute():
            raise NativePackageError("Native extension source declaration is invalid")
        if not isinstance(digest, str) or len(digest) != 64:
            raise NativePackageError("Native extension source declaration is invalid")
        if any(character not in "0123456789abcdef" for character in digest):
            raise NativePackageError("Native extension source declaration is invalid")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool):
            raise NativePackageError("Native extension source declaration is invalid")
        if byte_count < 0:
            raise NativePackageError("Native extension source declaration is invalid")
        try:
            snapshot_path = (snapshot_root / snapshot).resolve(strict=True)
            snapshot_path.relative_to(snapshot_root)
            content = snapshot_path.read_bytes()
        except (OSError, ValueError) as error:
            raise NativePackageError("Native extension source snapshot is unavailable") from error
        if len(content) != byte_count:
            raise NativePackageError(
                "Native extension source snapshot differs from its declaration"
            )
        if hashlib.sha256(content).hexdigest() != digest:
            raise NativePackageError(
                "Native extension source snapshot differs from its declaration"
            )
        return cls(Path(path), content)

    def is_installed(self) -> bool:
        try:
            info = self.destination.lstat()
            return (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and self.destination.read_bytes() == self.content
            )
        except (FileNotFoundError, PermissionError, IsADirectoryError):
            return False

    def stage(self) -> Path:
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=self.destination.parent,
            prefix=f".{self.destination.name}.native-source-",
        )
        temporary = Path(name)
        complete = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(self.content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            if temporary.stat().st_nlink != 1:
                raise NativePackageError("Native extension source staging file is shared")
            complete = True
            return temporary
        finally:
            if not complete:
                temporary.unlink(missing_ok=True)

    def verify_installed(self) -> None:
        info = self.destination.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise NativePackageError("Installed native extension source could not be verified")
        if info.st_nlink != 1 or info.st_size != len(self.content):
            raise NativePackageError("Installed native extension source could not be verified")
        if self.destination.read_bytes() != self.content:
            raise NativePackageError("Installed native extension source could not be verified")


def install_declared_sources(root: Path, snapshot_root: Path) -> None:
    """Install the source snapshots committed by a verified native package."""
    try:
        manifest = json.loads((root / "dist/agent-comms-imports.json").read_text())
        snapshot_root = snapshot_root.resolve(strict=True)
    except (OSError, json.JSONDecodeError) as error:
        raise NativePackageError("Native extension source manifest is unavailable") from error
    declarations = manifest.get("extensionEntries")
    if not isinstance(declarations, list):
        raise NativePackageError("Native extension source declarations are invalid")

    sources: list[NativeSource] = []
    destinations: set[Path] = set()
    for declaration in declarations:
        if not isinstance(declaration, dict):
            raise NativePackageError("Native extension source declaration is invalid")
        values = declaration.get("sources", [])
        if not isinstance(values, list):
            raise NativePackageError("Native extension source declaration is invalid")
        for value in values:
            source = NativeSource.declared(value, snapshot_root)
            if source.destination in destinations:
                raise NativePackageError("Native extension source destination is declared twice")
            destinations.add(source.destination)
            sources.append(source)

    replacements: list[tuple[Path, NativeSource]] = []
    try:
        for source in sources:
            if not source.is_installed():
                replacements.append((source.stage(), source))
        for temporary, source in replacements:
            os.replace(temporary, source.destination)
        for source in sources:
            source.verify_installed()
        for parent in {source.destination.parent for source in sources}:
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except OSError as error:
        raise NativePackageError("Native extension source could not be installed") from error
    finally:
        for temporary, _ in replacements:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class VerifiedNativePackage:
    """The immutable artifact acquired for one process; never a readiness proof."""

    path: Path

    def __post_init__(self):
        pins = [
            line[len(TREE_PREFIX) :]
            for line in MANIFEST.read_text().splitlines()
            if line.startswith(TREE_PREFIX)
        ]
        if len(pins) != 1 or len(pins[0]) != 64 or any(c not in "0123456789abcdef" for c in pins[0]):
            raise NativePackageError("Pinned native package tree commitment unavailable")
        if package_tree_digest(self.path) != pins[0]:
            raise NativePackageError("Native package tree differs from pinned artifact")

    @property
    def cli(self) -> Path:
        return self.path / "dist/cli.js"

def verify_native_package(root: Path) -> VerifiedNativePackage:
    """Verify the entire copied package against the repository-owned commitment."""
    return VerifiedNativePackage(root)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--share-resources":
        print(json.dumps(share_native_resources(Path(sys.argv[2]), Path(sys.argv[3]))), file=sys.stderr)
    elif len(sys.argv) == 4 and sys.argv[1] == "--install-sources":
        install_declared_sources(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--digest":
        print(package_tree_digest(Path(sys.argv[2])))
    elif len(sys.argv) == 3 and sys.argv[1] == "--write-manifest":
        write_native_manifest(Path(sys.argv[2]))
    elif len(sys.argv) == 2:
        verify_native_package(Path(sys.argv[1]))
    else:
        raise SystemExit(
            "Usage: native_package.py [--digest|--write-manifest] PACKAGE_DIR | "
            "--share-resources NEW_PACKAGE DEPLOYMENTS | "
            "--install-sources PACKAGE_DIR SNAPSHOT_ROOT"
        )
