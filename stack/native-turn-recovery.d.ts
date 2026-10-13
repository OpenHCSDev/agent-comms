import type { Agent, AgentEvent, AgentMessage, StreamFn } from "@earendil-works/pi-agent-core";
import type { AssistantMessage } from "@earendil-works/pi-ai/compat";
import type { AgentSessionEvent } from "./agent-session.js";
import type { NativeInputClaim } from "./turn-context.js";

/** The committed native run owns production, retry policy and attempt lifetime. */
export declare class NativeTurnRun {
    constructor(agent?: Agent, emit?: (event: AgentSessionEvent) => void);
    get tracked(): boolean;
    get retrying(): boolean;
    include(messages: readonly AgentMessage[], claims: ReadonlyMap<string, NativeInputClaim>): this;
    observe(event: AgentEvent, claims: ReadonlyMap<string, NativeInputClaim>): void;
    stream(original: StreamFn, ...request: Parameters<StreamFn>): ReturnType<StreamFn>;
    willRetry(message: AssistantMessage): boolean;
    recover(message: AssistantMessage): Promise<boolean>;
    cancel(): void;
    close(): void;
}
