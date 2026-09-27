#!/usr/bin/env python3
"""Pinned Pi RPC readiness patch used by normal native preparation.

The strict dry-run response does not resolve auth or authorize a summary/write.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE_SHA = "bd6dfca7b14cad4023c5ab56a7fc91bef3db9670c96b6ad7625df16353b42e5a"


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise SystemExit(f"Native RPC anchor changed: {old[:80]!r}")
    return source.replace(old, new, 1)


def main(path: Path) -> None:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != BASE_SHA:
        raise SystemExit("Native readiness requires exact pinned RPC bytes")
    helper = Path(__file__).with_name("native-compaction-readiness.mjs").read_text()
    source = raw.decode()
    source = replace_once(
        source,
        'import * as crypto from "node:crypto";',
        'import * as crypto from "node:crypto";\n'
        'import { prepareCompaction } from "../../core/compaction/index.js";',
    )
    source = replace_once(source, "export async function runRpcMode(runtimeHost) {", helper + "\nexport async function runRpcMode(runtimeHost) {")
    source = replace_once(source, "    // Handle a single command\n    const handleCommand", "    // Synchronous admission observes async RPC mutations; never changes native state.\n    let acOtherCommandInFlight = 0;\n    // Handle a single command\n    const handleCommand")
    source = replace_once(source, '''                void session
                    .prompt(command.message, {''', '''                // `prompt` is fire-and-forget in RPC. Its preflight may still be
                // awaiting extensions/auth before isStreaming becomes true.
                // Hold the conflict fence until that entire promise settles.
                acOtherCommandInFlight++;
                void session
                    .prompt(command.message, {''')
    source = replace_once(source, '''                    .catch((e) => {
                    if (!preflightSucceeded) {
                        output(error(id, "prompt", e.message));
                    }
                });''', '''                    .catch((e) => {
                    if (!preflightSucceeded) {
                        output(error(id, "prompt", e.message));
                    }
                })
                    .finally(() => { acOtherCommandInFlight--; });''')
    source = replace_once(source, '            case "get_state": {', '''            case "agent_comms_prepare_compaction": {
                if (!acValidReadiness(command))
                    return error(id, command.type, "Invalid v1 dry-run readiness request");
                return success(id, command.type,
                    acPrepareReadiness(command, session, acOtherCommandInFlight !== 0));
            }
            case "get_state": {''')
    source = replace_once(source, '''        const command = parsed;
        try {
            const response = await handleCommand(command);''', '''        const command = parsed;
        const acCountCommand = command?.type !== "agent_comms_prepare_compaction" &&
            command?.type !== "get_state";
        if (acCountCommand) acOtherCommandInFlight++;
        try {
            const response = await handleCommand(command);''')
    source = replace_once(source, '''            output(error(command.id, command.type, commandError instanceof Error ? commandError.message : String(commandError)));
            await waitForRawStdoutBackpressure();
        }
    };''', '''            output(error(command?.id, command?.type ?? "parse", commandError instanceof Error ? commandError.message : String(commandError)));
            await waitForRawStdoutBackpressure();
        }
        finally {
            if (acCountCommand) acOtherCommandInFlight--;
        }
    };''')
    path.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
