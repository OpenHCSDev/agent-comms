#!/usr/bin/env python3
"""Attach original local write evidence to the same successful SDK tool result."""
from pathlib import Path
import sys


def replace_once(path, before, after):
    source = path.read_text()
    if source.count(before) != 1:
        raise ValueError(f"Original SDK file operation changed: {path}")
    path.write_text(source.replace(before, after, 1))


def main(package):
    tools = Path(package) / "dist/core/tools"
    (tools / "agent-comms-file-artifact.js").write_bytes(
        Path(__file__).with_name("native-file-artifact.mjs").read_bytes())
    for name in ("write", "edit"):
        path = tools / f"{name}.js"
        imports = "mkdir as fsMkdir" if name == "write" else "access as fsAccess, readFile as fsReadFile"
        replace_once(path,
            f'import {{ {imports}, writeFile as fsWriteFile }} from "fs/promises";',
            f'import {{ {imports} }} from "fs/promises";')
        replace_once(path,
            '    writeFile: (path, content) => fsWriteFile(path, content, "utf-8"),',
            '    writeFile: (path, content) => CompletedFileMutation.write(path, content),')
        content = "content" if name == "write" else "finalContent"
        replace_once(path, f"                await ops.writeFile(absolutePath, {content});",
            f"                const mutation = await ops.writeFile(absolutePath, {content});")
        original = "undefined" if name == "write" else "{ diff: diffResult.diff, patch, firstChangedLine: diffResult.firstChangedLine }"
        replace_once(path, f"                    details: {original},",
            f"                    details: CompletedFileMutation.resultDetails(mutation, {original}),")
        path.write_text('import { CompletedFileMutation } from "./agent-comms-file-artifact.js";\n' + path.read_text())


if __name__ == "__main__":
    main(sys.argv[1])
