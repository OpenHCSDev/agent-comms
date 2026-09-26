#!/usr/bin/env python3
"""Successor native commit-ID/reconciliation patch, disposable packages only.

Apply AFTER the dormant --production writer patch. Not deployed by prepare-pi-native.
The native layer owns only file evidence, never registry/goal authority.
"""

# ruff: noqa: E501  # Exact generated/pinned JS source anchors.

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE_SHA = "536f29b64149b2c18a53667a7f8bba1bf243d0afba90aeac16b84bf132e3d760"
HELPERS = """
function acReadStrictSession(file) {
    const raw = readFileSync(file, "utf8");
    if (!raw.endsWith("\\n")) throw new Error("Incomplete native session; outcome unknown");
    const entries = raw.slice(0, -1).split("\\n").map(line => JSON.parse(line));
    const ids = new Set();
    for (const entry of entries) {
        if (!entry || typeof entry !== "object" || typeof entry.id !== "string" ||
            ids.has(entry.id)) throw new Error("Invalid native session; outcome unknown");
        ids.add(entry.id);
    }
    if (entries[0]?.type !== "session") throw new Error("Missing native session identity");
    return entries;
}
function acValidateCommit(commit) {
    if (!commit || typeof commit !== "object" ||
        !/^[0-9a-f]{32}$/.test(commit.commitId) ||
        !/^[0-9a-f]{64}$/.test(commit.payloadDigest) ||
        Object.keys(commit).some(key => !["commitId", "payloadDigest"].includes(key)))
        throw new Error("Invalid native compaction commit identity");
}
function acPayloadDigest(summary, firstKeptEntryId, tokensBefore) {
    return createHash("sha256").update(JSON.stringify([
        summary, firstKeptEntryId, tokensBefore,
    ])).digest("hex");
}
"""
METHOD = """    /** File evidence only. Caller must retain registry authority and never replay. */
    reconcileCompactionCommit(commit, witness) {
        acValidateCommit(commit);
        if (!this.persist || !this.sessionFile || witness?.sessionFile !== this.sessionFile ||
            typeof witness.revision !== "string" || witness.sessionId !== this.sessionId)
            throw new Error("Invalid native reconciliation witness");
        return pr48WriterLock(this.sessionFile, () => {
            const revision = pr48DiskRevision(this.sessionFile);
            const disk = acReadStrictSession(this.sessionFile);
            if (disk[0].id !== witness.sessionId)
                return { status: "unknown", reason: "session-replaced" };
            const matches = disk.filter(entry =>
                entry.details?.agentCommsCommit?.commitId === commit.commitId);
            if (matches.length === 1) {
                const entry = matches[0];
                if (entry.type !== "compaction" ||
                    entry.details.agentCommsCommit.payloadDigest !== commit.payloadDigest ||
                    acPayloadDigest(entry.summary, entry.firstKeptEntryId, entry.tokensBefore) !==
                        commit.payloadDigest || entry.parentId !== witness.leafId ||
                    entry.firstKeptEntryId !== witness.firstKeptEntryId)
                    return { status: "unknown", reason: "commit-mismatch" };
                // A visible entry after an uncertain append is not yet proof of
                // durability. Sync it while still holding the writer fence.
                const fd = openSync(this.sessionFile, "r");
                try { fsyncSync(fd); } finally { closeSync(fd); }
                return { status: "committed", entryId: entry.id, revision,
                    leafId: disk.at(-1).id };
            }
            if (matches.length === 0 && revision === witness.revision)
                return { status: "aborted-no-write", revision, leafId: disk.at(-1).id };
            return { status: "unknown", reason: "changed-or-duplicate" };
        });
    }
"""
CHECK = """            const commit = details?.agentCommsCommit;
            if (commit !== undefined) {
                acValidateCommit(commit);
                if (typeof summary !== "string" || !summary.isWellFormed() ||
                    !Number.isSafeInteger(tokensBefore) || tokensBefore < 0 ||
                    acPayloadDigest(summary, witness.firstKeptEntryId, tokensBefore) !==
                        commit.payloadDigest)
                    throw new Error("Native commit payload mismatch; no replay");
                if (disk.some(entry => entry.details?.agentCommsCommit?.commitId === commit.commitId))
                    throw new Error("Native commit ID already present; no replay");
            }
"""


def main(path: Path) -> None:
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != BASE_SHA:
        raise SystemExit("Native journal patch requires pinned production writer artifact")
    source = original.decode()
    replacements = [
        (
            'import { fstatSync, realpathSync, unlinkSync } from "fs";',
            'import { fstatSync, realpathSync, unlinkSync, readFileSync } from "fs";\n'
            'import { createHash } from "crypto";',
        ),
        ("export class SessionManager {", HELPERS + "\nexport class SessionManager {"),
        (
            "    /** Native-only observation; never a registry/goal/correction attestation. */",
            METHOD
            + "    /** Native-only observation; never a registry/goal/correction attestation. */",
        ),
        (
            "            const disk = loadEntriesFromFile(this.sessionFile);",
            "            const disk = acReadStrictSession(this.sessionFile);",
        ),
        (
            '            const entry = { type: "compaction", id: generateId(this.byId),',
            CHECK + '            const entry = { type: "compaction", id: generateId(this.byId),',
        ),
    ]
    for before, after in replacements:
        if source.count(before) != 1:
            raise SystemExit("Pinned native journal anchor changed")
        source = source.replace(before, after, 1)
    path.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
