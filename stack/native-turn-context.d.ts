/** Read-only observations of the original SDK provider context. */
export interface NativeContextSegmentManifest {
    kind: string;
    provenance: readonly unknown[];
    sha256: string;
    utf8_bytes: number;
    tokens: number;
    contributors?: readonly NativeContextSegmentManifest[];
    captured_text?: readonly string[];
    source_spans?: readonly ContributionCoordinates[];
}
export interface ContributionCoordinates {
    kind: string;
    provenance: readonly unknown[];
    offset: number;
    length: number;
    sha256: string;
}
export interface InputContributionCoordinates extends ContributionCoordinates {
    images: readonly number[];
}
export declare class SystemLayerSegment {
    readonly value: string;
    readonly sourceSpans: readonly ContributionCoordinates[];
    static unattributed(value: string): SystemLayerSegment;
    static fromFile(path: string, raw: Uint8Array, content?: string): SystemLayerSegment;
    static fromResource(path: string, raw: Uint8Array, representation: string): SystemLayerSegment;
    append(content: string, provenance?: readonly unknown[]): void;
    extend(segment: SystemLayerSegment): void;
    replaced(content: string): SystemLayerSegment;
    sourceFile(): {kind: 'file'; path: string; sha256: string} | undefined;
}
export declare class NativeInputClaim {
    readonly digest: string;
    private readonly contributions;
    static capture(digest: string, request: {text: string; images?: readonly unknown[] | null}, contributions?: readonly InputContributionCoordinates[]): NativeInputClaim;
    observe(message: unknown, native: unknown, journal: unknown): NativeContextSegmentManifest[];
}
export interface NativeContextManifest {
    counter: string;
    segments: readonly NativeContextSegmentManifest[];
    requestId?: string;
    values?: NativeContextData['segments'];
}
export interface NativeContextData {
    identity: {sessionId: string; sessionFile: string};
    counter: string;
    segments: readonly (NativeContextSegmentManifest &
        ({content: string} | {messages: readonly unknown[]} | {tools: readonly unknown[]}))[];
}
