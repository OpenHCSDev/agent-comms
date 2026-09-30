/** Generation intent, capability admission, and pre-stream rejection recovery. */
import { normalizeProviderError } from "../utils/error-body.js";
import { estimateContextTokens, estimateSerializedRequestTokens } from "../utils/estimate.js";

export class BudgetAdmissionError extends Error {
    constructor(message) {
        super(message);
        this.name = "BudgetAdmissionError";
    }
}

/** A request-local calculation, never a second model catalog or usage store. */
export class ContextBudget {
    constructor(model, context, serializedInput) {
        this.model = model;
        this.input = serializedInput === undefined
            ? estimateContextTokens(context).tokens
            : estimateSerializedRequestTokens(context, serializedInput);
    }

    allowance(desired, minimum = 1) {
        const available = this.model.contextWindow > 0
            ? this.model.contextWindow - this.input : Infinity;
        if (available < minimum) {
            throw new BudgetAdmissionError("Estimated input leaves no admissible generation budget");
        }
        // Optional API parameters stay absent. Capability is not generation intent.
        if (desired === undefined) return undefined;
        if (!Number.isSafeInteger(desired) || desired < minimum) {
            throw new BudgetAdmissionError("Requested generation allowance violates the API limit");
        }
        if (!Number.isSafeInteger(this.model.maxTokens) || this.model.maxTokens < minimum) {
            throw new BudgetAdmissionError("Model has no admissible declared output capability");
        }
        const fitted = Math.min(desired, this.model.maxTokens, available);
        if (fitted < minimum) {
            throw new BudgetAdmissionError("Estimated input and model capability cannot admit the API output minimum");
        }
        return fitted;
    }

    admit(parameters, field, minimum = 1) {
        const fitted = this.allowance(parameters[field], minimum);
        if (fitted !== undefined) parameters[field] = fitted;
    }
}

export class ProviderRejection {
    static formats = new Set();

    static decode(error) {
        const normalized = normalizeProviderError(error);
        if (normalized.status !== 400) return new UnrecoverableRejection();
        for (const format of this.formats) {
            const rejection = format.decodeCounts(normalized.body ?? normalized.message);
            if (rejection !== undefined) return rejection;
        }
        return new UnrecoverableRejection();
    }

    revisedAllowance(request) {
        throw new Error("ProviderRejection must declare its recovery behavior");
    }
}

export class UnrecoverableRejection extends ProviderRejection {
    revisedAllowance() { return undefined; }
}

export class ContextBudgetRejection extends ProviderRejection {
    constructor(limit, total, input, completion) {
        super();
        this.limit = limit;
        this.total = total;
        this.input = input;
        this.completion = completion;
    }

    static decodeCounts(body) {
        const match = this.pattern.exec(body);
        if (match === null) return undefined;
        const rejection = this.fromMatch(match);
        return rejection.valid() ? rejection : undefined;
    }

    valid() {
        return [this.limit, this.total, this.input, this.completion].every(Number.isSafeInteger)
            && this.limit > 0 && this.input >= 0 && this.completion > 0
            && this.total === this.input + this.completion && this.total > this.limit;
    }

    revisedAllowance(request) {
        // An explicit allowance must identify this rejected request's count.
        if (request.allowance !== undefined && request.allowance !== this.completion) return undefined;
        const ceiling = request.allowance ?? Math.min(this.completion, request.model.maxTokens);
        const limit = Math.min(this.limit, request.model.contextWindow);
        const uncertainty = Math.abs(this.input - request.budget.input);
        const available = Math.min(request.model.maxTokens, limit - this.input - uncertainty);
        if (available <= 0 || available >= ceiling) return undefined;
        return available;
    }
}

// These are message-only external contracts, not structured provider count proof.
export class InputCompletionRejection extends ContextBudgetRejection {
    static pattern = /maximum context length of (\d+) tokens\. You requested a total of (\d+) tokens: (\d+) tokens from the input messages and (\d+) tokens for the completion/;
    static { ProviderRejection.formats.add(this); }
    static fromMatch(match) { return new this(...match.slice(1).map(Number)); }
}

export class TextToolOutputRejection extends ContextBudgetRejection {
    static pattern = /This endpoint's maximum context length is (\d+) tokens\. However, you requested about (\d+) tokens \((\d+) of text input, (\d+) of tool input, (\d+) in the output\)/;
    static { ProviderRejection.formats.add(this); }
    static fromMatch(match) {
        const [limit, total, text, tools, output] = match.slice(1).map(Number);
        if (!Number.isSafeInteger(text) || !Number.isSafeInteger(tools) || text < 0 || tools < 0) {
            return new this(0, 0, 0, 0);
        }
        return new this(limit, total, text + tools, output);
    }
}

export class ContextBudgetRequest {
    constructor(model, context, params, budgetField, send, serializedInput, signal) {
        this.model = model;
        this.params = params;
        this.budgetField = budgetField;
        this.sendRequest = send;
        this.signal = signal;
        this.budget = new ContextBudget(model, context, serializedInput);
        this.budget.admit(this.params, this.budgetField);
    }

    get allowance() { return this.params[this.budgetField]; }

    async send(attempt) {
        for (;;) {
            this.signal?.throwIfAborted();
            try {
                return await this.sendRequest(this.params, attempt);
            } catch (error) {
                this.signal?.throwIfAborted();
                const revised = ProviderRejection.decode(error).revisedAllowance(this);
                if (revised === undefined) throw error;
                // Strictly decreasing positive allowances terminate negotiation.
                // An accepted stream is consumed outside this retry boundary.
                this.params = { ...this.params, [this.budgetField]: revised };
            }
        }
    }
}
