/** Safe recovery belongs to the native run, not the caller or the input journal. */
import { setTimeout as sleep } from 'node:timers/promises';
import { OwnerToolConnection } from '../owner_tool_socket.mjs';

class RecoveryBudget {
    constructor(backoffMs = [1_000, 10_000, 60_000]) {
        if (!Array.isArray(backoffMs) || backoffMs.some(delay =>
            !Number.isFinite(delay) || delay < 0))
            throw new TypeError('Native recovery backoffs must be finite nonnegative milliseconds');
        this.backoffMs = Object.freeze([...backoffMs]);
    }
    get retries() { return this.backoffMs.length; }
    get attempts() { return this.retries + 1; }
    delay(attempt) { return this.backoffMs[attempt - 1]; }
}

class NativeRunState {
    constructor(owner, attempt = 1) { this.owner = owner; this.attempt = attempt; }
    get retrying() { return false; }
    refusal() { return undefined; }
    started() { return new NothingProduced(this.owner); }
    produced() { return this; }
    finished() { return this; }
    enter() { return this; }
    published() {}
    close() {}
    cancel() { this.close(); }
}

class NothingProduced extends NativeRunState {
    started() { return this; }
    produced() { return new OutputProduced(this.owner, this.attempt); }
    terminal() { return new ClosedNothingProduced(this.owner, this.attempt); }
    finished(message) {
        // One decision constructs the state for this refusal. Retryable and
        // terminal states then own disjoint behavior without checking a flag.
        if (!this.owner.tracked) return this;
        if (!this.owner.retryable(message)) return this.terminal(message);
        const reason = message.errorMessage || 'Provider request failed';
        return (this.attempt < this.owner.budget.attempts
            ? new RefusedNothingProduced(this.owner, this.attempt, reason)
            : new FailedProviderRequest(this.owner, this.attempt, reason)
        ).enter(message);
    }
}

/** Every live recovery state shares completion and retirement behavior. */
class RecoveringNothingProduced extends NothingProduced {
    produced() {
        this.owner.finishRetry(true, undefined, this.attempt - 1);
        return super.produced();
    }
    terminal(message) {
        // A clean provider stop completes recovery. Whether its text answers
        // the selected task belongs to Core's TrackedTerminal, not this run.
        return message?.stopReason === 'stop'
            ? new RecoveredProviderRequest(this.owner, this.attempt)
            : new FailedProviderRequest(this.owner, this.attempt,
                message?.errorMessage || 'Provider retry closed before completing');
    }
    close(message) {
        this.owner.state = this.terminal(message);
        this.owner.state.published();
    }
}

/** Both completed states use the next request's own production and retry window. */
class OutputProduced extends NativeRunState {}
class ClosedNothingProduced extends NativeRunState {}

class RecoveredProviderRequest extends ClosedNothingProduced {
    published() { this.owner.finishRetry(true, undefined, this.attempt - 1); }
}

class RefusedNothingProduced extends RecoveringNothingProduced {
    constructor(owner, attempt, reason) { super(owner, attempt); this.reason = reason; }
    refusal() { return this; }
    enter() {
        this.owner.emit({ type: 'native_retry_start', attempt: this.attempt,
            maxAttempts: this.owner.budget.retries, delayMs: this.owner.budget.delay(this.attempt),
            errorMessage: this.reason });
        return this;
    }
    async recover(message) {
        const backoff = new BackoffNothingProduced(this.owner, this.attempt, this.reason);
        this.owner.state = backoff;
        return backoff.recover(message);
    }
}

/** Typed terminal state after the provider used every owned re-request. */
class FailedProviderRequest extends ClosedNothingProduced {
    constructor(owner, attempt, reason) { super(owner, attempt); this.reason = reason; }
    published() { this.owner.finishRetry(false, this.reason, this.attempt - 1); }
}

class BackoffNothingProduced extends RefusedNothingProduced {
    constructor(owner, attempt, reason) {
        super(owner, attempt, reason);
        this.controller = new AbortController();
    }
    get retrying() { return true; }
    cancel() { this.controller.abort(); }
    close(message) {
        this.controller.abort();
        super.close(message);
    }
    async recover(message) {
        const delayMs = this.owner.budget.delay(this.attempt);
        try { await sleep(delayMs, undefined, { signal: this.controller.signal }); }
        catch (error) {
            if (error.name !== 'AbortError') throw error;
            // Retirement already closed the window if it aborted this wait.
            if (this.owner.state === this) {
                this.close({ errorMessage: 'Retry cancelled' });
            }
            return false;
        }
        // A late production/retirement owns the run now, not this old wait.
        if (this.owner.state !== this) return false;
        // Continue the already committed context; retain the failed assistant in
        // the native journal and remove ONLY that exact assistant from model context.
        // The refusal was already announced and counted at recognition; the
        // attempt increments exactly once, here.
        const messages = this.owner.agent.state.messages;
        if (messages.at(-1) !== message) throw new Error('Native recovery context changed');
        this.owner.agent.state.messages = messages.slice(0, -1);
        this.owner.state = new RecoveringNothingProduced(this.owner, this.attempt + 1);
        return true;
    }
}

/** Explicit absence of a native run; never a partially initialized active run. */
export class InactiveNativeTurnRun {
    static shared = new InactiveNativeTurnRun();
    get ownerTools() { return OwnerToolConnection.unavailable; }
    get tracked() { return false; }
    get retrying() { return false; }
    include() { return this; }
    observe() { return this; }
    published() {}
    stream(original, ...request) { return original(...request); }
    willRetry() { return false; }
    async recover() { return false; }
    cancel() {}
    close() {}
}

/** Replaces the native tracked-input flag and owns per-request production and recovery. */
export class NativeTurnRun {
    constructor(agent, emit, retryable, backoffMs) {
        this.budget = new RecoveryBudget(backoffMs);
        this.agent = agent;
        this.emit = emit;
        // AgentSession supplies its existing classifier, including context-
        // overflow exclusion. Tracked and untracked runs share that authority.
        this.retryable = retryable;
        this.inputs = new Map();
        this.state = new NothingProduced(this);
    }
    finishRetry(success, finalError, attempt) {
        // The request index increments only after an admitted backoff. A
        // cancelled first backoff therefore reports zero actual re-requests.
        this.emit({ type: 'native_retry_end', success,
            attempt, ...(finalError ? { finalError } : {}) });
    }
    get ownerTools() { return [...this.inputs.values()].at(-1)?.ownerTools ?? OwnerToolConnection.unavailable; }
    get tracked() { return this.inputs.size > 0; }
    get retrying() { return this.state.retrying; }
    include(messages, claims) {
        for (const message of messages) {
            if (message.role === 'user' && claims.has(message.inputId))
                this.inputs.set(message.inputId, claims.get(message.inputId));
        }
        return this;
    }
    observe(event, claims) {
        if (event.type === 'message_start') {
            this.include([event.message], claims);
            if (event.message.role === 'assistant') this.state = this.state.started();
        }
        // Observe the original assistant event BEFORE extension callbacks or UI projection.
        if (event.type === 'tool_execution_start') this.state = this.state.produced();
        if (event.type === 'message_update') {
            const delta = event.assistantMessageEvent;
            if (delta?.type === 'toolcall_start' ||
                (delta?.type === 'text_delta' && delta.delta?.length)) this.state = this.state.produced();
        }
        if (event.type === 'message_end' && event.message.role === 'assistant') {
            const content = event.message.content;
            if (content.some(block => block.type === 'toolCall' || block.text?.length)) {
                this.state = this.state.produced();
            }
            this.state = this.state.finished(event.message);
        }
        return this.state;
    }
    stream(original, model, context, options) {
        return original(model, context,
            this.tracked ? { ...options, maxRetries: 0 } : options);
    }
    willRetry() { return this.tracked && this.state.refusal() !== undefined; }
    async recover(message) {
        if (!this.tracked || this.agent.state.messages.at(-1) !== message) return false;
        const refusal = this.state.refusal(message);
        if (!refusal) return false;
        this.state = refusal;
        return refusal.recover(message);
    }
    cancel() { this.state.cancel(); }
    close() { this.state.close(); this.inputs.clear(); }
}
