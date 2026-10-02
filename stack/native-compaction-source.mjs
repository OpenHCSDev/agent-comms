/** Compaction source traversal and model-sized serialization; no history-sized arrays. */
import { closeSync, mkdirSync, mkdtempSync, openSync, readSync, rmSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { convertToLlm } from '../messages.js';
import { sessionEntryToContextMessages } from '../session-manager.js';
import { serializeConversation } from './utils.js';
import { estimateTextTokens } from '@earendil-works/pi-ai/utils/estimate';

export class EntryMessageRange {
    constructor(store, leafId, start, end) { Object.assign(this, {store, leafId, start, end}); }
    *[Symbol.iterator]() {
        for (const meta of this.store.branchMetadata(this.leafId)) {
            if (meta.sequence < this.start || meta.sequence >= this.end || meta.type === 'compaction') continue;
            const messages = sessionEntryToContextMessages(this.store.get(meta.id));
            if (messages.length) yield messages[0];
        }
    }
    isEmpty() {
        const iterator = this[Symbol.iterator]();
        try { return iterator.next().done; } finally { iterator.return?.(); }
    }
    *prefixMessages() {
        // The original context owner places the previous compaction first,
        // even though that record was appended after its retained messages.
        // Stop at this source's end; never reconstruct the old summary envelope.
        for (const meta of this.store.contextMetadata(this.leafId)) {
            if (meta.type !== 'compaction' && meta.sequence >= this.end) return;
            yield* sessionEntryToContextMessages(this.store.get(meta.id));
        }
    }
}

export class SummarySource {
    constructor() { if (new.target === SummarySource) throw new TypeError('Concrete summary source required'); }
    *pieces() { throw new Error('SummarySource.pieces must be implemented'); }
    byteLength() { let bytes = 0; for (const piece of this.pieces()) bytes += Buffer.byteLength(piece); return bytes; }
    tokenLength() { let tokens = 0; for (const piece of this.pieces()) tokens += estimateTextTokens(piece); return tokens; }
    *chunks(tokenLimit) {
        if (!Number.isSafeInteger(tokenLimit) || tokenLimit < 1) throw new Error('Native policy source budget required');
        let chunk = '';
        for (const piece of this.pieces()) {
            const bytes = Buffer.from(piece);
            let offset = 0;
            while (offset < bytes.length) {
                let lower = offset, upper = bytes.length;
                // Native's shared text estimator owns capacity. UTF-8 offsets
                // own exact source traversal, never a second token formula.
                while (lower < upper) {
                    const middle = Math.ceil((lower + upper) / 2);
                    let end = middle;
                    while (end > offset && end < bytes.length && (bytes[end] & 0xc0) === 0x80) end--;
                    const text = bytes.subarray(offset, end).toString('utf8');
                    if (estimateTextTokens(chunk + text) <= tokenLimit) lower = middle;
                    else upper = middle - 1;
                }
                let end = lower;
                while (end > offset && end < bytes.length && (bytes[end] & 0xc0) === 0x80) end--;
                if (end === offset) {
                    if (!chunk) throw new Error('Native source cannot admit one Unicode scalar');
                    yield chunk; chunk = ''; continue;
                }
                chunk += bytes.subarray(offset, end).toString('utf8');
                offset = end;
                if (offset < bytes.length) { yield chunk; chunk = ''; }
            }
        }
        if (chunk) yield chunk;
    }
    close() {}
}

export class HistorySummarySource extends SummarySource {
    #consumedBytes = 0;
    constructor(messages, previousSummary) {
        super(); this.messages = messages; this.previousSummary = previousSummary;
        this.sourceBytes = this.byteLength();
    }
    get consumedBytes() { return this.#consumedBytes; }
    get summaryPhase() { return 'history'; }
    summaryInstructions(instructions) { return instructions; }
    consume(bytes) { this.#consumedBytes += bytes; }
    complete() { this.#consumedBytes = this.sourceBytes; }
    async requestContext(policy, model, reserveTokens, instructions, systemPrompt, options, summaryPrefix) {
        // Capability absence/oversize selects the bounded source BEFORE any
        // provider request. A failed or uncertain request never selects again.
        const prefix = await summaryPrefix?.(this.messages, `${systemPrompt}\n\n${instructions}`, options);
        if (prefix && policy.requestFits(prefix.context, model, reserveTokens)) return prefix;
        const context = { systemPrompt, messages: [{ role: 'user',
            content: [{ type: 'text', text: this.boundedPrompt(instructions) }], timestamp: Date.now() }] };
        policy.requireRequest(context, model, reserveTokens);
        return { context, options };
    }
    boundedPrompt(instructions) {
        let prompt = `<conversation>\n${[...this.historyPieces()].join('')}\n</conversation>\n\n`;
        if (this.previousSummary) prompt += `<previous-summary>\n${this.previousSummary}\n</previous-summary>\n\n`;
        return prompt + instructions;
    }
    *pieces() {
        if (this.previousSummary) {
            yield `<previous-summary>\n${this.previousSummary}\n</previous-summary>`;
        }
        yield* this.historyPieces(Boolean(this.previousSummary));
    }
    *historyPieces(emitted = false) {
        for (const message of this.messages) {
            const text = serializeConversation(convertToLlm([message]));
            if (!text) continue;
            if (emitted) yield '\n\n';
            yield text; emitted = true;
        }
    }
}

/** Intermediate model results are temporary job data on owned persistent disk. */
export class ReducedSummarySource extends SummarySource {
    #directory; #size = 0; #count = 0;
    constructor() {
        super();
        const root = process.env.AGENT_COMMS_SESSION_INDEX_DIR ?? join(homedir(), '.cache', 'agent-comms', 'session-indexes');
        mkdirSync(root, {recursive:true, mode:0o700});
        this.#directory = mkdtempSync(join(root,'summary-'));
    }
    get count() { return this.#count; }
    append(text, index = this.#count) {
        const framed = `${index ? '\n\n' : ''}<segment-summary index="${index + 1}">\n${text}\n</segment-summary>`;
        const data = Buffer.from(framed);
        writeFileSync(join(this.#directory, String(index)), data, {flag: 'wx', mode: 0o600});
        this.#size += data.length;
        this.#count = Math.max(this.#count, index + 1);
    }
    byteLength() { return this.#size; }
    *pieces() {
        const decoder = new TextDecoder('utf8',{fatal:true});
        const buffer = Buffer.allocUnsafe(64*1024);
        for (let index = 0; index < this.#count; index++) {
            const fd = openSync(join(this.#directory, String(index)), 'r');
            try {
                for (;;) {
                    const n = readSync(fd, buffer, 0, buffer.length, null);
                    if (!n) break;
                    yield decoder.decode(buffer.subarray(0,n), {stream:true});
                }
            } finally { closeSync(fd); }
        }
        const final=decoder.decode();if(final)yield final;
    }
    close() {
        if(this.#directory)rmSync(this.#directory,{recursive:true,force:true});this.#directory=undefined;
    }
}
