/** Diagnostic request coordinates. Not an admission or retry receipt. */
export interface RequestObservationPoint {
    stage: string;
    detail?: string;
    transport?: string;
    attempt?: number;
    status?: number;
    responseId?: string;
    callback?: string;
}
export interface NativeRequestProgress extends RequestObservationPoint {
    requestId: string;
    sessionId: string;
    inputId: string;
    startedAtMs: number;
    observedAtMs: number;
    monotonicNs: string;
    elapsedMs: number;
    callbackMs: number;
    callbackCount: number;
    callbackMaxMs: number;
}
export type RequestObserver = (point: RequestObservationPoint) => void;
export declare function observeRequest(options: { onRequestProgress?: RequestObserver } | undefined,
                                      point: RequestObservationPoint): void;
export declare class NativeRequestObservation {
    constructor(config: { sessionId?: string; onRequestProgress?: (progress: NativeRequestProgress) => void },
                context: { messages: readonly { role: string; inputId?: string }[] });
    observe(point: RequestObservationPoint): void;
    callback<T>(name: string, action: () => T | Promise<T>, announce?: boolean, detail?: string): Promise<T>;
    options<T>(config: T): T & { onRequestProgress: RequestObserver };
    emit<T>(event: { type: string }, publish: (event: { type: string }) => T | Promise<T>): Promise<T>;
}
