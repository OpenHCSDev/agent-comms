/** Native session history storage. JSONL is authority; SQLite contains rebuildable selectors only. */
import { closeSync, fstatSync, lstatSync, mkdirSync, openSync, readSync, rmSync, statSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { getHeapStatistics } from 'node:v8';
import { sessionEntryToContextMessages } from './session-manager.js';

function revision(stat) {
    return `${stat.dev}:${stat.ino}:${stat.size}:${stat.mtimeNs}:${stat.ctimeNs}`;
}

/** One entry's selectors, never its message body or summary. */
export class EntryMetadata {
    static indexVersion = 4;
    static initialSettings = Object.freeze({model: null, thinkingLevel: 'off'});
    inheritSettings(parent) {
        if (!this.contextSettings && parent) this.contextSettings = {
            model: this.model ?? parent.model,
            thinkingLevel: this.thinkingLevel ?? parent.thinkingLevel,
        };
        return this;
    }
    constructor(entry, sequence, offset = 0, length = 0, parentSequence) {
        this.id = entry.id;
        this.parentId = entry.parentId;
        this.type = entry.type;
        this.timestamp = entry.timestamp;
        this.time = Date.parse(entry.timestamp);
        this.sequence = sequence;
        this.offset = offset;
        this.length = length;
        // A read projection, not a selector persisted beside parentId.
        Object.defineProperty(this, 'parentSequence', {value: parentSequence});
        this.role = entry.message?.role ?? null;
        this.inputId = entry.message?.inputId ?? null;
        this.inputDigest = entry.message?.inputDigest ?? null;
        this.firstKeptEntryId = entry.firstKeptEntryId ?? null;
        this.contextMessageCount = sessionEntryToContextMessages(entry).length;
        this.model = entry.type === 'model_change'
            ? { provider: entry.provider, modelId: entry.modelId }
            : this.role === 'assistant' ? { provider: entry.message.provider, modelId: entry.message.model } : null;
        this.thinkingLevel = entry.type === 'thinking_level_change' ? entry.thinkingLevel : null;
        this.label = EntryMetadata.labelFor(entry);
    }
    static labelFor(entry) {
        return entry.type === 'label' ? { targetId: entry.targetId, label: entry.label ?? null, timestamp: entry.timestamp } : null;
    }
    static liveLabel(label) { return label?.label ? label : undefined; }
    get liveLabel() { return EntryMetadata.liveLabel(this.label); }
    static fromIndex(row) {
        if (!row) return undefined;
        const meta = Object.setPrototypeOf(JSON.parse(row.selectors), EntryMetadata.prototype);
        Object.defineProperty(meta, 'parentSequence', {value: row.parent_sequence});
        return meta;
    }
}

/** The existing SDK tree node owns only its demanded entry and derived child view. */
class SessionTreeNode {
    #store;
    #metadata;
    constructor(store, metadata) {
        this.#store = store;
        this.#metadata = metadata;
        this.children = [];
        this.label = undefined;
        this.labelTimestamp = undefined;
    }
    get metadata() { return this.#metadata; }
    get entry() {
        const entry = this.#store.get(this.#metadata.id);
        Object.defineProperty(this, 'entry', {value: entry, enumerable: true});
        return entry;
    }
    toJSON() {
        return {entry: this.entry, children: this.children,
            label: this.label, labelTimestamp: this.labelTimestamp};
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
    *chronologicalMetadata() { throw new Error('EntryStore.chronologicalMetadata must be implemented'); }
    *branchMetadata(leafId = this.lastId, firstId = null) {
        const selected = new Set();
        let first;
        for (const meta of this.ancestors(leafId)) {
            selected.add(meta.id);
            first = meta;
            if (meta.id === firstId) break;
        }
        if (first) for (const meta of this.chronologicalMetadata(first.id))
            if (selected.has(meta.id)) yield meta;
    }
    append(entry) {
        this.validate(entry);
        const coordinates = this.appendCoordinates(entry);
        const parent = entry.parentId === null ? undefined : this.settingsMetadata(entry.parentId);
        if (entry.parentId !== null && !parent) throw new Error('Invalid native append ancestry');
        this.storeAppended(entry, this.metadataFor(entry, ...coordinates, parent));
    }
    appendCoordinates(entry) { throw new Error('EntryStore.appendCoordinates must be implemented'); }
    storeAppended(entry, metadata) { throw new Error('EntryStore.storeAppended must be implemented'); }
    knownMetadata(id) { return this.metadata(id); }
    settingsMetadata(id) { return this.metadata(id); }
    metadataFor(entry, sequence, offset = 0, length = 0, parent = this.knownMetadata(entry.parentId)) {
        return new EntryMetadata(entry, sequence, offset, length, parent?.sequence).inheritSettings(
            entry.parentId === null ? EntryMetadata.initialSettings : parent?.contextSettings);
    }
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
            typeof entry.id !== 'string' || !entry.id || entry.id === this.header.id ||
            !(entry.parentId === null || typeof entry.parentId === 'string'))
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
    *entries() { for (const meta of this.chronologicalMetadata()) yield this.get(meta.id); }
    tree() {
        // Source sequence locates output nodes; IDs stay with the metadata index.
        const nodes = new Map(), labels = [], roots = [];
        for (const meta of this.chronologicalMetadata()) {
            const node = new SessionTreeNode(this, meta);
            nodes.set(meta.sequence, node);
            const parent = meta.parentId === null ? undefined : nodes.get(meta.parentSequence);
            if (meta.parentId === null || parent === node || !parent) roots.push(node);
            else parent.children.push(node);
            if (meta.label) labels.push(meta.label);
        }
        for (const declared of labels) {
            const node = nodes.get(this.metadata(declared.targetId)?.sequence);
            if (!node) continue;
            const label = EntryMetadata.liveLabel(declared);
            node.label = label?.label;
            node.labelTimestamp = label?.timestamp;
        }
        const pending = [...roots];
        while (pending.length) {
            const node = pending.pop();
            if (node.children.length > 1)
                node.children.sort((a, b) => a.metadata.time - b.metadata.time);
            pending.push(...node.children);
        }
        return roots;
    }
    *fileEntries() { yield this.header; yield* this.entries(); }
    *branch(leafId = this.lastId) { for (const meta of this.branchMetadata(leafId)) yield this.get(meta.id); }
    *ancestors(leafId = this.lastId) {
        let meta = leafId === null ? undefined : this.metadata(leafId);
        while (meta) {
            yield meta;
            if (meta.parentId === null) return;
            const parent = this.metadata(meta.parentId);
            if (!parent || parent.sequence >= meta.sequence) throw new Error('Invalid native entry ancestry');
            meta = parent;
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
        this.assertCurrent();
        return this.settingsMetadata(leafId)?.contextSettings ?? EntryMetadata.initialSettings;
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
    *keptMetadata(leafId = this.lastId, compaction = this.latestMetadata(leafId, 'compaction')) {
        // The same original floor owns both canonical and recent-only views.
        if (!compaction) throw new Error('Kept-source view requires an original compaction floor');
        for (const meta of this.branchMetadata(leafId, compaction.firstKeptEntryId ?? compaction.id))
            if (meta.id !== compaction.id) yield meta;
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
        for (const meta of this.trackedMetadata()) if (meta.inputId === inputId) return meta;
        return undefined;
    }
    *trackedInputs() { for (const meta of this.trackedMetadata()) yield this.get(meta.id); }
    trackedInput(inputId) {
        const meta = this.trackedInputMetadata(inputId);
        return meta ? this.get(meta.id) : undefined;
    }
    *children(parentId) {
        for (const meta of this.chronologicalMetadata())
            if (meta.parentId === parentId) yield this.get(meta.id);
    }
    *labels() {
        const seen = new Set();
        for (const meta of this.metadataEntries()) {
            if (!meta.label || seen.has(meta.label.targetId)) continue;
            seen.add(meta.label.targetId);
            const label = meta.liveLabel;
            if (label) yield label;
        }
    }
    label(id) {
        for (const meta of this.metadataEntries()) if (meta.label?.targetId === id)
            return meta.liveLabel;
        return undefined;
    }
}

/** A re-iterable branch suffix, excluding its ancestor boundary. */
export class EntryBranchRange {
    constructor(store, leafId, ancestorId = null) {
        this.store = store;
        this.leafId = leafId;
        this.ancestorId = ancestorId;
        this.start = ancestorId === null ? -1 : store.metadata(ancestorId).sequence;
    }
    *[Symbol.iterator]() {
        for (const meta of this.store.branchMetadata(this.leafId, this.ancestorId)) {
            if (meta.sequence > this.start) yield this.store.get(meta.id);
        }
    }
    *newestFirst() {
        for (const meta of this.store.ancestors(this.leafId)) {
            if (meta.sequence <= this.start) break;
            yield this.store.get(meta.id);
        }
    }
    isEmpty() { return this.leafId === null || this.store.metadata(this.leafId).sequence <= this.start; }
}

export class MemoryEntryStore extends EntryStore {
    #header;
    #entries = [];
    #metadata = new Map();
    constructor(header, entries = []) {
        super();
        this.#header = EntryStore.validateHeader(header);
        for (const entry of entries) this.append(entry);
    }
    get header() { this.assertUsable(); return this.#header; }
    get lastId() { this.assertUsable(); return this.#entries.at(-1)?.id ?? null; }
    get(id) {
        const meta = this.metadata(id);
        return meta ? this.#entries[meta.sequence - 1] : undefined;
    }
    metadata(id) { this.assertUsable(); return this.#metadata.get(id); }
    assertCurrent() { this.assertUsable(); }
    *metadataEntries() {
        this.assertUsable();
        for (let i = this.#entries.length - 1; i >= 0; i--)
            yield this.#metadata.get(this.#entries[i].id);
    }
    *chronologicalMetadata(firstId = null) {
        this.assertUsable();
        const start = firstId === null ? 0 : this.metadata(firstId).sequence - 1;
        for (let i = start; i < this.#entries.length; i++)
            yield this.#metadata.get(this.#entries[i].id);
    }
    appendCoordinates(entry) {
        if (this.#metadata.has(entry.id)) throw new Error('Invalid native entry identity');
        return [this.#entries.length + 1, 0, 0];
    }
    storeAppended(entry, metadata) {
        this.#metadata.set(entry.id, metadata);
        this.#entries.push(entry);
    }
    committedAppend(entry, file, write) {
        if (write) file = write();
        if (file) return this.storedAt(file);
        this.append(entry); return this;
    }
    storedAt(file) {
        const store = new DiskEntryStore(file);
        this.close();
        return store;
    }
    close() { this.invalidate(); this.#entries.length = 0; this.#metadata.clear(); }
}

/** Disk-backed, bounded-page selector index; payloads remain exclusively in the native JSONL. */
export class DiskEntryStore extends EntryStore {
    #fd;
    #db;
    #header;
    #last = null;
    #revision;
    #extent = 0;
    #metadata;
    #input;
    #label;
    #labels;
    #children;
    #insert;
    #lines;
    #headerEnd;
    #decoder = new TextDecoder('utf-8', {fatal: true});
    constructor(file, { indexDirectory = join(homedir(), '.cache', 'agent-comms', 'session-indexes') } = {}) {
        super();
        this.file = resolve(file);
        mkdirSync(indexDirectory, { recursive: true, mode: 0o700 });
        const directory = join(indexDirectory, createHash('sha256').update(this.file).digest('hex'));
        mkdirSync(directory, {recursive: true, mode: 0o700});
        const info = lstatSync(directory);
        if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o077))
            throw new Error('Native metadata index directory is not private');
        for (let version = 2; version < EntryMetadata.indexVersion; version++)
            for (const suffix of ['', '-wal', '-shm'])
                rmSync(join(directory, `entries-v${version}.sqlite${suffix}`), {force: true});
        const index = join(directory, `entries-v${EntryMetadata.indexVersion}.sqlite`);
        try { closeSync(openSync(index, 'ax', 0o600)); }
        catch (error) { if (error.code !== 'EEXIST') throw error; }
        const indexInfo = lstatSync(index);
        if (!indexInfo.isFile() || indexInfo.nlink !== 1 || indexInfo.uid !== process.getuid() || (indexInfo.mode & 0o077))
            throw new Error('Native metadata index file is not private');
        try {
            this.#fd = openSync(this.file, 'r');
            this.#db = new DatabaseSync(index);
            this.#db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA mmap_size=0; PRAGMA busy_timeout=10000;');
            this.#db.exec(`CREATE TABLE IF NOT EXISTS source(revision TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entries(sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                parent TEXT, type TEXT NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL,
                input_id TEXT, selectors TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS entry_input ON entries(input_id);
                CREATE INDEX IF NOT EXISTS entry_parent ON entries(parent,sequence);
                CREATE INDEX IF NOT EXISTS entry_label ON entries(json_extract(selectors,'$.label.targetId'),sequence DESC);
                CREATE INDEX IF NOT EXISTS entry_pending_context ON entries(parent)
                    WHERE json_type(selectors,'$.contextSettings') IS NULL;`);
            this.#metadata = this.#db.prepare('SELECT selectors FROM entries WHERE id=?');
            this.#input = this.#db.prepare('SELECT selectors FROM entries WHERE input_id=? ORDER BY sequence DESC LIMIT 1');
            this.#label = this.#db.prepare("SELECT selectors FROM entries WHERE json_extract(selectors,'$.label.targetId')=? ORDER BY sequence DESC LIMIT 1");
            this.#labels = this.#db.prepare(`SELECT selectors FROM (
                SELECT selectors, row_number() OVER (
                    PARTITION BY json_extract(selectors,'$.label.targetId') ORDER BY sequence DESC
                ) AS position FROM entries INDEXED BY entry_label
                WHERE json_extract(selectors,'$.label.targetId') IS NOT NULL
            ) WHERE position=1`);
            this.#children = this.#db.prepare('SELECT id FROM entries WHERE parent IS ? ORDER BY sequence');
            this.#insert = this.#db.prepare('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)');
            this.#writeIndex(() => this.refresh());
        } catch (error) {
            this.close();
            throw error;
        }
    }
    #writeIndex(action) {
        const owns = !this.#db.isTransaction;
        if (owns) this.#db.exec('BEGIN IMMEDIATE');
        try {
            if (owns && this.#revision) {
                if (this.#db.prepare('SELECT revision FROM source').get()?.revision !== this.#revision)
                    throw new Error('Native metadata index revision changed');
            }
            const result = action();
            if (owns) this.#db.exec('COMMIT');
            return result;
        } catch (error) {
            if (owns) this.#db.exec('ROLLBACK');
            throw error;
        }
    }
    knownMetadata(id) { return id === null ? undefined : EntryMetadata.fromIndex(this.#metadata.get(id)); }
    settingsMetadata(id) {
        let meta = this.metadata(id);
        if (!meta || meta.contextSettings) return meta;
        return this.#writeIndex(() => {
            while (!meta.contextSettings && this.#readPrevious()) meta = this.knownMetadata(id);
            meta = this.knownMetadata(id);
            if (!meta.contextSettings) throw new Error('Native settings ancestry is unresolved');
            return meta;
        });
    }
    #resolveSettings(meta, inherited) {
        const pending = [[meta, inherited]];
        const save = this.#db.prepare('UPDATE entries SET selectors=? WHERE id=?');
        while (pending.length) {
            const [current, parentSettings] = pending.pop();
            if (current.contextSettings) continue;
            const parent = parentSettings ?? (current.parentId === null ? EntryMetadata.initialSettings : this.knownMetadata(current.parentId)?.contextSettings);
            if (!parent) continue;
            current.inheritSettings(parent);
            save.run(JSON.stringify(current), current.id);
            for (const row of this.#db.prepare("SELECT selectors FROM entries WHERE parent=? AND json_type(selectors,'$.contextSettings') IS NULL").iterate(current.id))
                pending.push([EntryMetadata.fromIndex(row), current.contextSettings]);
        }
    }
    get header() { this.assertUsable(); return this.#header; }
    get lastId() { this.assertUsable(); return this.#last; }
    get revision() { this.assertUsable(); return this.#revision; }
    #find(statement, key) {
        this.assertUsable();
        let row = statement.get(key);
        if (!row) {
            this.assertCurrent();
            while (this.#readPrevious()) {
                row = statement.get(key);
                if (row) break;
            }
            this.assertCurrent();
        }
        return EntryMetadata.fromIndex(row);
    }
    metadata(id) { return id === null || id === this.#header.id ? undefined : this.#find(this.#metadata, id); }
    *metadataEntries() {
        this.assertCurrent();
        let ceiling = Number.MAX_SAFE_INTEGER;
        try {
            const page = this.#db.prepare('SELECT sequence,selectors FROM entries WHERE sequence < ? ORDER BY sequence DESC');
            for (;;) {
                for (const row of page.iterate(ceiling)) {
                    ceiling = row.sequence;
                    yield EntryMetadata.fromIndex(row);
                }
                if (!this.#readPrevious()) return;
            }
        } finally { this.assertCurrent(); }
    }
    *chronologicalMetadata(firstId = null) {
        this.assertCurrent();
        try {
            const start = firstId === null ? 0 : this.metadata(firstId).sequence;
            if (firstId === null) while (this.#readPrevious()) {}
            for (const row of this.#db.prepare(`SELECT e.selectors, p.sequence AS parent_sequence FROM entries e
                LEFT JOIN entries p ON p.id=e.parent WHERE e.sequence >= ? ORDER BY e.sequence`).iterate(start))
                yield EntryMetadata.fromIndex(row);
        } finally { this.assertCurrent(); }
    }
    *children(parentId) {
        this.assertCurrent();
        try {
            // A child follows its parent in the journal. Reading through that
            // parent indexes every possible child; roots require the full index.
            if (parentId === null) while (this.#readPrevious()) {}
            else this.metadata(parentId);
            for (const row of this.#children.iterate(parentId)) yield this.get(row.id);
        } finally { this.assertCurrent(); }
    }
    trackedInputMetadata(inputId) { return this.#find(this.#input, inputId); }
    *labels() {
        this.assertCurrent();
        try {
            while (this.#readPrevious()) {}
            for (const row of this.#labels.iterate()) {
                const label = EntryMetadata.fromIndex(row).liveLabel;
                if (label) yield label;
            }
        } finally { this.assertCurrent(); }
    }
    label(id) { return this.#find(this.#label, id)?.liveLabel; }
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
        return JSON.parse(this.#decoder.decode(raw));
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
    *entries() {
        this.assertCurrent();
        try {
            const seen = new Set(), forwardParents = new Set();
            for (const entry of this.#forwardEntries()) {
                this.validate(entry);
                if (seen.has(entry.id)) throw new Error('Duplicate native entry identity');
                if (forwardParents.has(entry.id)) throw new Error('Invalid native entry ancestry');
                if (entry.parentId !== null && entry.parentId !== entry.id && !seen.has(entry.parentId))
                    forwardParents.add(entry.parentId);
                seen.add(entry.id);
                yield entry;
            }
        } finally { this.assertCurrent(); }
    }
    *#forwardEntries() {
        // Whole-history consumers need every payload, not a selector-index build
        // followed by one random read and another metadata decode per payload.
        const buffer = Buffer.allocUnsafe(1024 * 1024);
        let position = this.#headerEnd, parts = [], size = 0;
        while (position < this.#extent) {
            const count = readSync(this.#fd, buffer, 0, Math.min(buffer.length, this.#extent - position), position);
            if (!count) throw new Error('Incomplete native entry');
            const chunk = buffer.subarray(0, count);
            let start = 0, newline;
            while ((newline = chunk.indexOf(10, start)) !== -1) {
                const part = chunk.subarray(start, newline);
                const raw = parts.length ? Buffer.concat([...parts, part], size + part.length) : part;
                yield this.#decode(raw);
                start = newline + 1; parts = []; size = 0;
            }
            if (start < count) {
                const part = Buffer.from(chunk.subarray(start));
                parts.push(part); size += part.length;
                if (size * Uint16Array.BYTES_PER_ELEMENT > getHeapStatistics().total_available_size)
                    throw new Error('Native entry exceeds currently available VM heap');
            }
            position += count;
        }
        if (size) throw new Error('Incomplete native entry');
    }
    #index(entry, offset, length) {
        this.validate(entry);
        // Byte offsets supply ordering; no lifetime row counter is needed.
        const existing = this.knownMetadata(entry.id);
        if (existing) {
            if (existing.offset !== offset || existing.length !== length) throw new Error('Duplicate native entry identity');
            return existing;
        }
        const meta = this.metadataFor(entry, offset, offset, length);
        const parent = this.knownMetadata(entry.parentId);
        if (parent && parent.sequence >= meta.sequence) throw new Error('Invalid native entry ancestry');
        this.#insert.run(meta.sequence, meta.id, meta.parentId, meta.type, offset, length, meta.inputId, JSON.stringify(meta));
        if (meta.contextSettings) {
            for (const row of this.#db.prepare("SELECT selectors FROM entries WHERE parent=? AND json_type(selectors,'$.contextSettings') IS NULL").all(meta.id))
                this.#resolveSettings(EntryMetadata.fromIndex(row), meta.contextSettings);
        } else this.#resolveSettings(meta);
        return this.knownMetadata(meta.id);
    }
    #readHeader() {
        const buffer = Buffer.allocUnsafe(4096), parts = [];
        let size = 0;
        for (let position = 0; position < this.#extent;) {
            const count = readSync(this.#fd, buffer, 0, Math.min(buffer.length, this.#extent - position), position);
            if (!count) throw new Error('Incomplete native header');
            const newline = buffer.subarray(0, count).indexOf(10);
            const part = Buffer.from(buffer.subarray(0, newline < 0 ? count : newline));
            parts.push(part); size += part.length;
            if (size * Uint16Array.BYTES_PER_ELEMENT > getHeapStatistics().total_available_size)
                throw new Error('Native header exceeds currently available VM heap');
            if (newline >= 0) {
                this.#header = EntryStore.validateHeader(this.#decode(Buffer.concat(parts, size)));
                this.#headerEnd = position + newline + 1;
                return;
            }
            position += count;
        }
        throw new Error('Incomplete native session header newline');
    }
    *#backwardLines(ceiling = this.#extent) {
        const buffer = Buffer.allocUnsafe(64 * 1024);
        const newline = Buffer.allocUnsafe(1);
        if (!this.#extent || readSync(this.#fd, newline, 0, 1, this.#extent - 1) !== 1 || newline[0] !== 10)
            throw new Error('Incomplete native session newline');
        let position = ceiling - 1, parts = [], size = 0;
        while (position > this.#headerEnd) {
            const start = Math.max(this.#headerEnd, position - buffer.length);
            const count = position - start;
            if (readSync(this.#fd, buffer, 0, count, start) !== count)
                throw new Error('Native session truncated during backward read');
            let end = count;
            for (let i = count - 1; i >= 0; i--) if (buffer[i] === 10) {
                const part = buffer.subarray(i + 1, end);
                const raw = parts.length ? Buffer.concat([part, ...parts], part.length + size) : part;
                yield { raw, offset: start + i + 1 };
                end = i; parts = []; size = 0;
            }
            const part = Buffer.from(buffer.subarray(0, end));
            parts.unshift(part); size += part.length;
            if (size * Uint16Array.BYTES_PER_ELEMENT > getHeapStatistics().total_available_size)
                throw new Error('Native entry exceeds currently available VM heap');
            position = start;
        }
        if (size) yield {raw: Buffer.concat(parts, size), offset: this.#headerEnd};
    }
    #readPrevious() {
        return this.#writeIndex(() => {
            this.assertCurrent();
            return this.#readIndexedPrevious();
        });
    }
    #readIndexedPrevious() {
        try {
            if (!this.#lines) return undefined;
            const next = this.#lines.next();
            if (next.done) {
                this.#lines = undefined;
                for (const row of this.#db.prepare(`SELECT e.selectors FROM entries e
                    WHERE json_type(e.selectors,'$.contextSettings') IS NULL
                    AND NOT EXISTS (SELECT 1 FROM entries p WHERE p.id=e.parent)`).all())
                    this.#resolveSettings(EntryMetadata.fromIndex(row), EntryMetadata.initialSettings);
                return undefined;
            }
            const {raw, offset} = next.value;
            const meta = this.#index(this.#decode(raw), offset, raw.length);
            this.#last ??= meta.id;
            return meta;
        } catch (error) {
            this.invalidate(error);
            throw error;
        }
    }
    refresh() {
        const before = fstatSync(this.#fd, { bigint: true });
        if (revision(statSync(this.file, { bigint: true })) !== revision(before) ||
            Number(before.size) < this.#extent) throw new Error('Native session replaced or truncated');
        if (this.#revision && revision(before) === this.#revision) return;
        if (this.#revision) throw new Error('Unrecognized native revision; open a fresh store');
        this.#extent = Number(before.size);
        this.#revision = revision(before);
        if (this.#db.prepare('SELECT revision FROM source').get()?.revision !== this.#revision) {
            this.#db.exec('DELETE FROM entries; DELETE FROM source;');
            this.#db.prepare('INSERT INTO source VALUES(?)').run(this.#revision);
        }
        this.#readHeader();
        const oldest = this.#db.prepare('SELECT offset FROM entries ORDER BY sequence LIMIT 1').get();
        this.#last = this.#db.prepare('SELECT id FROM entries ORDER BY sequence DESC LIMIT 1').get()?.id ?? null;
        this.#lines = this.#backwardLines(oldest?.offset ?? this.#extent);
        const latest = this.#last ? this.knownMetadata(this.#last) : this.#readPrevious();
        if (latest) for (const meta of this.ancestors(latest.id)) {
            if (meta.type === 'compaction') break;
        }
        this.assertCurrent();
    }
    /** Extend only after SessionManager's existing writer fence appended these exact bytes. */
    appendCoordinates(entry) {
        const before = this.#extent;
        const current = fstatSync(this.#fd, { bigint: true });
        const encoded = Buffer.from(JSON.stringify(entry) + '\n');
        if (Number(current.size) !== before + encoded.length ||
            revision(statSync(this.file, { bigint: true })) !== revision(current))
            throw new Error('Native append extent changed');
        const saved = Buffer.allocUnsafe(encoded.length);
        if (readSync(this.#fd, saved, 0, saved.length, before) !== saved.length || !saved.equals(encoded))
            throw new Error('Native appended entry differs');
        this.#extent = Number(current.size);
        this.#revision = revision(current);
        return [before, before, encoded.length - 1];
    }
    storeAppended(entry, meta) {
        if (this.knownMetadata(meta.id)) throw new Error('Duplicate native entry identity');
        this.#insert.run(meta.sequence, meta.id, meta.parentId, meta.type, meta.offset, meta.length, meta.inputId, JSON.stringify(meta));
        this.#last = meta.id;
    }
    committedAppend(entry, file, write) {
        return this.#writeIndex(() => {
            if (write) write();
            this.append(entry);
            this.#db.prepare('UPDATE source SET revision=?').run(this.#revision);
            return this;
        });
    }
    storedAt(file) {
        if (resolve(file) !== this.file) throw new Error('Native storage identity differs');
        this.assertCurrent(); return this;
    }
    close() {
        this.invalidate();
        this.#lines?.return();
        this.#lines = undefined;
        const database = this.#db, descriptor = this.#fd;
        this.#db = undefined; this.#fd = undefined;
        try { database?.close(); }
        finally { if (descriptor !== undefined) closeSync(descriptor); }
    }
}
