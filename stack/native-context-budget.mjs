/** Recover only an explicitly rejected, not-yet-streaming provider request. */
import { normalizeProviderError } from "../utils/error-body.js";
import { estimateContextTokens } from "../utils/estimate.js";

export class ProviderRejection {
    static decode(error) {
        const normalized = normalizeProviderError(error);
        if (normalized.status !== 400) return new UnrecoverableRejection();
        const match = /maximum context length of (\d+) tokens\. You requested a total of (\d+) tokens: (\d+) tokens from the input messages and (\d+) tokens for the completion/.exec(normalized.body ?? normalized.message);
        if (match === null) return new UnrecoverableRejection();
        const [limit, total, input, completion] = match.slice(1).map(Number);
        if (![limit, total, input, completion].every(Number.isSafeInteger)
                || limit <= 0 || input < 0 || completion <= 0
                || total !== input + completion || total <= limit) {
            return new UnrecoverableRejection();
        }
        return new ContextBudgetRejection(limit, input, completion);
    }

    revisedAllowance(request) {
        throw new Error("ProviderRejection must declare its recovery behavior");
    }
}

export class UnrecoverableRejection extends ProviderRejection {
    revisedAllowance(request) { return undefined; }
}

export class ContextBudgetRejection extends ProviderRejection {
    constructor(limit, input, completion) {
        super();
        this.limit = limit;
        this.input = input;
        this.completion = completion;
    }

    revisedAllowance(request) {
        // A count belonging to another request cannot authorize this revision.
        if (request.allowance !== this.completion) return undefined;
        const limit = Math.min(this.limit, request.model.contextWindow);
        const uncertainty = Math.abs(this.input - estimateContextTokens(request.context).tokens);
        const available = Math.min(request.model.maxTokens, limit - this.input - uncertainty);
        if (available <= 0 || available >= request.allowance) return undefined;
        return available;
    }
}

export class ContextBudgetRequest {
    constructor(model, context, params, budgetField, send) {
        this.model = model;
        this.context = context;
        this.params = params;
        this.budgetField = budgetField;
        this.sendRequest = send;
    }

    get allowance() { return this.params[this.budgetField]; }

    async send() {
        for (;;) {
            try {
                return await this.sendRequest(this.params);
            } catch (error) {
                const revised = ProviderRejection.decode(error).revisedAllowance(this);
                if (revised === undefined) throw error;
                // Strictly decreasing positive allowances terminate negotiation;
                // no transport failure or accepted response authorizes a replay.
                this.params = { ...this.params, [this.budgetField]: revised };
            }
        }
    }
}
