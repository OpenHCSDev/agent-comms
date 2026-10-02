/** Agent-comms summary policy. Pi owns its enabled/reserve/keep-recent settings.
 * This is the sole declaration of adapter defaults and accepted configuration.
 * Provider/model selection, context window, and no-replay are not policy knobs.
 */
import { ContextBudget, BudgetAdmissionError } from '@earendil-works/pi-ai/api/agent-comms-context-budget';
import { convertToLlm } from '../messages.js';

const strategies = Object.freeze({
    serial: Object.freeze({ plan: segments => new CompactionPlan(segments, 1) }),
    parallel: Object.freeze({ plan: (segments, policy) => new CompactionPlan(segments, policy.concurrency) }),
});

/** One bounded scheduler for every native map and reduction source. */
export class CompactionPlan {
    #running = new Set();
    #pending = [];
    #controller;
    constructor(segments, workers) {
        Object.assign(this, { segments, workers });
        this.startedAtMs = Math.floor(performance.timeOrigin + performance.now());
    }
    /** Source orchestration never holds a provider slot while awaiting children. */
    async execute(consume, controller, segments = this.segments) {
        // Retain the actual source scope's cancellation authority, not a copy.
        this.#controller ??= controller;
        const iterator = segments[Symbol.iterator]();
        let index = 0;
        const worker = async () => {
            try {
                while (!controller.signal.aborted) {
                    const next = iterator.next();
                    if (next.done) return;
                    await consume(next.value, index++);
                }
            } catch (error) { this.#controller.abort(error); controller.abort(error); }
        };
        try {
            await Promise.allSettled(Array.from({ length: this.workers }, worker));
            this.#controller.signal.throwIfAborted();
            controller.signal.throwIfAborted();
        } finally { iterator.return?.(); }
    }
    /** All leaf requests, including nested reductions, share this owned bound. */
    run(produce, signal) {
        signal?.throwIfAborted();
        return new Promise((resolve, reject) => {
            const job = { produce, signal, resolve, reject };
            job.abort = () => {
                this.#pending.splice(this.#pending.indexOf(job), 1);
                reject(signal.reason);
            };
            signal?.addEventListener('abort', job.abort, { once: true });
            this.#pending.push(job);
            this.#drain();
        });
    }
    #drain() {
        while (this.#pending.length && this.#running.size < this.workers) {
            const job = this.#pending.shift();
            job.signal?.removeEventListener('abort', job.abort);
            const request = Promise.resolve().then(() => {
                job.signal?.throwIfAborted();
                return job.produce();
            });
            this.#running.add(request);
            request.then(job.resolve, error => {
                this.#controller?.abort(error);
                job.reject(error);
            }).finally(() => {
                this.#running.delete(request);
                this.#drain();
            });
        }
    }
    progress(phase) {
        return {
            sourceBytesDone: this.segments.reduce((sum, source) => sum + source.consumedBytes, 0),
            sourceBytesTotal: this.segments.reduce((sum, source) => sum + source.sourceBytes, 0),
            summaryPhase: phase,
            startedAtMs: this.startedAtMs,
            observedAtMs: Math.floor(performance.timeOrigin + performance.now()),
        };
    }
}
export class CompactionPolicy {
    static declarations = Object.freeze({
        strategy: Object.freeze({ default: 'parallel', choices: Object.keys(strategies) }),
        concurrency: Object.freeze({ default: 4, integer: true, min: 1, max: 8 }),
        inputBudgetRatio: Object.freeze({ default: 0.75, min: 0.25, max: 0.9 }),
        sourceBudgetRatio: Object.freeze({ default: 0.7, min: 0.25, max: 0.8 }),
        summaryMaxTokens: Object.freeze({ default: 4096, integer: true, min: 256, max: 32768 }),
        summaryOutputRatio: Object.freeze({ default: 1 / 12, min: 0.01, max: 0.5 }),
        taskAware: Object.freeze({ default: false, choices: [false, true] }),
    });
    constructor(configuration = {}) {
        if (!configuration || typeof configuration !== 'object' || Array.isArray(configuration))
            throw new Error('Compaction policy must be an object');
        for (const key of Object.keys(configuration)) {
            if (!Object.hasOwn(CompactionPolicy.declarations, key))
                throw new Error(`Unknown compaction policy field: ${key}`);
        }
        for (const [key, rule] of Object.entries(CompactionPolicy.declarations)) {
            const value = Object.hasOwn(configuration, key) ? configuration[key] : rule.default;
            const valid = rule.choices ? rule.choices.includes(value)
                : typeof value === 'number' && Number.isFinite(value) &&
                    (!rule.integer || Number.isInteger(value)) && value >= rule.min && value <= rule.max;
            if (!valid) throw new Error(`Invalid compaction policy field: ${key}`);
            this[key] = value;
        }
        Object.freeze(this);
    }
    static fromEnvironment(env) {
        const value = env?.AGENT_COMMS_COMPACTION_POLICY ?? process.env.AGENT_COMMS_COMPACTION_POLICY;
        return new CompactionPolicy(value === undefined ? {} : JSON.parse(value));
    }
    plan(segments) {
        return Object.freeze(strategies[this.strategy].plan(segments, this));
    }
    decision(session, settings, purpose, boundary) {
        // Explicit manual intent and mandatory native capacity do not depend
        // on task evidence. Optional timing never recomputes a context limit.
        if (purpose === 'manual') return 'manual';
        if (session.storedContext.compactionRequired(session, settings)) return 'overflow';
        return this.taskTimingEnabled() && boundary.length ? 'task_boundary' : 'unneeded';
    }
    taskTimingEnabled() {
        // Managed sessions disable Pi's autonomous compaction/retry. This
        // explicit policy selects only the journaled owner operation.
        return this.taskAware;
    }
    inputTokens(model, reserveTokens) {
        if (!(model.contextWindow > 0)) throw new Error('Compaction model context is unavailable');
        const tokens = Math.floor((model.contextWindow - reserveTokens) * this.inputBudgetRatio);
        if (tokens < 4096) throw new BudgetAdmissionError('Compaction model context is too small');
        return tokens;
    }
    sourceTokens(model, reserveTokens) {
        return Math.floor(this.inputTokens(model, reserveTokens) * this.sourceBudgetRatio);
    }
    contextTokens(messages, model) {
        return new ContextBudget(model, convertToLlm(Array.from(messages))).input;
    }
    contextFits(messages, model, reserveTokens) {
        return this.requestFits({messages: convertToLlm(Array.from(messages))}, model, reserveTokens);
    }
    requireContext(messages, model, reserveTokens) {
        this.requireRequest({messages: convertToLlm(Array.from(messages))}, model, reserveTokens);
    }
    requestFits(context, model, reserveTokens) {
        return new ContextBudget(model, context).input <= this.inputTokens(model, reserveTokens);
    }
    requireRequest(context, model, reserveTokens) {
        if (!this.requestFits(context, model, reserveTokens))
            throw new BudgetAdmissionError('Compaction result exceeds its selected context budget');
    }
    retainedFits(required, messages, model, reserveTokens) {
        // Required exact source and cumulative file annotations are allocated
        // first. Recent atomic messages share only the remaining token capacity.
        const mandatory = this.contextTokens([required], model);
        const limit = mandatory + Math.floor(
            (this.inputTokens(model, reserveTokens) - mandatory) * this.sourceBudgetRatio);
        return this.contextTokens([required, ...messages], model) <= limit;
    }
    packSummary(exactText, narrative, annotations, tokensBefore, retainedMessages,
                model, reserveTokens, createSummary) {
        // Only narrative may be shortened. Original tool pairs/recent messages
        // remain in Pi's preparation; mandatory source text is never reduced.
        const compose = text => `${exactText}\n\n${text}${annotations}`;
        const fits = text => {
            const synthesized = createSummary(compose(text), tokensBefore, Date.now());
            function* context() { yield synthesized; yield* retainedMessages; }
            return this.contextFits(context(), model, reserveTokens);
        };
        if (!fits('')) throw new BudgetAdmissionError('Mandatory exact task source exceeds the selected context budget');
        if (fits(narrative)) return compose(narrative);
        let lower = 0, upper = narrative.length;
        while (lower < upper) {
            const middle = Math.ceil((lower + upper) / 2);
            if (fits(narrative.slice(0, middle).toWellFormed())) lower = middle;
            else upper = middle - 1;
        }
        return compose(narrative.slice(0, lower).toWellFormed());
    }
    summaryTokens(model, inputTokens, reserveTokens) {
        return Math.min(reserveTokens, this.summaryMaxTokens, Math.max(CompactionPolicy.declarations.summaryMaxTokens.min, Math.floor(inputTokens * this.summaryOutputRatio)),
            model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY);
    }
    summaryInstructions(instructions, maxTokens) {
        // This is generation intent, shared with the SDK request options. A
        // route may omit an output cap; provider usage is cost accounting, not
        // the size of the retained context. packSummary owns that admission.
        return [instructions, `Keep the generated summary within ${maxTokens} tokens.`]
            .filter(Boolean).join('\n\n');
    }
}
