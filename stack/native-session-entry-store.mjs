/** Native session history storage. JSONL is authority; SQLite contains rebuildable selectors only. */
import { closeSync, fstatSync, mkdirSync, mkdtempSync, openSync, readSync, rmSync, statSync, unlinkSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { getHeapStatistics } from 'node:v8';
import { sessionEntryToContextMessages } from './session-manager.js';

function revision(stat) {
    return `${stat.dev}:${stat.ino}:${stat.size}:${stat.mtimeNs}:${stat.ctimeNs}`;
}

/** One entry's selectors, never its message body or compaction summary. */
export class EntryMetadata {
    constructor(entry, sequence, offset = 0, length = 0) {
        this.id = entry.id;
        this.parentId = entry.parentId;
        this.type = entry.type;
        this.sequence = sequence;
        this.offset = offset;
        this.length = length;
        this.role = entry.message?.role ?? null;
        this.inputId = entry.message?.inputId ?? null;
        this.inputDigest = entry.message?.inputDigest ?? null;
        this.commitId = entry.details?.agentCommsCommit?.commitId ?? null;
        this.firstKeptEntryId = entry.firstKeptEntryId ?? null;
        this.contextMessageCount = sessionEntryToContextMessages(entry).length;
        this.model = entry.type === 'model_change'
            ? { provider: entry.provider, modelId: entry.modelId }
            : this.role === 'assistant' ? { provider: entry.message.provider, modelId: entry.message.model } : null;
        this.thinkingLevel = entry.type === 'thinking_level_change' ? entry.thinkingLevel : null;
        this.label = entry.type === 'label' ? { targetId: entry.targetId, label: entry.label ?? null, timestamp: entry.timestamp } : null;
    }
    static fromIndex(row) {
        if (!row) return undefined;
        return Object.assign(Object.create(EntryMetadata.prototype), JSON.parse(row.selectors));
    }
}

export class StoreAvailability {
    constructor() { if (new.target === StoreAvailability) throw new TypeError('Concrete store availability required'); }
    requireOpen() { throw new Error('Concrete store availability required'); }
    closed(cause) { return new UnavailableStore(cause); }
}
export class AvailableStore extends StoreAvailability {
    requireOpen() {}
}
export class UnavailableStore extends StoreAvailability {
    constructor(cause) { super(); this.cause = cause; }
    requireOpen() { throw new Error('Native session store unusable; reopen validated disk, never replay', {cause:this.cause}); }
    closed() { return this; }
}

/** Shared integrity, ancestry and context selection. Implementations own only storage. */
export class EntryStore {
    #availability = new AvailableStore();
    constructor() {
        if (new.target === EntryStore) throw new TypeError('EntryStore requires a concrete storage owner');
    }
    get header() { throw new Error('EntryStore.header must be implemented'); }
    get lastId() { throw new Error('EntryStore.lastId must be implemented'); }
    metadata(id) { throw new Error('EntryStore.metadata must be implemented'); }
    get(id) { throw new Error('EntryStore.get must be implemented'); }
    *metadataEntries() { throw new Error('EntryStore.metadataEntries must be implemented'); }
    *branchMetadata(leafId) { throw new Error('EntryStore.branchMetadata must be implemented'); }
    append(entry) { throw new Error('EntryStore.append must be implemented'); }
    committedAppend(entry, file) { throw new Error('EntryStore.committedAppend must be implemented'); }
    storedAt(file) { throw new Error('EntryStore.storedAt must be implemented'); }
    close() { throw new Error('EntryStore.close must be implemented'); }
    assertCurrent() { throw new Error('Persisted native session observation required'); }
    assertUsable() { this.#availability.requireOpen(); }
    invalidate(cause) { this.#availability = this.#availability.closed(cause); }
    has(id) { return this.metadata(id) !== undefined; }
    validate(entry) {
        if (!entry || typeof entry !== 'object' || Array.isArray(entry) ||
            typeof entry.type !== 'string' || entry.type === 'session' ||
            typeof entry.id !== 'string' || !entry.id || entry.id === this.header.id || this.has(entry.id) ||
            !(entry.parentId === null || typeof entry.parentId === 'string' && this.has(entry.parentId)))
            throw new Error('Invalid native entry identity or ancestry');
        if (entry.type === 'message' && (!entry.message || typeof entry.message !== 'object' ||
            typeof entry.message.role !== 'string')) throw new Error('Invalid native message entry');
    }
    static validateHeader(header) {
        if (!header || header.type !== 'session' || header.version !== 3 ||
            typeof header.id !== 'string' || !header.id)
            throw new Error('Strict native v3 session header required');
        return header;
    }
    *entries() { for (const meta of this.metadataEntries()) yield this.get(meta.id); }
    *fileEntries() { yield this.header; yield* this.entries(); }
    *branch(leafId = this.lastId) { for (const meta of this.branchMetadata(leafId)) yield this.get(meta.id); }
    *ancestors(leafId = this.lastId) {
        let meta = leafId === null ? undefined : this.metadata(leafId);
        while (meta) {
            yield meta;
            meta = meta.parentId === null ? undefined : this.metadata(meta.parentId);
        }
    }
    branchContains(leafId, id) {
        for (const meta of this.ancestors(leafId)) if (meta.id === id) return true;
        return false;
    }
    commonAncestor(leftId, rightId) {
        let left = leftId === null ? undefined : this.metadata(leftId);
        let right = rightId === null ? undefined : this.metadata(rightId);
        while (left && right) {
            if (left.id === right.id) return left.id;
            if (left.sequence > right.sequence) left = this.metadata(left.parentId);
            else right = this.metadata(right.parentId);
        }
        return null;
    }
    latestMetadata(leafId, type) {
        for (const meta of this.ancestors(leafId)) if (meta.type === type) return meta;
        return undefined;
    }
    latest(leafId, type) {
        const meta = this.latestMetadata(leafId, type);
        return meta ? this.get(meta.id) : undefined;
    }
    contextSettings(leafId = this.lastId) {
        let model = null, thinkingLevel = null;
        for (const meta of this.ancestors(leafId)) {
            model ??= meta.model;
            thinkingLevel ??= meta.thinkingLevel;
            if (model && thinkingLevel !== null) break;
        }
        return { model, thinkingLevel: thinkingLevel ?? 'off' };
    }
    *contextMetadata(leafId = this.lastId) {
        this.assertCurrent();
        try {
            const compaction = this.latestMetadata(leafId, 'compaction');
            if (!compaction) { yield* this.branchMetadata(leafId); return; }
            yield compaction;
            yield* this.keptMetadata(leafId, compaction);
        } finally { this.assertCurrent(); }
    }
    *keptMetadata(leafId, compaction) {
        // The same original floor owns both canonical and recent-only views.
        let keeping = false;
        for (const meta of this.branchMetadata(leafId)) {
            if (meta.id === compaction.firstKeptEntryId) keeping = true;
            if (meta.id === compaction.id) { keeping = true; continue; }
            if (keeping) yield meta;
        }
    }
    *uncompactedEntries(leafId = this.lastId) {
        this.assertCurrent();
        try {
            for (const meta of this.branchMetadata(leafId)) {
                // A stored summary replaces source; it is not raw ancestry.
                if (meta.type !== 'compaction') yield this.get(meta.id);
            }
        } finally { this.assertCurrent(); }
    }
    *keptEntries(leafId = this.lastId) {
        this.assertCurrent();
        try {
            const compaction = this.latestMetadata(leafId, 'compaction');
            if (!compaction) throw new Error('Kept-source view requires an original compaction floor');
            for (const meta of this.keptMetadata(leafId, compaction)) yield this.get(meta.id);
        } finally { this.assertCurrent(); }
    }
    *contextEntries(leafId = this.lastId) {
        for (const meta of this.contextMetadata(leafId)) yield this.get(meta.id);
    }
    contextMessageCount(leafId = this.lastId) {
        let count = 0;
        for (const meta of this.contextMetadata(leafId)) {
            count += meta.contextMessageCount;
        }
        return count;
    }
    *trackedMetadata() {
        for (const meta of this.metadataEntries()) if (meta.inputId !== null) yield meta;
    }
    trackedInputMetadata(inputId) {
        let found;
        for (const meta of this.trackedMetadata()) if (meta.inputId === inputId) {
            if (found) throw new Error('Duplicate native input ID in session');
            found = meta;
        }
        return found;
    }
    *trackedInputs() { for (const meta of this.trackedMetadata()) yield this.get(meta.id); }
    trackedInput(inputId) {
        const meta = this.trackedInputMetadata(inputId);
        return meta ? this.get(meta.id) : undefined;
    }
    *commits(commitId) {
        for (const meta of this.metadataEntries()) if (meta.commitId === commitId) yield this.get(meta.id);
    }
    *children(parentId) {
        for (const meta of this.metadataEntries()) if (meta.parentId === parentId) yield this.get(meta.id);
    }
    label(id) {
        let found;
        for (const meta of this.metadataEntries()) if (meta.label?.targetId === id) found = meta.label;
        return found?.label ? found : undefined;
    }
}

/** A re-iterable branch suffix, excluding its ancestor boundary. */
export class EntryBranchRange {
    constructor(store, leafId, ancestorId = null) {
        this.store = store;
        this.leafId = leafId;
        this.start = ancestorId === null ? -1 : store.metadata(ancestorId).sequence;
    }
    *[Symbol.iterator]() {
        for (const meta of this.store.branchMetadata(this.leafId)) {
            if (meta.sequence > this.start) yield this.store.get(meta.id);
        }
    }
    *reverse() {
        for (const meta of this.store.ancestors(this.leafId)) {
            if (meta.sequence <= this.start) break;
            yield this.store.get(meta.id);
        }
    }
    isEmpty() { return this.leafId === null || this.store.metadata(this.leafId).sequence <= this.start; }
}

export class MemoryEntryStore extends EntryStore {
    #header;
    #entries = new Map();
    #metadata = new Map();
    #last = null;
    constructor(header, entries = []) {
        super();
        this.#header = EntryStore.validateHeader(header);
        for (const entry of entries) this.append(entry);
    }
    get header() { this.assertUsable(); return this.#header; }
    get lastId() { this.assertUsable(); return this.#last; }
    get(id) { this.assertUsable(); return this.#entries.get(id); }
    metadata(id) { this.assertUsable(); return this.#metadata.get(id); }
    assertCurrent() { this.assertUsable(); }
    *metadataEntries() { this.assertUsable(); yield* this.#metadata.values(); }
    *branchMetadata(leafId = this.lastId) { yield* [...this.ancestors(leafId)].reverse(); }
    append(entry) {
        this.validate(entry);
        this.#metadata.set(entry.id, new EntryMetadata(entry, this.#entries.size + 1));
        this.#entries.set(entry.id, entry);
        this.#last = entry.id;
    }
    committedAppend(entry, file) {
        if (file) return this.storedAt(file);
        this.append(entry); return this;
    }
    storedAt(file) {
        const store = new DiskEntryStore(file);
        this.close();
        return store;
    }
    close() { this.invalidate(); this.#entries.clear(); this.#metadata.clear(); }
}

/** Disk-backed, bounded-page selector index; payloads remain exclusively in the native JSONL. */
export class DiskEntryStore extends EntryStore {
    #fd;
    #db;
    #header;
    #last = null;
    #revision;
    #extent = 0;
    #sequence = 0;
    #metadata;
    #insert;
    constructor(file, { indexDirectory = process.env.AGENT_COMMS_SESSION_INDEX_DIR ?? join(homedir(), '.cache', 'agent-comms', 'session-indexes') } = {}) {
        super();
        this.file = resolve(file);
        mkdirSync(indexDirectory, { recursive: true, mode: 0o700 });
        const directory = mkdtempSync(join(indexDirectory, 'open-'));
        const index = join(directory, 'entries.sqlite');
        try {
            this.#fd = openSync(this.file, 'r');
            this.#db = new DatabaseSync(index);
            this.#db.exec('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA mmap_size=0; PRAGMA locking_mode=EXCLUSIVE;');
            this.#db.exec(`CREATE TABLE entries(sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                parent TEXT, type TEXT NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL,
                input_id TEXT, commit_id TEXT, selectors TEXT NOT NULL);
                CREATE INDEX entry_parent ON entries(parent);
                CREATE INDEX entry_input ON entries(input_id);
                CREATE INDEX entry_commit ON entries(commit_id);
                CREATE INDEX entry_label ON entries(json_extract(selectors,'$.label.targetId'),sequence);`);
            // SQLite has initialized and owns an exclusive open inode. Derived pages
            // live on persistent disk, but no pathname survives reader exit or SIGKILL.
            unlinkSync(index);
            rmSync(directory, {recursive:true});
            this.#metadata = this.#db.prepare('SELECT selectors FROM entries WHERE id=?');
            this.#insert = this.#db.prepare('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?)');
            this.refresh();
        } catch (error) {
            this.close();
            rmSync(directory, { recursive: true, force: true });
            throw error;
        }
    }
    get header() { this.assertUsable(); return this.#header; }
    get lastId() { this.assertUsable(); return this.#last; }
    get revision() { this.assertUsable(); return this.#revision; }
    metadata(id) { this.assertUsable(); return EntryMetadata.fromIndex(this.#metadata.get(id)); }
    *metadataEntries() {
        this.assertUsable();
        for (const row of this.#db.prepare('SELECT selectors FROM entries ORDER BY sequence').iterate())
            yield EntryMetadata.fromIndex(row);
    }
    *branchMetadata(leafId = this.lastId) {
        this.assertUsable();
        if (leafId === null) return;
        for (const row of this.#db.prepare(`WITH RECURSIVE path(id,parent,sequence,selectors) AS (
            SELECT id,parent,sequence,selectors FROM entries WHERE id=? UNION ALL
            SELECT e.id,e.parent,e.sequence,e.selectors FROM entries e JOIN path p ON e.id=p.parent)
            SELECT selectors FROM path ORDER BY sequence`).iterate(leafId)) yield EntryMetadata.fromIndex(row);
    }
    *trackedMetadata() {
        this.assertCurrent();
        try { for (const row of this.#db.prepare('SELECT selectors FROM entries WHERE input_id IS NOT NULL ORDER BY sequence').iterate())
            yield EntryMetadata.fromIndex(row);
        } finally { this.assertCurrent(); }
    }
    trackedInputMetadata(inputId) {
        this.assertCurrent();
        const rows = this.#db.prepare('SELECT selectors FROM entries WHERE input_id=? LIMIT 2').all(inputId);
        this.assertCurrent();
        if (rows.length > 1) throw new Error('Duplicate native input ID in session');
        return EntryMetadata.fromIndex(rows[0]);
    }
    label(id) {
        this.assertUsable();
        const row = this.#db.prepare("SELECT selectors FROM entries WHERE json_extract(selectors,'$.label.targetId')=? ORDER BY sequence DESC LIMIT 1").get(id);
        const label = EntryMetadata.fromIndex(row)?.label;
        return label?.label ? label : undefined;
    }
    *commits(commitId) {
        this.assertUsable();
        for (const row of this.#db.prepare('SELECT id FROM entries WHERE commit_id=? ORDER BY sequence').iterate(commitId)) yield this.get(row.id);
    }
    *children(parentId) {
        this.assertUsable();
        for (const row of this.#db.prepare('SELECT id FROM entries WHERE parent IS ? ORDER BY sequence').iterate(parentId)) yield this.get(row.id);
    }
    assertCurrent() {
        this.assertUsable();
        if (revision(fstatSync(this.#fd, { bigint: true })) !== this.#revision ||
            revision(statSync(this.file, { bigint: true })) !== this.#revision)
            throw new Error('Native session revision changed; reopen validated history, never replay');
    }
    #decode(raw) {
        // UTF16 decoding alone can need twice the encoded bytes; use actual VM
        // available heap, not a limit on the lifetime size of the session.
        if (raw.length * Uint16Array.BYTES_PER_ELEMENT > getHeapStatistics().total_available_size)
            throw new Error('Native entry decode exceeds currently available VM heap');
        return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw));
    }
    get(id) {
        const meta = this.metadata(id);
        if (!meta) return undefined;
        this.assertCurrent();
        if (meta.length * Uint16Array.BYTES_PER_ELEMENT > getHeapStatistics().total_available_size)
            throw new Error('Native entry decode exceeds currently available VM heap');
        const data = Buffer.allocUnsafe(meta.length);
        let offset = 0;
        while (offset < data.length) {
            const n = readSync(this.#fd, data, offset, data.length - offset, meta.offset + offset);
            if (!n) throw new Error('Incomplete native entry');
            offset += n;
        }
        const entry = this.#decode(data);
        this.assertCurrent();
        return entry;
    }
    #index(entry, offset, length) {
        if (!this.#header) { this.#header = EntryStore.validateHeader(entry); return; }
        this.validate(entry);
        const meta = new EntryMetadata(entry, ++this.#sequence, offset, length);
        this.#insert.run(meta.sequence, meta.id, meta.parentId, meta.type, offset, length, meta.inputId, meta.commitId, JSON.stringify(meta));
        this.#last = meta.id;
    }
    refresh() {
        const before = fstatSync(this.#fd, { bigint: true });
        if (revision(statSync(this.file, { bigint: true })) !== revision(before) ||
            Number(before.size) < this.#extent) throw new Error('Native session replaced or truncated');
        if (this.#revision && revision(before) === this.#revision) return;
        if (this.#revision) throw new Error('Unrecognized native revision; open a fresh store');
        const buffer = Buffer.allocUnsafe(64 * 1024);
        let position = this.#extent, lineStart = position, parts = [], size = 0;
        this.#db.exec('BEGIN');
        try {
            while (position < Number(before.size)) {
                const n = readSync(this.#fd, buffer, 0, Math.min(buffer.length, Number(before.size) - position), position);
                if (!n) throw new Error('Native session truncated during scan');
                let start = 0;
                for (let end = 0; end < n; end++) if (buffer[end] === 10) {
                    const tail = buffer.subarray(start, end);
                    const raw = parts.length ? Buffer.concat([...parts, tail], size + tail.length) : tail;
                    this.#index(this.#decode(raw), lineStart, size + tail.length);
                    lineStart = position + end + 1;
                    parts = []; size = 0; start = end + 1;
                }
                if (start < n) {
                    size += n - start;
                    if (size * Uint16Array.BYTES_PER_ELEMENT > getHeapStatistics().total_available_size)
                        throw new Error('Native entry decode exceeds currently available VM heap');
                    parts.push(Buffer.from(buffer.subarray(start, n)));
                }
                position += n;
            }
            if (size || !this.#header) throw new Error('Incomplete native session newline/header');
            if (revision(fstatSync(this.#fd, { bigint: true })) !== revision(before) ||
                revision(statSync(this.file, { bigint: true })) !== revision(before))
                throw new Error('Native session changed during strict scan');
            this.#db.exec('COMMIT');
            this.#extent = position;
            this.#revision = revision(before);
        } catch (error) { this.#db.exec('ROLLBACK'); throw error; }
    }
    /** Extend only after SessionManager's existing writer fence appended these exact bytes. */
    append(entry) {
        this.validate(entry);
        const before = this.#extent;
        const current = fstatSync(this.#fd, { bigint: true });
        const encoded = Buffer.from(JSON.stringify(entry) + '\n');
        if (Number(current.size) !== before + encoded.length ||
            revision(statSync(this.file, { bigint: true })) !== revision(current))
            throw new Error('Native append extent changed');
        const saved = Buffer.allocUnsafe(encoded.length);
        if (readSync(this.#fd, saved, 0, saved.length, before) !== saved.length || !saved.equals(encoded))
            throw new Error('Native appended entry differs');
        this.#index(entry, before, encoded.length - 1);
        this.#extent = Number(current.size);
        this.#revision = revision(current);
    }
    committedAppend(entry) { this.append(entry); return this; }
    storedAt(file) {
        if (resolve(file) !== this.file) throw new Error('Native storage identity differs');
        this.assertCurrent(); return this;
    }
    close() {
        this.invalidate();
        const database = this.#db, descriptor = this.#fd;
        this.#db = undefined; this.#fd = undefined;
        try { database?.close(); }
        finally { if (descriptor !== undefined) closeSync(descriptor); }
    }
}
