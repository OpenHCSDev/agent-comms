import type { AgentMessage } from '@earendil-works/pi-agent-core';
import type { EntryStore } from '../session-entry-store.js';

export declare class EntryMessageRange implements Iterable<AgentMessage> {
    constructor(store: EntryStore, leafId: string | null, start: number, end: number);
    [Symbol.iterator](): IterableIterator<AgentMessage>;
    isEmpty(): boolean;
}
export declare abstract class SummarySource {
    abstract pieces(): IterableIterator<string>;
    byteLength(): number;
    chunks(byteLimit: number): IterableIterator<string>;
    close(): void;
}
export declare class HistorySummarySource extends SummarySource {
    constructor(messages: Iterable<AgentMessage>, previousSummary?: string);
    readonly sourceBytes: number;
    readonly consumedBytes: number;
    readonly summaryPhase: string;
    summaryInstructions(instructions?: string): string | undefined;
    consume(bytes: number): void;
    complete(): void;
    pieces(): IterableIterator<string>;
}
export declare class ReducedSummarySource extends SummarySource {
    constructor();
    get count(): number;
    append(text: string, index?: number): void;
    pieces(): IterableIterator<string>;
    byteLength(): number;
    close(): void;
}
