#!/usr/bin/env python3
"""Poison a native manager after a synchronous mutation/transition exception.

Apply only to a NEW disposable 41a writer-coverage artifact. No automatic reset,
repair or retry: recovery constructs a fresh manager from validated disk bytes.
"""

# ruff: noqa: E501  # Exact/generated JS lines.

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

BASE_SHA = "41a94b3777ac0ec322f649e3e234836893b8de86085f55a927ce29216205c28f"
MUTATIONS = (
    "_assertLoadedRevision",
    "setSessionFile",
    "_setSessionFile",
    "newSession",
    "_loadEntries",
    "_buildIndex",
    "_rewriteFile",
    "_persist",
    "_appendEntry",
    "flushInputDurably",
    "appendMessage",
    "appendThinkingLevelChange",
    "appendModelChange",
    "reconcileCompactionCommit",
    "appendCompactionIfCurrent",
    "appendCompaction",
    "appendCustomEntry",
    "appendSessionInfo",
    "appendCustomMessageEntry",
    "appendLabelChange",
    "branch",
    "resetLeaf",
    "branchWithSummary",
    "createBranchedSession",
)
READS = (
    "assertNativeInputSafe",
    "getTrackedInput",
    "captureCompactionWitness",
    "getSessionName",
    "getLeafEntry",
    "getEntry",
    "getChildren",
    "getLabel",
    "getBranch",
    "buildContextEntries",
    "buildSessionContext",
    "getHeader",
    "getEntries",
    "getTree",
)
DIAGNOSTICS = (
    "constructor",
    "isPersisted",
    "getCwd",
    "getSessionDir",
    "usesDefaultSessionDir",
    "getSessionId",
    "getSessionFile",
    "getLeafId",
    "nativeInputProofAvailable",
)
PRIVATE_GUARD = """export class SessionManager {
    // Irreversible for this instance. Never reset by newSession or setSessionFile.
    #acUnusable = false;
    #acAssertUsable() {
        if (this.#acUnusable)
            throw new Error("Native session manager unusable after failed mutation; reopen validated disk, never replay");
    }
    #acMutation(action) {
        this.#acAssertUsable();
        try {
            const result = action();
            this.#acAssertUsable();
            return result;
        } catch (error) {
            this.#acUnusable = true;
            throw error;
        }
    }
"""


def main(path: Path) -> None:
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != BASE_SHA:
        raise SystemExit("Failure-state patch requires exact writer-coverage artifact")
    source = original.decode()
    methods = set(re.findall(r"^    ([a-zA-Z_][a-zA-Z_0-9]*)\([^\n]*\) \{$", source, re.M))
    if methods != set(MUTATIONS + READS + DIAGNOSTICS):
        raise SystemExit(
            f"Native manager method inventory changed: {methods ^ set(MUTATIONS + READS + DIAGNOSTICS)}"
        )
    if source.count("export class SessionManager {\n") != 1:
        raise SystemExit("Native manager declaration changed")
    source = source.replace("export class SessionManager {\n", PRIVATE_GUARD, 1)
    for name in MUTATIONS + READS:
        pattern = re.compile(rf"(^    {name}\([^\n]*\) \{{\n)(.*?)(^    \}}$)", re.M | re.S)
        if len(pattern.findall(source)) != 1:
            raise SystemExit(f"Native manager method boundary changed: {name}")

        def wrap(match: re.Match[str], *, mutation: bool = name in MUTATIONS) -> str:
            opening, body, closing = match.groups()
            if mutation:
                # Preserve literal contents/indentation in the pinned original body.
                return (
                    opening
                    + "        return this.#acMutation(() => {\n"
                    + body
                    + "        });\n"
                    + closing
                )
            return opening + "        this.#acAssertUsable();\n" + body + closing

        source = pattern.sub(wrap, source, count=1)
    path.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
