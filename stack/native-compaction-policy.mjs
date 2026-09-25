/** Agent-comms summary policy. Pi owns its enabled/reserve/keep-recent settings.
 * This is the sole declaration of adapter defaults and accepted configuration.
 * Provider/model selection, context window, and no-replay are not policy knobs.
 */
const strategies = Object.freeze({
    serial: Object.freeze({ workers: () => 1 }),
    parallel: Object.freeze({ workers: policy => policy.concurrency }),
});
export class CompactionPolicy {
    static declarations = Object.freeze({
        strategy: Object.freeze({ default: 'parallel', choices: Object.keys(strategies) }),
        concurrency: Object.freeze({ default: 4, integer: true, min: 1, max: 8 }),
        inputBudgetRatio: Object.freeze({ default: 0.75, min: 0.25, max: 0.9 }),
        sourceBudgetRatio: Object.freeze({ default: 0.7, min: 0.25, max: 0.8 }),
        summaryMaxTokens: Object.freeze({ default: 4096, integer: true, min: 256, max: 32768 }),
        summaryOutputRatio: Object.freeze({ default: 1 / 12, min: 0.01, max: 0.5 }),
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
    get workers() { return strategies[this.strategy].workers(this); }
    inputBytes(model, reserveTokens) {
        if (!(model.contextWindow > 0)) throw new Error('Compaction model context is unavailable');
        const bytes = Math.floor((model.contextWindow - reserveTokens) * this.inputBudgetRatio);
        if (bytes < 4096) throw new Error('Compaction model context is too small');
        return bytes;
    }
    summaryTokens(model, byteLimit) {
        return Math.min(this.summaryMaxTokens, Math.max(256, Math.floor(byteLimit * this.summaryOutputRatio)),
            model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY);
    }
}
