import type { AgentMessage } from '@earendil-works/pi-agent-core';
import type { EntryStore } from '../session-entry-store.js';
import type { Api, Context, Model, ProviderStreams, SimpleStreamOptions } from '@earendil-works/pi-ai';
import type { CompactionPolicy } from './agent-comms-policy.js';

export declare class EntryMessageRange implements Iterable<AgentMessage> {
    constructor(store: EntryStore, leafId: string | null, start: number, end: number);
    [Symbol.iterator](): IterableIterator<AgentMessage>;
    isEmpty(): boolean;
    prefixMessages(): IterableIterator<AgentMessage>;
}
export declare abstract class SummarySource {
    abstract pieces(): IterableIterator<string>;
    byteLength(): number;
    tokenLength(): number;
    chunks(tokenLimit: number): IterableIterator<string>;
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
    requestContext(policy: CompactionPolicy,
        model: Model<Api>, reserveTokens: number, instructions: string, systemPrompt: string,
        options: SimpleStreamOptions,
        summaryPrefix?: (messages: Iterable<AgentMessage>, instructions: string, options: SimpleStreamOptions) =>
            ReturnType<NonNullable<ProviderStreams['summaryPrefix']>>):
        Promise<NonNullable<Awaited<ReturnType<NonNullable<ProviderStreams['summaryPrefix']>>>>>;
    boundedPrompt(instructions: string): string;
    historyPieces(emitted?: boolean): IterableIterator<string>;
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
