import type { SessionManager } from './session-manager.js';

/** One proof-resource option shared by SDK and runtime configuration. */
export interface NativeProofConfiguration {
    nativeInputProof?: NativeProof;
}

/** A selected resource; absence does not grant native input capability. */
export abstract class NativeProof {
    static fromPath(file: string | undefined, sessionId: string): NativeProof;
    available(manager: SessionManager): boolean;
    abstract recover(manager: SessionManager): number;
    abstract require(manager: SessionManager): NativeProof;
}
