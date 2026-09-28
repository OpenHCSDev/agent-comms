#!/usr/bin/env python3
"""Bind optional native compaction fileOps/usage to exact commit-ID reconciliation.

Apply only after the pinned writer failure-state patch, in a disposable package.
Older two-field native fixture markers remain readable; the production Python
bridge always supplies and requires metadataDigest for new commits.
"""

# ruff: noqa: E501  # Exact pinned JS anchors and embedded source.

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE_SHA = "10ac30c15dd1b47b86fef4121c01d4b52b4a114cb9fcba6a909c04a3f5f88a7d"
HELPERS = r"""
// Cross-language v1 metadata encoding: UTF-8 path bytes and IEEE-754 cost
// bytes are hex, counters are safe integers, optional fields have fixed slots.
// No JS/Python floating-point JSON formatting or object insertion order enters
// the digest. The marker contains only the hash, never redundant metadata.
function acMetadataDetails(details) {
    if (details == null) return null;
    const keys = Object.keys(details).filter(key => key !== "agentCommsCommit").sort();
    if (keys.length === 0) return null;
    if (keys.join(",") !== "modifiedFiles,readFiles" ||
        [details.readFiles, details.modifiedFiles].some(paths =>
            !Array.isArray(paths) || paths.some(path =>
                typeof path !== "string" || !path || !path.isWellFormed() ||
                Buffer.byteLength(path, "utf8") > 4096 || path.includes("\0"))))
        throw new Error("Invalid native compaction metadata details");
    return details;
}
function acMetadataDigest(details, usage) {
    const operations = acMetadataDetails(details);
    const paths = operations === null ? null :
        [operations.readFiles, operations.modifiedFiles].map(values =>
            values.map(value => Buffer.from(value, "utf8").toString("hex")));
    let metrics = null;
    if (usage != null) {
        const counters = ["input", "output", "cacheRead", "cacheWrite", "totalTokens"];
        const costs = ["input", "output", "cacheRead", "cacheWrite", "total"];
        const safe = value => Number.isSafeInteger(value) && value >= 0;
        const price = value => typeof value === "number" && Number.isFinite(value) &&
            value >= 0 && value <= Number.MAX_SAFE_INTEGER;
        if (typeof usage !== "object" || Array.isArray(usage) ||
            Object.keys(usage).some(key => ![...counters, "reasoning", "cacheWrite1h", "cost"].includes(key)) ||
            counters.some(key => !safe(usage[key])) ||
            ["reasoning", "cacheWrite1h"].some(key => usage[key] !== undefined && !safe(usage[key])) ||
            !usage.cost || typeof usage.cost !== "object" || Array.isArray(usage.cost) ||
            Object.keys(usage.cost).sort().join(",") !== costs.slice().sort().join(",") ||
            costs.some(key => !price(usage.cost[key])))
            throw new Error("Invalid native compaction metadata usage");
        const doubleHex = number => {
            const bytes = Buffer.alloc(8);
            bytes.writeDoubleBE(number);
            return bytes.toString("hex");
        };
        metrics = [...counters.map(key => usage[key]), usage.reasoning ?? null,
            usage.cacheWrite1h ?? null, ...costs.map(key => doubleHex(usage.cost[key]))];
    }
    return createHash("sha256").update("agent-comms-metadata-v1\n")
        .update(JSON.stringify([paths, metrics])).digest("hex");
}
"""


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise SystemExit(f"Pinned native metadata anchor changed: {old[:80]!r}")
    return source.replace(old, new, 1)


def main(path: Path) -> None:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != BASE_SHA:
        raise SystemExit("Native metadata patch requires exact failed-manager successor")
    source = raw.decode()
    source = replace_once(
        source,
        "function acValidateCommit(commit) {",
        HELPERS + "\nfunction acValidateCommit(commit) {",
    )
    source = replace_once(
        source,
        'Object.keys(commit).some(key => !["commitId", "payloadDigest"].includes(key)))',
        'Object.keys(commit).some(key => !["commitId", "payloadDigest", "metadataDigest"].includes(key)) ||\n'
        "        (commit.metadataDigest !== undefined && !/^[0-9a-f]{64}$/.test(commit.metadataDigest)))",
    )
    source = replace_once(
        source,
        "entry.details.agentCommsCommit.payloadDigest !== commit.payloadDigest ||",
        "entry.details.agentCommsCommit.payloadDigest !== commit.payloadDigest ||\n"
        "                    (commit.metadataDigest !== undefined &&\n"
        "                        (entry.details.agentCommsCommit.metadataDigest !== commit.metadataDigest ||\n"
        "                         acMetadataDigest(entry.details, entry.usage) !== commit.metadataDigest)) ||",
    )
    source = replace_once(
        source,
        'return { status: "committed", entryId: entry.id, revision,\n                    leafId: disk.at(-1).id };',
        'return { status: "committed", entryId: entry.id, revision,\n'
        "                    leafId: disk.at(-1).id,\n"
        "                    ...(commit.metadataDigest !== undefined ?\n"
        "                        { metadataDigest: entry.details.agentCommsCommit.metadataDigest } : {}) };",
    )
    source = replace_once(
        source,
        'throw new Error("Native commit payload mismatch; no replay");',
        'throw new Error("Native commit payload mismatch; no replay");\n'
        "                if (commit.metadataDigest !== undefined &&\n"
        "                    acMetadataDigest(details, usage) !== commit.metadataDigest)\n"
        '                    throw new Error("Native commit metadata mismatch; no replay");',
    )
    path.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
