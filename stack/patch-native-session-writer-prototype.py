#!/usr/bin/env python3
"""LOCAL-ONLY experiment: guarded native compaction append in a disposable pinned Pi copy.

Not wired into prepare-pi-native, the installed package, ACP, or PR48 runtime.
The caller-controlled ownerRequired switch now requires a Python-issued
OwnerCompactionAttestation (blocker-1 registry recheck output) bound to the
witness fence; JS cannot recheck the registry after handoff, so Python must
re-attest at commit time. This is NOT yet a full live bridge and grants no
send/commit coordination.
PR48_PROBE_FAIL_AFTER_WRITE is a test fault hook.
"""

# ruff: noqa: E501  # Exact pinned JS anchors and generated source lines.

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE_SHA = "dd75fef58eaa5458a91cff9fe1cf70556ebc720afd98720eae3dcb6572cb63ca"
IMPORT = (
    "import { appendFileSync, closeSync, createReadStream, existsSync, fsyncSync, "
    "lstatSync, mkdirSync, openSync, readdirSync, readSync, statSync, "
    'writeFileSync, } from "fs";'
)
EXTRA_IMPORT = (
    'import { fstatSync, realpathSync, unlinkSync } from "fs";\nimport { basename } from "path";'
)
ANCHOR = "export class SessionManager {\n"
LOCK_HELPERS = """// PR48 local-only prototype: no automatic stale-lock stealing after a crash.
function pr48DiskRevision(file) {
    if (!file || !existsSync(file)) return null;
    const s = lstatSync(file, { bigint: true });
    if (!s.isFile() || s.nlink !== 1n || s.size > 256n * 1024n * 1024n)
        throw new Error("Native session file is not a bounded regular file");
    return [s.dev, s.ino, s.size, s.mtimeNs, s.ctimeNs].map(String).join(":");
}
function pr48WriterLock(file, write) {
    if (!file) return write();
    const canonical = existsSync(file) ? realpathSync(file) :
        join(realpathSync(dirname(file)), basename(file));
    const lock = `${canonical}.pr48-writer.lock`;
    let fd;
    try { fd = openSync(lock, "wx", 0o600); }
    catch { throw new Error("Native session writer lock unavailable; no replay"); }
    try {
        writeFileSync(fd, `${process.pid}\\n`);
        fsyncSync(fd);
        return write();
    } finally {
        try { closeSync(fd); } finally { unlinkSync(lock); }
    }
}
"""
CLASS_FIELDS = "    leafId = null;\n"
CONSTRUCTOR_END = """        else {
            this.newSession(newSessionOptions);
        }
    }
    /** Switch to a different session file (used for resume and branching) */"""
CONSTRUCTOR_REPLACEMENT = """        else {
            this.newSession(newSessionOptions);
        }
        this._pr48LoadedRevision = this.persist ? pr48DiskRevision(this.sessionFile) : null;
    }
    /** Switch to a different session file (used for resume and branching) */"""
OPEN_ANCHOR = """    _setSessionFile(sessionFile, preloadedFileEntries) {
        this.sessionFile = resolvePath(sessionFile);
"""
OPEN_REPLACEMENT = """    _setSessionFile(sessionFile, preloadedFileEntries) {
        this.sessionFile = resolvePath(sessionFile);
        this._pr48LoadedRevision = pr48DiskRevision(this.sessionFile);
"""
EMPTY_REWRITE_ANCHOR = """                this.newSession();
                this.sessionFile = explicitPath;
                this._rewriteFile();
"""
EMPTY_REWRITE_NEW = """                this.newSession();
                this.sessionFile = explicitPath;
                this._pr48LoadedRevision = pr48DiskRevision(explicitPath);
                this._rewriteFile();
"""
NEW_SESSION_ANCHOR = """            this.sessionFile = join(this.getSessionDir(), `${fileTimestamp}_${this.sessionId}.jsonl`);
        }
        return this.sessionFile;
"""
NEW_SESSION_NEW = """            this.sessionFile = join(this.getSessionDir(), `${fileTimestamp}_${this.sessionId}.jsonl`);
        }
        this._pr48LoadedRevision = null;
        return this.sessionFile;
"""
BRANCH_ANCHOR = """            this.sessionFile = newSessionFile;
            this._buildIndex();
"""
BRANCH_NEW = """            this.sessionFile = newSessionFile;
            this._pr48LoadedRevision = pr48DiskRevision(newSessionFile);
            this._buildIndex();
"""
SWITCH_ANCHOR = """    setSessionFile(sessionFile) {
        this._setSessionFile(sessionFile);
    }
"""
SWITCH_REPLACEMENT = """    setSessionFile(sessionFile) {
        this._setSessionFile(sessionFile);
        this._pr48LoadedRevision = this.persist ? pr48DiskRevision(this.sessionFile) : null;
    }
"""
FLUSH_START = """    flushInputDurably(inputId) {
        this.assertNativeInputSafe();
"""
FLUSH_START_NEW = """    flushInputDurably(inputId) {
        this.assertNativeInputSafe();
        return pr48WriterLock(this.sessionFile, () => {
            if (pr48DiskRevision(this.sessionFile) !== this._pr48LoadedRevision)
                throw new Error("Native session writer changed; no replay");
"""
FLUSH_END = """        return entry.id;
    }
    /** Append a message as child of current leaf, then advance leaf. Returns entry id.
"""
FLUSH_END_NEW = """        this._pr48LoadedRevision = pr48DiskRevision(this.sessionFile);
        return entry.id;
        });
    }
    /** Append a message as child of current leaf, then advance leaf. Returns entry id.
"""
PERSIST_ANCHOR = """    _persist(entry) {
        if (!this.persist || !this.sessionFile)
            return;
        const hasAssistant = this.fileEntries.some((e) => e.type === "message" && e.message.role === "assistant");
"""
PERSIST_REPLACEMENT = """    _persist(entry) {
        if (!this.persist || !this.sessionFile)
            return;
        const prospective = this.fileEntries.concat(entry);
        const hasAssistant = prospective.some((e) => e.type === "message" && e.message.role === "assistant");
"""
PERSIST_LOOP = """            const fd = openSync(this.sessionFile, "wx", 0o600);
            try {
                for (const e of this.fileEntries) {
"""
PERSIST_LOOP_NEW = """            const fd = openSync(this.sessionFile, "wx", 0o600);
            try {
                for (const e of prospective) {
"""
REWRITE_ANCHOR = """    _rewriteFile() {
        if (!this.persist || !this.sessionFile)
            return;
        const fd = openSync(this.sessionFile, "w", 0o600);
        try {
            for (const entry of this.fileEntries) {
                writeFileSync(fd, `${JSON.stringify(entry)}\\n`);
            }
        }
        finally {
            closeSync(fd);
        }
    }
"""
REWRITE_NEW = """    _rewriteFile() {
        if (!this.persist || !this.sessionFile) return;
        return pr48WriterLock(this.sessionFile, () => {
            if (pr48DiskRevision(this.sessionFile) !== this._pr48LoadedRevision)
                throw new Error("Native session rewrite source changed; no replay");
            const fd = openSync(this.sessionFile, "w", 0o600);
            try {
                for (const entry of this.fileEntries) {
                    writeFileSync(fd, `${JSON.stringify(entry)}\\n`);
                }
                fsyncSync(fd);
            } catch { throw new Error("Native session rewrite outcome unknown; no replay"); }
            finally { closeSync(fd); }
            this._pr48LoadedRevision = pr48DiskRevision(this.sessionFile);
        });
    }
"""
APPEND_ANCHOR = """    _appendEntry(entry) {
        this.fileEntries.push(entry);
        this.byId.set(entry.id, entry);
        this.leafId = entry.id;
        this._persist(entry);
    }
"""
APPEND_REPLACEMENT = """    _appendEntry(entry) {
        return pr48WriterLock(this.persist ? this.sessionFile : undefined, () => {
            // Every native append uses the same per-session cross-process lock.
            // A stale in-memory manager must not attach a sibling after another
            // process advanced the file, even for non-compaction writes.
            if (this.persist && pr48DiskRevision(this.sessionFile) !== this._pr48LoadedRevision)
                throw new Error("Native session writer changed; no replay");
            try { this._persist(entry); }
            catch { throw new Error("Native session write outcome unknown; no replay"); }
            this.fileEntries.push(entry);
            this.byId.set(entry.id, entry);
            this.leafId = entry.id;
            this._pr48LoadedRevision = this.persist ? pr48DiskRevision(this.sessionFile) : null;
        });
    }
"""
COMPACTION_ANCHOR = (
    "    /** Append a compaction summary as child of current leaf, "
    "then advance leaf. Returns entry id. */\n"
)
CAS_METHODS = """    /** Native-only observation; never a registry/goal/correction attestation. */
    captureCompactionWitness(firstKeptEntryId) {
        if (!this.persist || !this.flushed || !this.sessionFile)
            throw new Error("Persisted native compaction witness required");
        if (!this.getBranch().some(entry => entry.id === firstKeptEntryId))
            throw new Error("First kept entry is not on the active branch");
        const revision = pr48DiskRevision(this.sessionFile);
        if (revision !== this._pr48LoadedRevision)
            throw new Error("Native session changed before witness capture");
        return Object.freeze({ sessionId: this.sessionId, sessionFile: this.sessionFile,
            leafId: this.leafId, firstKeptEntryId, revision });
    }
    /** Prototype only: owner attestation is Python-issued evidence, not self-asserted. */
    appendCompactionIfCurrent(witness, summary, tokensBefore, details, usage, options = {}) {
        // Blocker-2 bridge: `options.attestation` must be a Python
        // OwnerCompactionAttestation from ThreadRegistry.attest_owner_compaction.
        // JS binds it to this witness's session fence at handoff. JS cannot
        // recheck the registry after handoff; Python must re-attest at commit
        // time (blocker-1 recheck design). This check NEVER proves registry
        // currency by itself and never bypasses the writer CAS below.
        if (options.ownerRequired === true) {
            const denial = "Canonical Python owner commit attestation unavailable";
            const attestation = options.attestation;
            if (!attestation || typeof attestation !== "object" || Array.isArray(attestation))
                throw new Error(denial);
            const valid =
                typeof attestation.thread === "string" && attestation.thread.length > 0 &&
                Number.isInteger(attestation.owner_epoch) && attestation.owner_epoch >= 1 &&
                typeof attestation.turn_id === "string" && attestation.turn_id.length > 0 &&
                typeof attestation.goal_id === "string" && attestation.goal_id.length > 0 &&
                Number.isInteger(attestation.goal_revision) && attestation.goal_revision >= 0 &&
                Number.isInteger(attestation.correction_revision) &&
                attestation.correction_revision >= 0 &&
                attestation.session_file === witness.sessionFile &&
                attestation.session_leaf === witness.leafId &&
                attestation.session_revision === witness.revision &&
                (attestation.registry_revision === null ||
                    (Array.isArray(attestation.registry_revision) &&
                        attestation.registry_revision.length === 4 &&
                        attestation.registry_revision.every(Number.isInteger)));
            if (!valid) throw new Error(denial);
        }
        if (Object.keys(options).some(key => key !== "ownerRequired" && key !== "attestation") ||
            !this.persist || !this.flushed || !this.sessionFile ||
            !witness || witness.sessionId !== this.sessionId ||
            witness.sessionFile !== this.sessionFile || witness.leafId !== this.leafId ||
            typeof witness.revision !== "string" ||
            typeof witness.firstKeptEntryId !== "string")
            throw new Error("Invalid native compaction witness");
        return pr48WriterLock(this.sessionFile, () => {
            const revision = pr48DiskRevision(this.sessionFile);
            // Re-read the actual authoritative file *under the writer lock*;
            // an independent SessionManager may have appended since capture.
            const disk = loadEntriesFromFile(this.sessionFile);
            const diskHeader = disk.find(entry => entry.type === "session");
            const diskLeaf = disk.at(-1)?.id ?? null;
            const diskById = new Map(disk.filter(entry => entry.id).map(entry => [entry.id, entry]));
            let cursor = diskById.get(diskLeaf);
            let remaining = disk.length;
            while (cursor && cursor.id !== witness.firstKeptEntryId && remaining-- > 0)
                cursor = diskById.get(cursor.parentId);
            if (revision !== witness.revision || revision !== this._pr48LoadedRevision ||
                diskHeader?.id !== this.sessionId || diskLeaf !== witness.leafId ||
                cursor?.id !== witness.firstKeptEntryId ||
                !this.getBranch().some(entry => entry.id === witness.firstKeptEntryId))
                throw new Error("Native compaction source changed; no replay");
            const entry = { type: "compaction", id: generateId(this.byId),
                parentId: this.leafId, timestamp: new Date().toISOString(), summary,
                firstKeptEntryId: witness.firstKeptEntryId, tokensBefore, details, usage,
                fromHook: false };
            let fd;
            try {
                fd = openSync(this.sessionFile, "a", 0o600);
                const opened = fstatSync(fd, { bigint: true });
                if (`${opened.dev}:${opened.ino}` !== revision.split(":").slice(0, 2).join(":"))
                    throw new Error("Native session file replaced during commit");
                writeFileSync(fd, `${JSON.stringify(entry)}\\n`);
                if (process.env.PR48_PROBE_FAIL_AFTER_WRITE === "1")
                    throw new Error("Injected post-write uncertainty");
                fsyncSync(fd);
            } catch {
                // Bytes may have been sent to disk before an error. Never replay.
                throw new Error("Native compaction commit outcome unknown; no replay");
            } finally {
                if (fd !== undefined) closeSync(fd);
            }
            this.fileEntries.push(entry);
            this.byId.set(entry.id, entry);
            this.leafId = entry.id;
            this._pr48LoadedRevision = pr48DiskRevision(this.sessionFile);
            return entry.id;
        });
    }
"""


def replace_once(source: str, before: str, after: str) -> str:
    if source.count(before) != 1:
        raise SystemExit("Pinned native SessionManager anchor changed")
    return source.replace(before, after, 1)


def main(path: Path) -> None:
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != BASE_SHA:
        raise SystemExit("Native SessionManager source does not match pinned Pi")
    source = original.decode()
    for before, after in (
        (IMPORT, IMPORT + "\n" + EXTRA_IMPORT),
        (ANCHOR, LOCK_HELPERS + ANCHOR),
        (CLASS_FIELDS, CLASS_FIELDS + "    _pr48LoadedRevision = null;\n"),
        (CONSTRUCTOR_END, CONSTRUCTOR_REPLACEMENT),
        (OPEN_ANCHOR, OPEN_REPLACEMENT),
        (EMPTY_REWRITE_ANCHOR, EMPTY_REWRITE_NEW),
        (NEW_SESSION_ANCHOR, NEW_SESSION_NEW),
        (BRANCH_ANCHOR, BRANCH_NEW),
        (SWITCH_ANCHOR, SWITCH_REPLACEMENT),
        (REWRITE_ANCHOR, REWRITE_NEW),
        (FLUSH_START, FLUSH_START_NEW),
        (FLUSH_END, FLUSH_END_NEW),
        (PERSIST_ANCHOR, PERSIST_REPLACEMENT),
        (PERSIST_LOOP, PERSIST_LOOP_NEW),
        (APPEND_ANCHOR, APPEND_REPLACEMENT),
        (COMPACTION_ANCHOR, CAS_METHODS + COMPACTION_ANCHOR),
    ):
        source = replace_once(source, before, after)
    path.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
