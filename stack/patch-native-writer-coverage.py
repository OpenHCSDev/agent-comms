#!/usr/bin/env python3
"""Close audited native loader/fork/snapshot gaps on the disposable journal artifact.

Not deployed until canonical preparation/provenance integration is reviewed.
Current v3 sessions only: malformed/partial/legacy persisted files are refused,
never silently repaired or migrated during a read.
"""

# ruff: noqa: E501  # Exact pinned/generated JS anchors.
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE_SHA = "8ec0b8f1b62ee6abe3ba3c98e2f64b1efea549b7e27f561fad2516f955b7c49c"
LOAD = """export function loadEntriesFromFile(filePath) {
    const resolvedFilePath = normalizePath(filePath);
    if (!existsSync(resolvedFilePath)) return [];
    // Reads never repair a tail, truncate a file, or migrate legacy data.
    return acReadStrictSession(resolvedFilePath);
}
"""
SYNC = """function acSyncParents(directory) {
    for (let current = directory; ; current = dirname(current)) {
        const fd = openSync(current, "r");
        try { fsyncSync(fd); } finally { closeSync(fd); }
        if (dirname(current) === current) break;
    }
}
"""
ASSERT = """    _assertLoadedRevision() {
        if (this.persist && pr48DiskRevision(this.sessionFile) !== this._pr48LoadedRevision)
            throw new Error("Native session changed during load; no replay");
    }
"""
FORK_BEFORE = """        writeFileSync(newSessionFile, `${JSON.stringify(newHeader)}\\n`, { flag: "wx", mode: 0o600 });
        // Copy all non-header entries from source
        for (const entry of sourceEntries) {
            if (entry.type !== "session") {
                appendFileSync(newSessionFile, `${JSON.stringify(entry)}\\n`);
            }
        }
        return new SessionManager(resolvedTargetCwd, dir, newSessionFile, true);
    }
"""
FORK_AFTER = """        pr48WriterLock(newSessionFile, () => {
            // Never adopt an existing destination as permission to overwrite it.
            let fd;
            try {
                fd = openSync(newSessionFile, "wx", 0o600);
                writeFileSync(fd, `${JSON.stringify(newHeader)}\\n`);
                for (const entry of sourceEntries) {
                    if (entry.type !== "session")
                        writeFileSync(fd, `${JSON.stringify(entry)}\\n`);
                }
                fsyncSync(fd);
                acSyncParents(dirname(newSessionFile));
            } catch {
                throw new Error("Native session fork outcome unknown; no replay");
            } finally {
                if (fd !== undefined) closeSync(fd);
            }
        });
        return new SessionManager(resolvedTargetCwd, dir, newSessionFile, true);
        });
    }
"""


def replace_once(source: str, before: str, after: str) -> str:
    if source.count(before) != 1:
        raise SystemExit("Pinned native writer coverage anchor changed")
    return source.replace(before, after, 1)


def main(path: Path) -> None:
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != BASE_SHA:
        raise SystemExit("Native coverage patch requires pinned journal artifact")
    source = original.decode()
    start = source.index("export function loadEntriesFromFile(filePath) {")
    end = source.index("/**\n * Inspect a physical line", start)
    source = source[:start] + LOAD + source[end:]
    replacements = [
        (
            'function acReadStrictSession(file) {\n    const raw = readFileSync(file, "utf8");',
            "function acReadStrictSession(file) {\n    pr48DiskRevision(file);\n"
            '    const raw = new TextDecoder("utf-8", { fatal: true }).decode(readFileSync(file));\n'
            "    if (raw.length === 0) return [];",
        ),
        (
            """    for (const entry of entries) {
        if (!entry || typeof entry !== "object" || typeof entry.id !== "string" ||
            ids.has(entry.id)) throw new Error("Invalid native session; outcome unknown");
        ids.add(entry.id);
    }""",
            """    for (let index = 0; index < entries.length; index++) {
        const entry = entries[index];
        if (!entry || typeof entry !== "object" || typeof entry.id !== "string" ||
            ids.has(entry.id)) throw new Error("Invalid native session; outcome unknown");
        if (index > 0 && (typeof entry.type !== "string" || entry.type === "session" ||
            entry.parentId !== null && (typeof entry.parentId !== "string" ||
                !ids.has(entry.parentId) || entry.parentId === entries[0].id)))
            throw new Error("Invalid native session ancestry; outcome unknown");
        ids.add(entry.id);
    }""",
        ),
        (
            '    if (entries[0]?.type !== "session") throw new Error("Missing native session identity");',
            '    if (entries[0]?.type !== "session") throw new Error("Missing native session identity");\n'
            "    if (entries[0].version !== CURRENT_SESSION_VERSION)\n"
            '        throw new Error("Native session migration requires explicit recovery");',
        ),
        ("export class SessionManager {", SYNC + "\nexport class SessionManager {"),
        (
            """        this._pr48LoadedRevision = this.persist ? pr48DiskRevision(this.sessionFile) : null;
    }
    /** Switch to a different session file (used for resume and branching) */""",
            """        this._assertLoadedRevision();
    }
""" + ASSERT + """    /** Switch to a different session file (used for resume and branching) */""",
        ),
        (
            """    setSessionFile(sessionFile) {
        this._setSessionFile(sessionFile);
        this._pr48LoadedRevision = this.persist ? pr48DiskRevision(this.sessionFile) : null;
    }""",
            """    setSessionFile(sessionFile) {
        this._setSessionFile(sessionFile);
        this._assertLoadedRevision();
    }""",
        ),
        (
            "            const entries = preloadedFileEntries ?? loadEntriesFromFile(this.sessionFile);",
            "            // A preloaded array carries no disk revision; reread the actual file.\n"
            "            const entries = loadEntriesFromFile(this.sessionFile);\n"
            "            this._assertLoadedRevision();",
        ),
        (
            """                this.newSession();
                this.sessionFile = explicitPath;
                this._pr48LoadedRevision = pr48DiskRevision(explicitPath);""",
            """                const emptyRevision = this._pr48LoadedRevision;
                this.newSession();
                this.sessionFile = explicitPath;
                this._pr48LoadedRevision = emptyRevision;""",
        ),
        (
            """            this._pr48LoadedRevision = pr48DiskRevision(this.sessionFile);
        });
    }
    isPersisted()""",
            """            try { acSyncParents(dirname(this.sessionFile)); }
            catch { throw new Error("Native session rewrite outcome unknown; no replay"); }
            this._pr48LoadedRevision = pr48DiskRevision(this.sessionFile);
        });
    }
    isPersisted()""",
        ),
        (
            """    createBranchedSession(leafId) {
        const previousSessionFile = this.sessionFile;""",
            """    createBranchedSession(leafId) {
        const previousSessionFile = this.sessionFile;
        return pr48WriterLock(this.persist ? previousSessionFile : undefined, () => {
        this._assertLoadedRevision();""",
        ),
        (
            """            this.sessionFile = newSessionFile;
            this._pr48LoadedRevision = pr48DiskRevision(newSessionFile);""",
            """            this.sessionFile = newSessionFile;
            this._pr48LoadedRevision = null;""",
        ),
        (
            """        this._buildIndex();
        return undefined;
    }
    /**
     * Create a new session.""",
            """        this._buildIndex();
        return undefined;
        });
    }
    /**
     * Create a new session.""",
        ),
        (
            """    static forkFrom(sourcePath, targetCwd, sessionDir, options) {
        const resolvedSourcePath = resolvePath(sourcePath);""",
            """    static forkFrom(sourcePath, targetCwd, sessionDir, options) {
        const resolvedSourcePath = resolvePath(sourcePath);
        return pr48WriterLock(resolvedSourcePath, () => {""",
        ),
        (FORK_BEFORE, FORK_AFTER),
    ]
    for before, after in replacements:
        source = replace_once(source, before, after)
    path.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
