"""The source inventory and pinned identity of a native package build."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

INPUTS_PREFIX = "# agent-comms-native-inputs-v1 "


@dataclass(frozen=True)
class NativeBuildInputs:
    root: Path
    SCRIPTS = "stack/test-native-*"

    def scripts(self) -> tuple[Path, ...]:
        """Stack scripts that check a built package; they are no input to building it."""
        return tuple(sorted(self.root.glob(self.SCRIPTS)))

    def paths(self) -> tuple[Path, ...]:
        """Inventory source families, never a second roster of patch filenames."""
        families = (
            ("stack", {"", ".py", ".mjs", ".ts", ".patch", ".json", ".sha256"}),
            ("experiments/pi-context-proof", {".sh", ".patch"}),
            ("extensions", {".ts", ".mjs", ".js", ".json", ".md"}),
            ("src/agent_comms", {".mjs"}),
            ("src/agent_comms/instructions", {".md"}),
        )
        paths: set[Path] = {
            path
            for name in ("native_build.py", "native_build_inputs.py", "native_package.py")
            if (path := self.root / "src/agent_comms" / name).is_file()
        }
        for directory, suffixes in families:
            for path in (self.root / directory).rglob("*"):
                relative = path.relative_to(self.root)
                if any(
                    part.startswith(".") or part in {"node_modules", "__pycache__"}
                    for part in relative.parts
                ):
                    continue
                if path.is_file() and path.suffix in suffixes:
                    paths.add(path)
        return tuple(sorted(paths - {self.root / "stack/pi-native.sha256", *self.scripts()}))

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

    def require_pinned(self, manifest: Path) -> str:
        source = self.digest(manifest)
        if INPUTS_PREFIX + source not in manifest.read_text().splitlines():
            raise ValueError("Native build inputs changed; rebuild with --update-pins")
        return hashlib.sha256(source.encode() + manifest.read_bytes()).hexdigest()[:16]
