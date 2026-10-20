import type { Agent, AgentEvent, AgentMessage, StreamFn } from "@earendil-works/pi-agent-core";
import type { AssistantMessage } from "@earendil-works/pi-ai/compat";
import type { AgentSessionEvent } from "./agent-session.js";
import type { NativeInputClaim } from "./turn-context.js";

/** Explicit absence of an active native run. */
export declare class InactiveNativeTurnRun {
    static readonly shared: InactiveNativeTurnRun;
    readonly ownerTools: import('./turn-context.js').OwnerToolConnection;
    readonly tracked: false;
    readonly retrying: false;
    include(messages: readonly AgentMessage[], claims: ReadonlyMap<string, NativeInputClaim>): this;
    observe(event: AgentEvent, claims: ReadonlyMap<string, NativeInputClaim>): void;
    stream(original: StreamFn, ...request: Parameters<StreamFn>): ReturnType<StreamFn>;
    willRetry(): false;
    recover(message: AssistantMessage): Promise<false>;
    cancel(): void;
    close(): void;
}

/** The committed native run owns production, retry policy and attempt lifetime. */
export declare class NativeTurnRun {
    constructor(agent: Agent, emit: (event: AgentSessionEvent) => void,
                retryable: (message: AssistantMessage) => boolean, backoffMs?: readonly number[]);
    finishRetry(success: boolean, finalError?: string): void;
    readonly ownerTools: import('./turn-context.js').OwnerToolConnection;
    get tracked(): boolean;
    get retrying(): boolean;
    include(messages: readonly AgentMessage[], claims: ReadonlyMap<string, NativeInputClaim>): this;
    observe(event: AgentEvent, claims: ReadonlyMap<string, NativeInputClaim>): void;
    stream(original: StreamFn, ...request: Parameters<StreamFn>): ReturnType<StreamFn>;
    willRetry(): boolean;
    recover(message: AssistantMessage): Promise<boolean>;
    cancel(): void;
    close(): void;
}
