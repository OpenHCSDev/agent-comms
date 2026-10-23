"""The source inventory and pinned identity of a native package build."""

import hashlib
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

INPUTS_PREFIX = "# agent-comms-native-inputs-v1 "


def built_tree_pin(manifest: Path) -> str:
    """Which built Pi tree this manifest pins: its file and tree commitments. The inputs
    line records the recipe for ``require_pinned`` and never names the build, so a recipe
    edit that builds the same bytes keeps its pin and its prepared copy."""
    built = "".join(
        line
        for line in manifest.read_text().splitlines(keepends=True)
        if not line.startswith(INPUTS_PREFIX)
    )
    return hashlib.sha256(built.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class NativeBuildInputs:
    root: Path

    def paths(self) -> tuple[Path, ...]:
        """Inventory source families, never a second roster of patch filenames."""
        families = (
            ("stack", {"", ".py", ".mjs", ".ts", ".patch", ".json", ".sha256"}),
            ("experiments/pi-context-proof", {".sh", ".patch"}),
            ("extensions", {".ts", ".mjs", ".js", ".json", ".md"}),
            ("src/agent_comms", {".mjs"}),
            ("src/agent_comms/instructions", {".md"}),
        )
        paths: set[Path] = set()
        for directory, suffixes in families:
            for parent, directories, files in (self.root / directory).walk():
                directories[:] = [
                    name
                    for name in directories
                    if not name.startswith(".") and name not in {"node_modules", "__pycache__"}
                ]
                for name in files:
                    path = parent / name
                    if not name.startswith(".") and path.suffix in suffixes:
                        paths.add(path)
        return tuple(sorted(paths - {self.root / "stack/pi-native.sha256"}))

    def digest(self, manifest: Path) -> str:
        digest = hashlib.sha256()
        for path in self.paths():
            digest.update(str(path.relative_to(self.root)).encode() + b"\0")
            digest.update((path.stat().st_mode & 0o111).to_bytes(2, "big"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        for line in manifest.read_text().splitlines():
            if line.startswith("# agent-comms-native-stock-v1 "):
                digest.update(line.encode())
        return digest.hexdigest()

    def require_pinned(self, manifest: Path) -> None:
        if INPUTS_PREFIX + self.digest(manifest) not in manifest.read_text().splitlines():
            raise ValueError("Native build inputs changed; rebuild with --update-pins")

    def preparation_digest(
        self, manifest: Path, node: str, npm: str, dependency_package: str = ""
    ) -> str:
        """Hash the recipe, pin, toolchain and supplied MCP input, never our output."""
        dependency_digest = ""
        if dependency_package:
            dependency_digest = subprocess.check_output(
                [
                    str(self.root / "stack/bin/native-source"),
                    sys.executable,
                    "digest",
                    dependency_package,
                ],
                text=True,
            ).strip()
        digest = hashlib.sha256()
        for value in (
            self.digest(manifest),
            manifest.read_text(),
            node,
            npm,
            sys.version,
            sys.platform,
            platform.machine(),
            dependency_digest,
        ):
            digest.update(value.encode() + b"\0")
        return digest.hexdigest()


if __name__ == "__main__":
    inputs = NativeBuildInputs(Path(sys.argv[1]))
    manifest = inputs.root / "stack/pi-native.sha256"
    print(built_tree_pin(manifest), inputs.preparation_digest(manifest, *sys.argv[2:]))
