import type { FileEntry, SessionEntry, SessionHeader, SessionTreeNode } from './session-manager.js';

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
    static initialSettings: {model: EntryMetadata['model']; thinkingLevel: string};
    contextSettings?: {model: EntryMetadata['model']; thinkingLevel: string};
    inheritSettings(parent?: {model: EntryMetadata['model']; thinkingLevel: string}): EntryMetadata;
    constructor(entry: SessionEntry, sequence: number, offset?: number, length?: number, parentSequence?: number);
    id: string;
    parentId: string | null;
    type: SessionEntry['type'];
    timestamp: string;
    readonly parentSequence?: number;
    sequence: number;
    offset: number;
    length: number;
    role: string | null;
    inputId: string | null;
    inputDigest: string | null;
    firstKeptEntryId: string | null;
    contextMessageCount: number;
    model: {provider: string; modelId: string} | null;
    thinkingLevel: string | null;
    label: {targetId: string; label: string | null; timestamp: string} | null;
    static labelFor(entry: SessionEntry): EntryMetadata['label'];
    static liveLabel(label: EntryMetadata['label']): EntryMetadata['liveLabel'];
    get liveLabel(): (NonNullable<EntryMetadata['label']> & {label: string}) | undefined;
}

export declare abstract class EntryStore {
    abstract get header(): SessionHeader;
    abstract get lastId(): string | null;
    abstract metadata(id: string | null): EntryMetadata | undefined;
    abstract get(id: string): SessionEntry | undefined;
    abstract metadataEntries(): IterableIterator<EntryMetadata>;
    abstract chronologicalMetadata(firstId?: string | null): IterableIterator<EntryMetadata>;
    branchMetadata(leafId?: string | null, firstId?: string | null): IterableIterator<EntryMetadata>;
    append(entry: SessionEntry): void;
    abstract appendCoordinates(entry: SessionEntry): [number, number, number];
    abstract storeAppended(entry: SessionEntry, metadata: EntryMetadata): void;
    knownMetadata(id: string | null): EntryMetadata | undefined;
    settingsMetadata(id: string | null): EntryMetadata | undefined;
    metadataFor(entry: SessionEntry, sequence: number, offset?: number, length?: number, parent?: EntryMetadata): EntryMetadata;
    abstract committedAppend(entry: SessionEntry, file?: string, write?: () => string | undefined): EntryStore;
    abstract storedAt(file: string): EntryStore;
    abstract close(): void;
    assertCurrent(): void;
    assertUsable(): void;
    invalidate(cause?: unknown): void;
    has(id: string): boolean;
    validate(entry: SessionEntry): void;
    static validateHeader(header: SessionHeader): SessionHeader;
    entries(): IterableIterator<SessionEntry>;
    tree(): SessionTreeNode[];
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
    labels(): IterableIterator<NonNullable<EntryMetadata['liveLabel']>>;
    label(id: string): EntryMetadata['liveLabel'];
}

export declare class MemoryEntryStore extends EntryStore {
    constructor(header: SessionHeader, entries?: Iterable<SessionEntry>);
    get header(): SessionHeader;
    get lastId(): string | null;
    metadata(id: string | null): EntryMetadata | undefined;
    get(id: string): SessionEntry | undefined;
    metadataEntries(): IterableIterator<EntryMetadata>;
    chronologicalMetadata(firstId?: string | null): IterableIterator<EntryMetadata>;
    appendCoordinates(entry: SessionEntry): [number, number, number];
    storeAppended(entry: SessionEntry, metadata: EntryMetadata): void;
    committedAppend(entry: SessionEntry, file?: string, write?: () => string | undefined): EntryStore;
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
    chronologicalMetadata(firstId?: string | null): IterableIterator<EntryMetadata>;
    appendCoordinates(entry: SessionEntry): [number, number, number];
    storeAppended(entry: SessionEntry, metadata: EntryMetadata): void;
    committedAppend(entry: SessionEntry, file?: string, write?: () => string | undefined): EntryStore;
    storedAt(file: string): EntryStore;
    close(): void;
}

export declare class EntryBranchRange implements Iterable<SessionEntry> {
    constructor(store: EntryStore, leafId: string | null, ancestorId?: string | null);
    [Symbol.iterator](): IterableIterator<SessionEntry>;
    newestFirst(): IterableIterator<SessionEntry>;
    isEmpty(): boolean;
}
