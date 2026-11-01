/** One model request owns its progress and callback observations. */
import { randomUUID } from "node:crypto";

export function observeRequest(options, record, iterator) {
    // Transport work must not depend on a presentation observer succeeding.
    try { options?.onRequestProgress?.(record, iterator); } catch { /* observer only */ }
}

export class NativeRequest {
    constructor(config) {
        this.observer = config.onRequestProgress;
        this.requestId = randomUUID();
        this.sessionId = config.sessionId ?? "";
        this.started = process.hrtime.bigint();
        this.startedAtMs = Date.now();
        this.callbackNs = 0n;
        this.callbackCount = 0;
        this.callbackMaxNs = 0n;
    }

    // Retain the actual last-observed stream, including its closed state across
    // retries. This is not an aggregate that merges distinct transport sources.
    observe = (record, iterator = this.iterator) => {
        this.iterator = iterator;
        const now = process.hrtime.bigint();
        const progress = { requestId: this.requestId, sessionId: this.sessionId,
            startedAtMs: this.startedAtMs,
            observedAtMs: Date.now(), monotonicNs: now.toString(),
            elapsedMs: Number(now - this.started) / 1e6,
            callbackMs: Number(this.callbackNs) / 1e6,
            callbackCount: this.callbackCount, callbackMaxMs: Number(this.callbackMaxNs) / 1e6,
            ...record,
            ...(iterator && { iteratorTiming: iterator.snapshot(now) }) };
        observeRequest({ onRequestProgress: this.observer }, progress);
    };

    async callback(name, action, announce = true, detail = "Publishing model response") {
        const started = process.hrtime.bigint();
        if (announce) this.observe({ stage: "callback", callback: name,
            detail });
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
                () => config.onPayload?.(payload, model), true, "Preparing model request"),
            onResponse: (response, model) => this.callback("after_provider_response",
                () => config.onResponse?.(response, model), true, "Reading provider response") };
    }

    async *events(events) {
        let firstDelta = true;
        for await (const event of events) {
            if (firstDelta && event.type.endsWith("_delta")) {
                firstDelta = false;
                this.observe({ stage: "first_delta_consumed", detail: "Receiving model response" });
            }
            yield event;
        }
    }

    async emit(event, publish) {
        return this.callback(event.type, () => publish(event), event.type !== "message_update");
    }
}

/** One original iterator owns its cumulative custody durations. These are wall
 * times, not CPU times: iterator await includes transport/decode/scheduling and
 * return() cleanup; consumer yield includes projection and downstream awaits.
 * No event payloads are inspected or retained. A snapshot includes the open
 * interval, so a stalled next() or consumer is visible before stream closure.
 */
class NativeIteratorObservation {
    constructor(transport) {
        this.streamId = randomUUID();
        this.transport = transport;
        this.phase = "iteratorAwait";
        this.elapsed = { iteratorAwait: 0n, consumerYield: 0n, closed: 0n };
        this.eventCount = 0;
    }

    transition(phase) {
        const now = process.hrtime.bigint();
        this.elapsed[this.phase] += now - this.changed;
        this.changed = now;
        this.phase = phase;
    }

    snapshot(now = process.hrtime.bigint()) {
        const elapsed = { ...this.elapsed };
        elapsed[this.phase] += now - this.changed;
        return { streamId: this.streamId, transport: this.transport, phase: this.phase,
            startedMonotonicNs: this.started.toString(), observedMonotonicNs: now.toString(),
            iteratorAwaitMs: Number(elapsed.iteratorAwait) / 1e6,
            consumerYieldMs: Number(elapsed.consumerYield) / 1e6,
            eventCount: this.eventCount };
    }

    async *events(options, events) {
        this.started = process.hrtime.bigint();
        this.changed = this.started;
        try {
            for await (const event of events) {
                this.transition("consumerYield");
                this.eventCount++;
                if (this.eventCount === 1) this.observe(options, "first_event", "Provider stream started");
                try { yield event; }
                finally { this.transition("iteratorAwait"); }
            }
        } finally {
            this.transition("closed");
            this.observe(options, "stream_end", "Provider stream closed");
        }
    }

    observe(options, stage, detail) {
        observeRequest(options, { stage, transport: this.transport, detail,
            iteratorTiming: this.snapshot() }, this);
    }
}

/** End is iterator custody release (including failure/cancel), not commit proof. */
export function observeStream(options, events, transport) {
    return new NativeIteratorObservation(transport).events(options, events);
}
