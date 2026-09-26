#!/usr/bin/env python3
"""Assemble the immutable import boundary in a NEW disposable native package."""

# ruff: noqa: E501  # Exact/generated JavaScript lines.

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LOADER_SHA = "a1393de916487a2c47107ac7239f3139dcdb938705f88ba1ea5a954b3c8bb483"


def patch_loader(path: Path) -> None:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != LOADER_SHA:
        raise SystemExit("Import boundary requires the exact upstream extension loader")
    text = raw.decode()
    text = text.replace(
        "Extension loader - loads TypeScript extension modules using jiti.",
        "Extension loader - native-only, deployment-manifest selected modules.",
    )
    for line in (
        'import { createRequire } from "node:module";\n',
        'import { fileURLToPath } from "node:url";\n',
        'import { createJiti } from "jiti/static";\n',
    ):
        assert text.count(line) == 1
        text = text.replace(line, "")
    text = text.replace(
        "import { CONFIG_DIR_NAME, getAgentDir, isBunBinary, isBundledNode } "
        'from "../../config.js";',
        'import { CONFIG_DIR_NAME, getAgentDir } from "../../config.js";\n'
        'import { loadApprovedExtension } from "../../agent-comms-import-fence.mjs";',
    )
    start = text.index("/** Modules available to extensions via virtualModules")
    end = text.index("let extensionCacheCwd;", start)
    text = text[:start] + text[end:]
    start = text.index("    const jiti = createJiti(import.meta.url, {")
    end = text.index("    const factory = module;", start)
    text = (
        text[:start]
        + "    const module = await loadApprovedExtension(extensionPath);\n"
        + text[end:]
    )
    # Keep static host imports: removing their provider-registration side effects
    # would be a separate runtime behavior change, not an import boundary fix.
    path.write_text(text)


def patch_package_manager(path: Path) -> None:
    raw = path.read_bytes()
    expected = "26aa8adf255f7b1ebf9b00732d2e9a1648e38b76a10eedcd443131e3708b6e88"
    if hashlib.sha256(raw).hexdigest() != expected:
        raise SystemExit("Import boundary requires the exact upstream package manager")
    text = raw.decode()
    text = 'import { assertApprovedPackage } from "../agent-comms-import-fence.mjs";\n' + text
    for method in ("install", "remove"):
        old = f"    async {method}(source, options) {{\n        const parsed = this.parseSource(source);"
        assert text.count(old) == 1
        text = text.replace(
            old,
            old
            + '\n        assertApprovedPackage(parsed.type === "local" ? this.resolvePath(parsed.path) : null);',
        )
    old = "            const parsed = this.parseSource(resolvedSource);"
    assert text.count(old) == 1
    text = text.replace(
        old,
        old
        + '\n            assertApprovedPackage(parsed.type === "local" ? this.resolvePathFromBase(parsed.path, this.getBaseDirForScope(resolvedScope)) : null);',
    )
    old = "    async updateConfiguredSources(sources) {"
    assert text.count(old) == 1
    text = text.replace(
        old,
        old + """
        for (const entry of sources) {
            const parsed = this.parseSource(entry.source);
            assertApprovedPackage(parsed.type === "local" ? this.resolvePathFromBase(parsed.path, this.getBaseDirForScope(entry.scope)) : null);
        }""",
    )
    path.write_text(text)


def materialize(source: Path, target: Path) -> None:
    source = source.resolve(strict=True)
    count = size = 0
    pending = [(source, 0)]
    while pending:
        directory, depth = pending.pop()
        with os.scandir(directory) as entries:
            for item in entries:
                count += 1
                if count > 30000 or depth > 32:
                    raise ValueError("MCP copy inventory limit exceeded")
                path = Path(item.path)
                actual = path.resolve(strict=True)
                if path.is_symlink() and (
                    not actual.is_relative_to(source) or not actual.is_file()
                ):
                    raise ValueError("MCP copy contains external or directory alias")
                if path.name == "session-manager.js":
                    raise ValueError("MCP dependencies contain another native session manager")
                if actual.is_file():
                    size += actual.stat().st_size
                elif actual.is_dir():
                    pending.append((path, depth + 1))
                else:
                    raise ValueError("MCP copy contains special file")
                if size > 512 * 1024 * 1024:
                    raise ValueError("MCP copy byte limit exceeded")
    subprocess.run(
        ["cp", "-aL", "--no-preserve=links", "--reflink=auto", str(source), str(target)], check=True
    )


def main(package: Path) -> None:
    os.umask(0o077)
    stack = Path(__file__).resolve().parent
    extension = stack.parent / "extensions/pi-mcp-client"
    destination = package / "agent-comms-extensions/pi-mcp-client"
    if destination.exists():
        raise SystemExit("Import-boundary destination already exists; never repair in place")
    patch_loader(package / "dist/core/extensions/loader.js")
    patch_package_manager(package / "dist/core/package-manager.js")
    environment = dict(os.environ)
    environment.pop("NODE_OPTIONS", None)
    environment.pop("NODE_PATH", None)
    environment.pop("NODE_COMPILE_CACHE", None)
    environment["NODE_DISABLE_COMPILE_CACHE"] = "1"
    with tempfile.TemporaryDirectory(prefix="pr95-mcp-deps-", dir="/var/tmp") as scratch:
        staging = Path(scratch)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(extension / name, staging / name)
        subprocess.run(
            [
                "npm",
                "ci",
                "--offline",
                "--ignore-scripts",
                "--omit=dev",
                "--omit=peer",
                "--no-audit",
                "--no-fund",
            ],
            cwd=staging,
            env=environment,
            check=True,
            stdout=sys.stderr,
            stderr=sys.stderr,
            timeout=120,
        )
        for name in ("index.mjs", "README.md"):
            shutil.copyfile(extension / name, staging / name)
        for name in ("src", "bin"):
            shutil.copytree(extension / name, staging / name, symlinks=True)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        materialize(staging, destination)
    for source, target in (
        ("native-import-fence.mjs", "agent-comms-import-fence.mjs"),
        ("native-import-manifest.json", "agent-comms-imports.json"),
        ("native-compaction-commit-child.mjs", "agent-comms-compaction-commit-child.mjs"),
    ):
        shutil.copyfile(stack / source, package / "dist" / target)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(strict=True))
