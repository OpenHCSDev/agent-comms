/** Read-only observations of the original SDK provider context. */
export interface NativeContextSegmentManifest {
    kind: string;
    provenance: readonly unknown[];
    sha256: string;
    utf8_bytes: number;
    tokens: number;
    contributors?: readonly NativeContextSegmentManifest[];
}
export interface InputContributionCoordinates {
    kind: string;
    provenance: readonly unknown[];
    offset: number;
    length: number;
    sha256: string;
    images: readonly number[];
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
}
export interface NativeContextData {
    identity: {sessionId: string; sessionFile: string};
    counter: string;
    segments: readonly (NativeContextSegmentManifest &
        ({content: string} | {messages: readonly unknown[]} | {tools: readonly unknown[]}))[];
}
