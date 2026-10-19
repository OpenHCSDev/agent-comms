"""The stack's native build step; paths are configuration, never process variables."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .native_build_inputs import NativeBuildInputs
from .native_package import (
    PACKAGE_LAYOUT, NativePackageError, VerifiedNativePackage, install_declared_sources,
    pinned_package_in, share_native_resources, write_native_manifest,
)


@dataclass(frozen=True)
class NativeBuild(NativeBuildInputs):
    """One checkout's inputs, interpreter and package destination own the entire build."""

    python: Path
    directory: Path
    environment: dict[str, str]
    dependency_package: Path | None = None

    @property
    def stack(self) -> Path:
        return self.root / "stack"

    @property
    def manifest(self) -> Path:
        return self.stack / "pi-native.sha256"

    @property
    def package(self) -> Path:
        return pinned_package_in(self.directory, self.manifest)

    def run(self, *command: str | Path, cwd: Path | None = None, input: str | None = None) -> str:
        print("+", " ".join(map(str, command)), file=sys.stderr, flush=True)
        result = subprocess.run(
            tuple(map(str, command)), cwd=cwd, input=input, capture_output=True, text=True,
            env=VerifiedNativePackage.node_environment(self.environment), umask=0o077,
        )
        if result.returncode:
            raise NativePackageError(result.stderr.strip() or result.stdout.strip() or
                                     f"{command[0]} exited {result.returncode}")
        return result.stdout

    def prepare(self, *, update_pins: bool = False, publish: Path | None = None) -> Path:
        if not update_pins:
            self.require_pinned(self.manifest)
            if self.package.exists():
                VerifiedNativePackage(self.package, self.manifest)
                return self.publish(self.package, publish)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".native-build-", dir=self.directory) as scratch:
            work = Path(scratch)
            stock = self.stock(work)
            package = work / "stage" / PACKAGE_LAYOUT
            package.parent.mkdir(mode=0o700, parents=True)
            self.copy_stock(stock, package)
            self.patch(package, self.root / "experiments/pi-context-proof/pi-0.85.1-native-input.patch")
            # The prototype's transport edit is not part of the running stack.
            bedrock = Path("node_modules/@earendil-works/pi-ai/dist/api/bedrock-converse-stream.js")
            shutil.copyfile(stock / bedrock, package / bedrock)
            self.patch_stack(package)
            share_native_resources(package, self.directory)
            if update_pins:
                write_native_manifest(package, self.manifest)
            VerifiedNativePackage(package, self.manifest)
            for name in ("core/session-manager.js", "core/settings-manager.js", "core/compaction/compaction.js",
                         "core/agent-session.js", "modes/rpc/rpc-mode.js", "core/agent-session-services.js"):
                self.run("node", "--check", package / "dist" / name)
            self.run("node", "--check", package / bedrock)
            destination = self.package
            if destination.exists():
                VerifiedNativePackage(destination, self.manifest)
            else:
                (work / "stage").rename(destination.parents[2])
            return self.publish(destination, publish)

    def stock(self, work: Path) -> Path:
        prefix = "# agent-comms-native-stock-v1 "
        specs = [line.removeprefix(prefix) for line in self.manifest.read_text().splitlines()
                 if line.startswith(prefix)]
        if len(specs) != 1 or not specs[0].startswith("@earendil-works/pi-coding-agent@"):
            raise NativePackageError("Pinned stock Pi is unavailable")
        tarball = self.run("npm", "pack", "--ignore-scripts", "--silent", specs[0], cwd=work).strip()
        self.run("tar", "-xzf", work / tarball, "-C", work)
        stock = work / "package"
        original = (stock / "package.json").read_bytes()
        self.run("npm", "pkg", "delete", "devDependencies", cwd=stock)
        shutil.copyfile(stock / "npm-shrinkwrap.json", stock / "package-lock.json")
        self.run("npm", "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund",
                 "--loglevel=error", cwd=stock)
        (stock / "package-lock.json").unlink()
        (stock / "node_modules/.package-lock.json").unlink()
        (stock / "package.json").write_bytes(original)
        self.run("sha256sum", "--check", "--status", self.stack / "pi-native-stock.sha256", cwd=stock)
        return stock

    def copy_stock(self, stock: Path, package: Path) -> None:
        """Materialize internal file aliases without allowing a patch to escape the copy."""
        for index, path in enumerate(stock.rglob("*")):
            if index >= 30000:
                raise NativePackageError("Stock package inventory limit exceeded")
            if path.is_symlink():
                target = path.resolve(strict=True)
                if not target.is_relative_to(stock) or not target.is_file():
                    raise NativePackageError("Stock package contains external or directory alias")
        self.run("cp", "-aL", "--no-preserve=links", "--reflink=auto", stock, package)

    def patch(self, package: Path, source: Path) -> None:
        self.run("patch", "--batch", "--fuzz=0", "--no-backup-if-mismatch", "-p1", "-d", package,
                 input=source.read_text())

    def python_step(self, name: str, *arguments: str | Path) -> None:
        self.run(self.python, self.stack / name, *arguments)

    def patch_stack(self, package: Path) -> None:
        self.python_step("patch-native-steering.py", package)
        self.python_step("patch-native-model-config.py", package / "dist/core/agent-session-services.js")
        self.patch(package, self.stack / "native-session-storage.patch")
        for source, target in (("native-session-entry-store.mjs", "session-entry-store.js"),
                               ("native-session-entry-store.d.ts", "session-entry-store.d.ts"),
                               ("native-proof-journal.mjs", "native-proof-journal.js"),
                               ("native-proof-schema.mjs", "native-proof-schema.js")):
            shutil.copyfile(self.stack / source, package / "dist/core" / target)
        shutil.copyfile(self.root / "src/agent_comms/pi_project_bootstrap.mjs",
                        package / "dist/agent-comms-project-bootstrap.mjs")
        for name in ("brief", "wip"):
            self.python_step(f"patch-native-compaction-{name}.py", package / "dist/core/agent-session.js")
        for name in ("compaction-cap", "turn-context", "file-artifact"):
            self.python_step(f"patch-native-{name}.py", package)
        self.patch(package, self.stack / "native-request-progress-adapters.patch")
        self.python_step("patch-native-request-observation.py", package)
        dependency = () if self.dependency_package is None else (self.dependency_package,)
        self.python_step("prepare-native-import-boundary.py", package, *dependency)

    def publish(self, package: Path, directory: Path | None) -> Path:
        if self.directory != self.stack:
            shutil.copyfile(self.manifest, self.directory / self.manifest.name)
        if directory is None:
            return package
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = directory / package.parents[2].name.replace(".pi-native-", "native-current-")
        installed = destination / PACKAGE_LAYOUT
        if destination.exists():
            VerifiedNativePackage(installed, self.manifest)
            install_declared_sources(installed, self.root)
        else:
            with tempfile.TemporaryDirectory(prefix=".native-current-", dir=directory) as scratch:
                stage = Path(scratch) / "deployment"
                target = stage / PACKAGE_LAYOUT
                target.parent.mkdir(mode=0o700, parents=True)
                self.run("cp", "-a", "--reflink=auto", package, target)
                VerifiedNativePackage(target, self.manifest)
                install_declared_sources(target, self.root)
                stage.rename(destination)
        return installed
