/** Safe recovery belongs to the native run, not the caller or the input journal. */
import { setTimeout as sleep } from 'node:timers/promises';

class RecoveryBudget {
    static emptyWaitMs = 120_000;
    static retries = 3;
    // Sustained provider outages survive only if the wait stretches to minutes.
    static backoffMs = [1_000, 10_000, 60_000];
    static get attempts() { return this.retries + 1; }
    static delay(attempt) { return this.backoffMs[Math.min(attempt - 1, this.backoffMs.length - 1)]; }
}

/** Decode known explicit provider refusals, never generic transport failures. */
class ProviderRefusal {
    static decode(message) {
        if (message?.stopReason !== 'error') return undefined;
        // Provider formatters can retain HTTP status and JSON bodies around the
        // original sentence. Decode the known refusal here, not in Core or UI.
        const match = /(?:^|[\s":])(Our servers are currently overloaded\. Please try again later\.?|An error occurred while processing your request\. You can retry your request\.?|Internal (?:server )?error\.?)(?=$|[\s"},])/i.exec(message.errorMessage ?? '');
        return match?.[1];
    }
}

class NativeRunState {
    constructor(owner, attempt = 1) { this.owner = owner; this.attempt = attempt; }
    get retrying() { return false; }
    get mayRetry() { return false; }
    refusal() { return undefined; }
    produced() { return this; }
    finished(message) { return this; }
    request(options) { return options; }
    close() {}
    cancel() { this.close(); }
}

class NothingProduced extends NativeRunState {
    produced() {
        this.close();
        if (this.attempt > 1) {
            this.owner.emit({ type: 'native_retry_end', success: true, attempt: this.attempt - 1 });
        }
        return new OutputProduced(this.owner, this.attempt);
    }
    finished(message) {
        // The recovery budget is the one owner of a refusal's retry count: it is
        // decoded and announced once here, before the refused message reaches
        // Core, so Core reports only the recovery's final outcome.
        if (!this.owner.tracked) return this;
        const reason = ProviderRefusal.decode(message);
        if (!reason) return this;
        const refused = new RefusedNothingProduced(this.owner, this.attempt, reason);
        refused.announce();
        return refused;
    }
    request(options) {
        const waiting = new WaitingNothingProduced(this.owner, this.attempt, options.signal);
        this.owner.state = waiting;
        return { ...options, signal: waiting.signal };
    }
}

class WaitingNothingProduced extends NothingProduced {
    constructor(owner, attempt, parentSignal) {
        super(owner, attempt);
        this.controller = new AbortController();
        this.signal = parentSignal
            ? AbortSignal.any([parentSignal, this.controller.signal]) : this.controller.signal;
        this.timer = setTimeout(() => {
            if (this.signal.aborted || this.owner.state !== this) return;
            const refusal = new RefusedNothingProduced(this.owner, this.attempt,
                `Provider produced nothing for ${RecoveryBudget.emptyWaitMs / 1000} seconds`);
            this.owner.state = refusal;
            refusal.announce();
            this.controller.abort(new Error(refusal.reason));
        }, RecoveryBudget.emptyWaitMs);
        this.timer.unref();
    }
    close() { clearTimeout(this.timer); }
    finished(message) {
        this.close();
        const next = super.finished(message);
        return next === this ? new NothingProduced(this.owner, this.attempt) : next;
    }
}

class OutputProduced extends NativeRunState {}

class RefusedNothingProduced extends NothingProduced {
    constructor(owner, attempt, reason) { super(owner, attempt); this.reason = reason; }
    get mayRetry() { return this.attempt < RecoveryBudget.attempts; }
    refusal() { return this; }
    announce() {
        if (!this.mayRetry) {
            // The final refusal keeps the refusal window closed: Core reports
            // this message_end as the outcome after the recovery gave up.
            this.owner.emit({ type: 'native_retry_end', success: false,
                attempt: RecoveryBudget.retries, finalError: this.reason });
            return;
        }
        this.owner.emit({ type: 'native_retry_start', attempt: this.attempt,
            maxAttempts: RecoveryBudget.retries, delayMs: RecoveryBudget.delay(this.attempt),
            errorMessage: this.reason });
    }
    finished(message) {
        // Only our own empty-wait abort has this state before message_end.
        // Keep its actual cause in retained native history instead of "Request aborted".
        message.errorMessage = this.reason;
        return this;
    }
    async recover(message) {
        if (!this.mayRetry) {
            this.owner.state = new ExhaustedNothingProduced(this.owner, this.attempt, this.reason);
            return false;
        }
        const backoff = new BackoffNothingProduced(this.owner, this.attempt, this.reason);
        this.owner.state = backoff;
        return backoff.recover(message);
    }
}

class ExhaustedNothingProduced extends RefusedNothingProduced {
    async recover() { return false; }
}

class CancelledNothingProduced extends NativeRunState {}

class BackoffNothingProduced extends RefusedNothingProduced {
    constructor(owner, attempt, reason) {
        super(owner, attempt, reason);
        this.controller = new AbortController();
    }
    get retrying() { return true; }
    cancel() { this.controller.abort(); }
    async recover(message) {
        const delayMs = RecoveryBudget.delay(this.attempt);
        try { await sleep(delayMs, undefined, { signal: this.controller.signal }); }
        catch (error) {
            if (error.name !== 'AbortError') throw error;
            this.owner.state = new CancelledNothingProduced(this.owner, this.attempt);
            this.owner.emit({ type: 'native_retry_end', success: false,
                attempt: this.attempt, finalError: 'Retry cancelled' });
            return false;
        }
        // Continue the already committed context; retain the failed assistant in
        // the native journal and remove ONLY that exact assistant from model context.
        // The refusal was already announced and counted at recognition; the
        // attempt increments exactly once, here.
        const messages = this.owner.agent.state.messages;
        if (messages.at(-1) !== message) throw new Error('Native recovery context changed');
        this.owner.agent.state.messages = messages.slice(0, -1);
        this.owner.state = new NothingProduced(this.owner, this.attempt + 1);
        return true;
    }
}

/** Replaces the native tracked-input flag and owns monotonic whole-run production. */
export class NativeTurnRun {
    constructor(agent, emit) {
        this.agent = agent;
        this.emit = emit;
        this.inputs = new Set();
        this.state = new NothingProduced(this);
    }
    get tracked() { return this.inputs.size > 0; }
    get retrying() { return this.state.retrying; }
    include(messages, claims) {
        for (const message of messages) {
            if (message.role === 'user' && claims.has(message.inputId)) this.inputs.add(message.inputId);
        }
        return this;
    }
    observe(event, claims) {
        if (event.type === 'message_start') this.include([event.message], claims);
        // Observe the original assistant event BEFORE extension callbacks or UI projection.
        if (event.type === 'tool_execution_start') this.state = this.state.produced();
        if (event.type === 'message_update') {
            const delta = event.assistantMessageEvent;
            if (delta?.type === 'toolcall_start' ||
                (delta?.type?.endsWith('_delta') && delta.delta?.length)) this.state = this.state.produced();
        }
        if (event.type === 'message_end' && event.message.role === 'assistant') {
            const content = event.message.content;
            if (content.some(block => block.type === 'toolCall' || block.text?.length || block.thinking?.length)) {
                this.state = this.state.produced();
            }
            this.state = this.state.finished(event.message);
        }
    }
    stream(original, model, context, options) {
        if (!this.tracked) return original(model, context, options);
        return original(model, context, this.state.request({ ...options, maxRetries: 0 }));
    }
    willRetry(message) { return this.tracked && (this.state.refusal(message)?.mayRetry ?? false); }
    async recover(message) {
        if (!this.tracked || this.agent.state.messages.at(-1) !== message) return false;
        const refusal = this.state.refusal(message);
        if (!refusal) return false;
        this.state = refusal;
        return refusal.recover(message);
    }
    cancel() { this.state.cancel(); }
    close() { this.state.close(); }
}
