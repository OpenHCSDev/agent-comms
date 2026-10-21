/** Native context proof is one bus-owned transactional journal, not a cache.
 * Recovery reserves generations only; it never emits live input acceptance.
 * The original native session remains authoritative for input claims/UNKNOWN.
 */
import { DatabaseSync } from 'node:sqlite';
import { closeSync, existsSync, fsyncSync, linkSync, lstatSync, openSync, renameSync, unlinkSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { dirname } from 'node:path';
import { nativeProofSchema } from './native-proof-schema.js';
import { NativeFileUnavailable, PrivateNativeFile } from './native-private-file.js';

export class NativeProof {
    static fromPath(file, sessionId) {
        return file === undefined ? UnconfiguredNativeProof.shared : new NativeProofJournal(file, sessionId);
    }
    available(manager) {
        try { this.require(manager); return true; }
        catch (error) {
            if (error instanceof NativeFileUnavailable) return false;
            throw error;
        }
    }
}

class UnconfiguredNativeProof extends NativeProof {
    static shared = Object.freeze(new UnconfiguredNativeProof());
    recover() { return 0; }
    require() { throw new NativeFileUnavailable('Core has not selected native proof storage'); }
}

export class NativeProofJournal extends PrivateNativeFile(NativeProof) {
    constructor(file, sessionId) {
        super();
        this.file = file;
        this.sessionId = sessionId;
        Object.freeze(this);
    }
    require(manager) {
        if (manager.getSessionId() !== this.sessionId)
            throw new NativeFileUnavailable('Native proof selection belongs to another saved session');
        manager.assertNativeInputSafe();
        this.requirePrivateNativeFile();
        return this;
    }
    #open() {
        this.requirePrivateNativeFile();
        this.#finishPublication();
        const info = lstatSync(this.file);
        if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 ||
            info.uid !== process.getuid() || (info.mode & 0o077) !== 0)
            throw new Error('Native proof journal must be a private owner file');
        // Core reads this journal while turns run. A commit waits out such a
        // read, as Core's readers wait out a commit, instead of failing the turn.
        const db = new DatabaseSync(this.file, { timeout: 5000 });
        try {
            // DELETE has recovery work bounded by the current context transaction,
            // independent of total journal history. EXTRA syncs directory removal.
            db.exec('PRAGMA journal_mode=DELETE; PRAGMA synchronous=EXTRA; PRAGMA mmap_size=0;');
            this.#requireSchema(db);
            return db;
        } catch (error) {
            db.close();
            throw error;
        }
    }
    #syncDirectory() {
        const parent = openSync(dirname(this.file), 'r');
        try { fsyncSync(parent); } finally { closeSync(parent); }
    }
    #initialize() {
        // Prepare only the empty declared schema outside the authoritative path.
        // link() publishes it atomically WITHOUT replacing an existing journal.
        // The inode-derived staging name allows a crash between link/unlink to
        // finish just this publication, never accept arbitrary hardlink aliases.
        let temporary = `${this.file}.initializing-${randomUUID()}`;
        closeSync(openSync(temporary, 'wx', 0o600));
        try {
            const staged = `${this.file}.initializing-${lstatSync(temporary).ino}`;
            if (existsSync(staged)) throw new Error('Native proof staging identity already exists');
            renameSync(temporary, staged);
            temporary = staged;
            const db = new DatabaseSync(temporary);
            try {
                db.exec('PRAGMA journal_mode=DELETE; PRAGMA synchronous=EXTRA; BEGIN IMMEDIATE;');
                for (const ddl of Object.values(nativeProofSchema.objects)) db.exec(ddl);
                db.exec('COMMIT');
            } finally { db.close(); }
            const complete = openSync(temporary, 'r');
            try { fsyncSync(complete); } finally { closeSync(complete); }
            linkSync(temporary, this.file);
        } finally { unlinkSync(temporary); }
        this.#syncDirectory();
    }
    #finishPublication() {
        const info = lstatSync(this.file);
        if (info.nlink !== 2) return;
        const staged = `${this.file}.initializing-${info.ino}`;
        const alias = lstatSync(staged);
        if (!info.isFile() || !alias.isFile() || alias.dev !== info.dev || alias.ino !== info.ino ||
            info.uid !== process.getuid() || (info.mode & 0o777) !== 0o600)
            throw new Error('Native proof publication identity differs');
        unlinkSync(staged);
        this.#syncDirectory();
    }
    #requireSchema(db) {
        const objects = db.prepare("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL").all();
        if (objects.length !== Object.keys(nativeProofSchema.objects).length ||
            objects.some(row => nativeProofSchema.objects[row.name] !== row.sql))
            throw new Error('Native proof journal schema differs; offline conversion required');
        const session = db.prepare(nativeProofSchema.session).get();
        if (session && session.id !== this.sessionId)
            throw new Error('Native proof journal belongs to another session');
    }
    recover(manager) {
        this.require(manager);
        const entryStore = manager.entryStore;
        if (!existsSync(this.file)) return 0;
        const db = this.#open();
        try {
            // Validate the current context lineage, bounded by that context.
            // Historical native claims remain in SessionManager, never replayed.
            for (const row of db.prepare(nativeProofSchema.current).iterate()) {
                if (entryStore.trackedInputMetadata(row.input_id)?.id !== row.session_entry_id)
                    throw new Error('Native proof current source lineage differs');
            }
            return db.prepare(nativeProofSchema.head).get().generation;
        }
        finally { db.close(); }
    }
    commit(tracked, digest) {
        this.requirePrivateNativeFile();
        if (!existsSync(this.file)) this.#initialize();
        const db = this.#open();
        try {
            db.exec('BEGIN IMMEDIATE');
            const generation = db.prepare(nativeProofSchema.head).get().generation + 1;
            const committedAtMs = Date.now();
            const insert = db.prepare(nativeProofSchema.insert);
            // Each journal row is also the live context_committed event, unchanged.
            const records = tracked.map(({inputId, sessionEntryId}) => ({
                schema:2, type:'context_committed', sessionId:this.sessionId, inputId,
                sessionEntryId, requestGeneration:generation, llmContextDigest:digest, committedAtMs}));
            for (const record of records)
                insert.run(...nativeProofSchema.columns.map(column => record[column.wire]));
            db.exec('COMMIT');
            // An exception here is still UNKNOWN to the live observer. On reopen
            // the committed generation is reserved, never re-emitted as a receipt.
            this.#syncDirectory();
            return records;
        } finally {
            db.close(); // An interrupted transaction rolls back; never retry here.
        }
    }
}
