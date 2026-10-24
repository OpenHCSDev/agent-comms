#!/usr/bin/env python3
"""Attach original local write evidence to the same successful SDK tool result."""
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SdkFileTool:
    """One Pi SDK file tool: its module, kept fs imports, written value and result details."""

    name: str
    imports: str
    content: str
    original: str


SDK_FILE_TOOLS = (
    SdkFileTool("write", "mkdir as fsMkdir", "content", "undefined"),
    SdkFileTool(
        "edit",
        "access as fsAccess, readFile as fsReadFile",
        "finalContent",
        "{ diff: diffResult.diff, patch, firstChangedLine: diffResult.firstChangedLine }",
    ),
)


def replace_once(path, before, after):
    source = path.read_text()
    if source.count(before) != 1:
        raise ValueError(f"Original SDK file operation changed: {path}")
    path.write_text(source.replace(before, after, 1))


def main(package):
    tools = Path(package) / "dist/core/tools"
    (tools / "agent-comms-file-artifact.js").write_bytes(
        Path(__file__).with_name("native-file-artifact.mjs").read_bytes())
    (tools / "native-file-artifact-schema.mjs").write_bytes(
        Path(__file__).with_name("native-file-artifact-schema.mjs").read_bytes())
    for tool in SDK_FILE_TOOLS:
        path = tools / f"{tool.name}.js"
        replace_once(path,
            f'import {{ {tool.imports}, writeFile as fsWriteFile }} from "fs/promises";',
            f'import {{ {tool.imports} }} from "fs/promises";')
        replace_once(path,
            '    writeFile: (path, content) => fsWriteFile(path, content, "utf-8"),',
            '    writeFile: (path, content) => CompletedFileMutation.write(path, content),')
        replace_once(path, f"                await ops.writeFile(absolutePath, {tool.content});",
            f"                const mutation = await ops.writeFile(absolutePath, {tool.content});")
        details = f"CompletedFileMutation.resultDetails(mutation, {tool.original})"
        replace_once(path, f"                    details: {tool.original},",
            f"                    details: {details},")
        path.write_text('import { CompletedFileMutation } from "./agent-comms-file-artifact.js";\n' + path.read_text())


if __name__ == "__main__":
    main(sys.argv[1])
