/** Request timing is a diagnostic resource, never admission or retry authority. */
import { randomUUID } from "node:crypto";

export function observeRequest(options, record) {
    // Transport work must not depend on a presentation observer succeeding.
    try { options?.onRequestProgress?.(record); } catch { /* observer only */ }
}

export class NativeRequestObservation {
    constructor(config, context) {
        this.observer = config.onRequestProgress;
        this.requestId = randomUUID();
        this.sessionId = config.sessionId ?? "";
        this.inputId = context.messages.findLast(message => message.role === "user")?.inputId ?? "";
        this.started = process.hrtime.bigint();
        this.startedAtMs = Date.now();
        this.callbackNs = 0n;
        this.callbackCount = 0;
        this.callbackMaxNs = 0n;
    }

    observe = (record) => {
        const now = process.hrtime.bigint();
        const progress = { requestId: this.requestId, sessionId: this.sessionId,
            inputId: this.inputId, startedAtMs: this.startedAtMs,
            observedAtMs: Date.now(), monotonicNs: now.toString(),
            elapsedMs: Number(now - this.started) / 1e6,
            callbackMs: Number(this.callbackNs) / 1e6,
            callbackCount: this.callbackCount, callbackMaxMs: Number(this.callbackMaxNs) / 1e6,
            ...record };
        observeRequest({ onRequestProgress: this.observer }, progress);
    };

    async callback(name, action, announce = true) {
        const started = process.hrtime.bigint();
        if (announce) this.observe({ stage: "callback", callback: name,
            detail: `Applying native callback: ${name}` });
        try { return await action(); }
        finally {
            const duration = process.hrtime.bigint() - started;
            this.callbackNs += duration;
            this.callbackCount++;
            if (duration > this.callbackMaxNs) this.callbackMaxNs = duration;
            if (announce) this.observe({ stage: "callback_end", callback: name,
                detail: "Waiting for model progress" });
        }
    }

    options(config) {
        return { ...config, onRequestProgress: this.observe,
            onPayload: (payload, model) => this.callback("before_provider_request",
                () => config.onPayload?.(payload, model)),
            onResponse: (response, model) => this.callback("after_provider_response",
                () => config.onResponse?.(response, model)) };
    }

    async emit(event, publish) {
        return this.callback(event.type, () => publish(event), event.type !== "message_update");
    }
}
