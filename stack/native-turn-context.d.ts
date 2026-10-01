/** Read-only observations of the original SDK provider context. */
export interface NativeContextSegmentManifest {
    kind: string;
    provenance: readonly unknown[];
    sha256: string;
    utf8_bytes: number;
    tokens: number;
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
