/** Compaction source traversal and model-sized serialization; no history-sized arrays. */
import { closeSync, mkdirSync, mkdtempSync, openSync, readSync, rmSync, unlinkSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { convertToLlm } from '../messages.js';
import { sessionEntryToContextMessages } from '../session-manager.js';
import { serializeConversation } from './utils.js';

export class EntryMessageRange {
    constructor(store, leafId, start, end) { Object.assign(this, {store, leafId, start, end}); }
    *[Symbol.iterator]() {
        for (const meta of this.store.branchMetadata(this.leafId)) {
            if (meta.sequence < this.start || meta.sequence >= this.end || meta.type === 'compaction') continue;
            const messages = sessionEntryToContextMessages(this.store.get(meta.id));
            if (messages.length) yield messages[0];
        }
    }
    isEmpty() { return this[Symbol.iterator]().next().done; }
}

export class SummarySource {
    constructor() { if (new.target === SummarySource) throw new TypeError('Concrete summary source required'); }
    *pieces() { throw new Error('SummarySource.pieces must be implemented'); }
    byteLength() { let bytes = 0; for (const piece of this.pieces()) bytes += Buffer.byteLength(piece); return bytes; }
    *chunks(byteLimit) {
        if (!Number.isSafeInteger(byteLimit) || byteLimit < 4) throw new Error('Native policy source budget required');
        let chunk = '', size = 0;
        for (const piece of this.pieces()) {
            const bytes = Buffer.from(piece);
            let offset = 0;
            while (offset < bytes.length) {
                let end = Math.min(offset + byteLimit - size, bytes.length);
                while (end > offset && end < bytes.length && (bytes[end] & 0xc0) === 0x80) end--;
                if (end === offset) { yield chunk; chunk = ''; size = 0; continue; }
                chunk += bytes.subarray(offset, end).toString('utf8');
                size += end - offset; offset = end;
                if (size === byteLimit) { yield chunk; chunk = ''; size = 0; }
            }
        }
        if (chunk) yield chunk;
    }
    close() {}
}

export class HistorySummarySource extends SummarySource {
    constructor(messages, previousSummary) { super(); this.messages = messages; this.previousSummary = previousSummary; }
    *pieces() {
        let emitted = false;
        for (const message of this.messages) {
            const text = serializeConversation(convertToLlm([message]));
            if (!text) continue;
            if (emitted) yield '\n\n';
            yield text; emitted = true;
        }
        if (this.previousSummary) yield `\n\n<previous-summary>\n${this.previousSummary}\n</previous-summary>`;
    }
}

/** Intermediate model results are temporary job data on owned persistent disk. */
export class ReducedSummarySource extends SummarySource {
    #directory; #fd; #size = 0; #count = 0;
    constructor() {
        super();
        const root = process.env.AGENT_COMMS_SESSION_INDEX_DIR ?? join(homedir(), '.cache', 'agent-comms', 'session-indexes');
        mkdirSync(root, {recursive:true, mode:0o700});
        this.#directory = mkdtempSync(join(root,'summary-'));
        const path = join(this.#directory,'segments');
        this.#fd = openSync(path, 'wx+',0o600);
        unlinkSync(path);
        rmSync(this.#directory);
        this.#directory = undefined;
    }
    get count() { return this.#count; }
    append(text) {
        const framed = `${this.#count ? '\n\n' : ''}<segment-summary index="${++this.#count}">\n${text}\n</segment-summary>`;
        const data = Buffer.from(framed); writeFileSync(this.#fd,data); this.#size += data.length;
    }
    byteLength() { return this.#size; }
    *pieces() {
        const decoder = new TextDecoder('utf8',{fatal:true});
        const buffer = Buffer.allocUnsafe(64*1024);
        let position=0;
        while (position < this.#size) {
            const n=readSync(this.#fd,buffer,0,Math.min(buffer.length,this.#size-position),position);
            if (!n) throw new Error('Incomplete compaction reduction source');
            yield decoder.decode(buffer.subarray(0,n),{stream:true});position+=n;
        }
        const final=decoder.decode();if(final)yield final;
    }
    close() {
        if(this.#fd !== undefined) {closeSync(this.#fd);this.#fd=undefined;}
        if(this.#directory)rmSync(this.#directory,{recursive:true,force:true});this.#directory=undefined;
    }
}
