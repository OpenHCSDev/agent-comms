import type { FileEntry, SessionEntry, SessionHeader } from './session-manager.js';
import type { Usage } from '@earendil-works/pi-ai/compat';

export declare abstract class StoreAvailability {
    abstract requireOpen(): void;
    closed(cause?: unknown): StoreAvailability;
}
export declare class AvailableStore extends StoreAvailability {
    requireOpen(): void;
}
export declare class UnavailableStore extends StoreAvailability {
    constructor(cause?: unknown);
    requireOpen(): never;
    closed(): StoreAvailability;
}

export declare class EntryMetadata {
    constructor(entry: SessionEntry, sequence: number, offset?: number, length?: number);
    id: string;
    parentId: string | null;
    type: SessionEntry['type'];
    sequence: number;
    offset: number;
    length: number;
    role: string | null;
    inputId: string | null;
    inputDigest: string | null;
    firstKeptEntryId: string | null;
    contextMessageCount: number;
    usage: Usage | undefined;
    toolCallCount: number;
    model: {provider: string; modelId: string} | null;
    thinkingLevel: string | null;
    label: {targetId: string; label: string | null; timestamp: string} | null;
}

export declare abstract class EntryStore {
    abstract get header(): SessionHeader;
    abstract get lastId(): string | null;
    abstract metadata(id: string | null): EntryMetadata | undefined;
    abstract get(id: string): SessionEntry | undefined;
    abstract metadataEntries(): IterableIterator<EntryMetadata>;
    abstract branchMetadata(leafId?: string | null): IterableIterator<EntryMetadata>;
    abstract append(entry: SessionEntry): void;
    abstract committedAppend(entry: SessionEntry, file?: string): EntryStore;
    abstract storedAt(file: string): EntryStore;
    abstract close(): void;
    assertCurrent(): void;
    assertUsable(): void;
    invalidate(cause?: unknown): void;
    has(id: string): boolean;
    validate(entry: SessionEntry): void;
    static validateHeader(header: SessionHeader): SessionHeader;
    entries(): IterableIterator<SessionEntry>;
    fileEntries(): IterableIterator<FileEntry>;
    branch(leafId?: string | null): IterableIterator<SessionEntry>;
    ancestors(leafId?: string | null): IterableIterator<EntryMetadata>;
    branchContains(leafId: string | null, id: string): boolean;
    commonAncestor(leftId: string | null, rightId: string | null): string | null;
    latest(leafId: string | null, type: SessionEntry['type']): SessionEntry | undefined;
    latestMetadata(leafId: string | null, type: SessionEntry['type']): EntryMetadata | undefined;
    contextSettings(leafId?: string | null): {model: EntryMetadata['model']; thinkingLevel: string};
    contextEntries(leafId?: string | null): IterableIterator<SessionEntry>;
    contextMetadata(leafId?: string | null): IterableIterator<EntryMetadata>;
    contextMessageCount(leafId?: string | null): number;
    trackedMetadata(): IterableIterator<EntryMetadata>;
    trackedInputMetadata(inputId: string): EntryMetadata | undefined;
    trackedInputs(): IterableIterator<SessionEntry>;
    trackedInput(inputId: string): SessionEntry | undefined;
    children(parentId: string | null): IterableIterator<SessionEntry>;
    label(id: string): EntryMetadata['label'] | undefined;
}

export declare class MemoryEntryStore extends EntryStore {
    constructor(header: SessionHeader, entries?: Iterable<SessionEntry>);
    get header(): SessionHeader;
    get lastId(): string | null;
    metadata(id: string | null): EntryMetadata | undefined;
    get(id: string): SessionEntry | undefined;
    metadataEntries(): IterableIterator<EntryMetadata>;
    branchMetadata(leafId?: string | null): IterableIterator<EntryMetadata>;
    append(entry: SessionEntry): void;
    committedAppend(entry: SessionEntry, file?: string): EntryStore;
    storedAt(file: string): DiskEntryStore;
    close(): void;
}

export declare class DiskEntryStore extends EntryStore {
    constructor(file: string, options?: {indexDirectory?: string});
    get header(): SessionHeader;
    get lastId(): string | null;
    get revision(): string;
    metadata(id: string | null): EntryMetadata | undefined;
    get(id: string): SessionEntry | undefined;
    metadataEntries(): IterableIterator<EntryMetadata>;
    branchMetadata(leafId?: string | null): IterableIterator<EntryMetadata>;
    append(entry: SessionEntry): void;
    committedAppend(entry: SessionEntry, file?: string): EntryStore;
    storedAt(file: string): EntryStore;
    close(): void;
}

export declare class EntryBranchRange implements Iterable<SessionEntry> {
    constructor(store: EntryStore, leafId: string | null, ancestorId?: string | null);
    [Symbol.iterator](): IterableIterator<SessionEntry>;
    reverse(): IterableIterator<SessionEntry>;
    isEmpty(): boolean;
}
