/** Diagnostic request coordinates. Not an admission or retry receipt. */
import type { Api, Model } from "../types.js";

export interface IteratorTiming {
    streamId: string;
    transport: string;
    phase: "iteratorAwait" | "consumerYield" | "closed";
    startedMonotonicNs: string;
    observedMonotonicNs: string;
    iteratorAwaitMs: number;
    consumerYieldMs: number;
    eventCount: number;
}
export interface RequestObservationPoint {
    iteratorTiming?: IteratorTiming;
    stage: string;
    detail?: string;
    transport?: string;
    attempt?: number;
    status?: number;
    responseId?: string;
    callback?: string;
    model?: Pick<Model<Api>, "provider" | "id" | "name" | "contextWindow" | "maxTokens">;
    estimatedInputTokens?: number;
    availableTokens?: number;
    outputTokenField?: string;
    requestedOutputTokens?: number;
    admittedOutputTokens?: number;
    minimumOutputTokens?: number;
}
export interface NativeRequestProgress extends RequestObservationPoint {
    requestId: string;
    sessionId: string;
    startedAtMs: number;
    observedAtMs: number;
    monotonicNs: string;
    elapsedMs: number;
    callbackMs: number;
    callbackCount: number;
    callbackMaxMs: number;
}
export type RequestObserver = (point: RequestObservationPoint, iterator?: NativeIteratorObservation) => void;
export declare function observeRequest(options: { onRequestProgress?: RequestObserver } | undefined,
                                      point: RequestObservationPoint, iterator?: NativeIteratorObservation): void;
export declare class NativeRequestObservation {
    constructor(config: { sessionId?: string; onRequestProgress?: (progress: NativeRequestProgress) => void });
    observe(point: RequestObservationPoint, iterator?: NativeIteratorObservation): void;
    callback<T>(name: string, action: () => T | Promise<T>, announce?: boolean, detail?: string): Promise<T>;
    options<T>(config: T): T & { onRequestProgress: RequestObserver };
    events<T extends { type: string }>(events: AsyncIterable<T>): AsyncGenerator<T>;
    emit<T>(event: { type: string }, publish: (event: { type: string }) => T | Promise<T>): Promise<T>;
}
export declare function observeStream<T>(options: { onRequestProgress?: RequestObserver } | undefined,
    events: AsyncIterable<T>, transport: string): AsyncGenerator<T>;
/** Only observeStream supplies this already-started, read-only source. */
declare class NativeIteratorObservation {
    private constructor();
    snapshot(now?: bigint): IteratorTiming;
}
