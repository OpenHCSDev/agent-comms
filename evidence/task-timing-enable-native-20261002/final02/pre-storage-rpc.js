/**
 * RPC mode: Headless operation with JSON stdin/stdout protocol.
 *
 * Used for embedding the agent in other applications.
 * Receives commands as JSON on stdin, outputs events and responses as JSON on stdout.
 *
 * Protocol:
 * - Commands: JSON objects with `type` field, optional `id` for correlation
 * - Responses: JSON objects with `type: "response"`, `command`, `success`, and optional `data`/`error`
 * - Events: AgentSessionEvent objects streamed as they occur
 * - Extension UI: Extension UI requests are emitted, client responds with extension_ui_response
 */
import * as crypto from "node:crypto";
import { CompactionPolicy } from "../../core/compaction/agent-comms-policy.js";
import { compact, prepareCompaction } from "../../core/compaction/index.js";
import { AssistantMessageEventStream } from "../../../node_modules/@earendil-works/pi-ai/dist/utils/event-stream.js";
import { flushRawStdout, takeOverStdout, waitForRawStdoutBackpressure, writeRawStdout, } from "../../core/output-guard.js";
import { killTrackedDetachedChildren } from "../../utils/shell.js";
import { theme } from "../interactive/theme/theme.js";
import { toJsonEvent } from "../json-event.js";
import { attachJsonlLineReader, serializeJsonLine } from "./jsonl.js";
/**
 * Run in RPC mode.
 * Listens for JSON commands on stdin, outputs events and responses on stdout.
 */
// Injected into the exact disposable Pi RPC build, never the installed pin.
// One operation is NOT a replay grant. Python must journal its ID before send.
function acExactObject(value, keys) {
    return value !== null && typeof value === "object" && !Array.isArray(value) &&
        Object.keys(value).sort().join(",") === [...keys].sort().join(",");
}
const acSummaryId = value => typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
const acNativeSummaryResult = AssistantMessageEventStream.prototype.result;
// Read the actual selected SettingsManager: it already owns project trust,
// migrations and in-memory overrides. No detached reader guesses that state.
function acValidCompactionSettingsRequest(command) {
    const text = value => typeof value === "string" && value.length > 0 && value.length <= 4096;
    return acExactObject(command, ["id", "type", "version", "sessionId", "sessionFile", "selected", "purpose", "boundary"]) &&
        command.type === "agent_comms_compaction_settings" && command.version === 2 && text(command.id) &&
        ["threshold", "manual"].includes(command.purpose) && Array.isArray(command.boundary) &&
        command.boundary.length <= 1 && command.boundary.every(ref =>
            acExactObject(ref, ["seq", "message_id"]) && Number.isSafeInteger(ref.seq) &&
            ref.seq > 0 && text(ref.message_id)) &&
        text(command.sessionId) && text(command.sessionFile) &&
        acExactObject(command.selected, ["provider", "modelId", "contextWindow"]) &&
        text(command.selected.provider) && text(command.selected.modelId) &&
        Number.isSafeInteger(command.selected.contextWindow) && command.selected.contextWindow > 0;
}
function acSelectedCompactionSettings(command, session, conflict) {
    if (conflict || session.isCompacting || !session.isIdle || session.isStreaming || session.isRetrying ||
        session._retryAttempt || session._nativeInterruptIds || session.pendingMessageCount ||
        session.agent.steeringQueue.messages.length || session.agent.followUpQueue.messages.length ||
        session._pendingNextTurnMessages.length || session._pendingCustomMessages.length ||
        session._pendingBashMessages.length) throw Error("Selected compaction settings require an idle owner");
    const model = session.model;
    if (command.sessionId !== session.sessionId || command.sessionFile !== session.sessionFile ||
        !model || command.selected.provider !== model.provider || command.selected.modelId !== model.id ||
        command.selected.contextWindow !== model.contextWindow) throw Error("Selected compaction source changed");
    const settings = session.settingsManager.getCompactionSettings();
    if (typeof settings.enabled !== "boolean" || !Number.isSafeInteger(settings.reserveTokens) ||
        settings.reserveTokens < 0 || settings.reserveTokens > 10000000 ||
        !Number.isSafeInteger(settings.keepRecentTokens) || settings.keepRecentTokens <= 0 ||
        settings.keepRecentTokens > 10000000) throw Error("Invalid effective compaction settings");
    const policy = CompactionPolicy.fromEnvironment();
    return {version: 2, sessionId: session.sessionId, sessionFile: session.sessionFile,
        selected: command.selected,
        decision: {enabled: settings.enabled, reserveTokens: settings.reserveTokens,
            keepRecentTokens: settings.keepRecentTokens,
            taskAware: policy.taskTimingEnabled(), boundary: command.boundary,
            reason: policy.decision(session, settings, command.purpose, command.boundary)}};
}
function acValidSummaryRequest(value) {
    const fields = ["id", "type", "version", "operationId", "witness", "selected", "settings", "retainedText"];
    if (value && Object.hasOwn(value, "customInstructions")) {
        if (typeof value.customInstructions !== "string" || !value.customInstructions.isWellFormed()) return false;
        fields.push("customInstructions");
    }
    if (!acExactObject(value, fields) ||
        value.type !== "agent_comms_summarize_compaction" || !acSummaryId(value.operationId)) return false;
    if (typeof value.retainedText !== "string" || !value.retainedText.isWellFormed()) return false;
    const nonempty = text => typeof text === "string" && text.length > 0 && text.length <= 4096;
    const integer = number => Number.isSafeInteger(number) && number >= 0;
    return nonempty(value.id) && value.version === 1 &&
        acExactObject(value.witness, ["sessionId", "sessionFile", "leafId", "firstKeptEntryId", "revision"]) &&
        ["sessionId", "sessionFile", "leafId", "firstKeptEntryId", "revision"].every(key =>
            nonempty(value.witness[key])) &&
        acExactObject(value.selected, ["provider", "modelId", "contextWindow"]) &&
        nonempty(value.selected.provider) && nonempty(value.selected.modelId) &&
        integer(value.selected.contextWindow) && value.selected.contextWindow > 0 &&
        acExactObject(value.settings, ["reserveTokens", "keepRecentTokens"]) &&
        integer(value.settings.reserveTokens) && integer(value.settings.keepRecentTokens);
}
function acValidSummaryCancel(value) {
    return acExactObject(value, ["id", "type", "version", "operationId"]) &&
        typeof value.id === "string" && value.id.length > 0 && value.id.length <= 4096 &&
        value.type === "agent_comms_cancel_summary" && value.version === 1 && acSummaryId(value.operationId);
}
const acSummaryDecline = (operationId, reason) => ({
    version: 1, status: "declined", operationId, reason,
});
// Diagnostic text never decides whether a failure is terminal and never grants replay.
const acSummaryReason = reason => reason.replace(/[\u0000-\u001f\u007f]/g, " ")
    .trim().slice(0, 1024).toWellFormed() || "Selected summary provider failed";
const acSummaryUnknown = (operationId, reason) => ({ version: 1, status: "unknown", operationId,
    ...(reason === undefined ? {} : { reason: acSummaryReason(reason) }) });
class AcSummaryProviderFailure extends Error {
    outcome(request) {
        return {version: 1, status: "failed", operationId: request.operationId,
            witness: request.witness, selected: request.selected, settings: request.settings,
            reason: acSummaryReason(this.message)};
    }
}
const acSummaryHooks = ["session_before_compact", "before_provider_headers", "before_provider_request",
    "after_provider_response"];
function acSummaryCompatible(session, binding) {
    const runner = session.extensionRunner;
    // The SDK's streamFn consults extensionRunnerRef dynamically for headers.
    // No pointer alone can attest mutable hook behavior or custom route parity.
    return runner && typeof runner.hasHandlers === "function" &&
        acSummaryHooks.every(name => !runner.hasHandlers(name)) &&
        (!session._extensionRunnerRef || session._extensionRunnerRef.current === runner) &&
        (!binding || (runner === binding.runner && session.agent.streamFunction === binding.streamFn &&
            session.model === binding.model && session.modelRuntime === binding.modelRuntime &&
            session.settingsManager === binding.settingsManager && session.sessionManager === binding.manager &&
            session.modelRuntime.getAvailableSnapshot() === binding.catalog));
}
function acSummaryCurrent(session, request, binding) {
    if (session.isCompacting || session.isStreaming || !session.isIdle || session.isRetrying ||
        session._retryAttempt || session._nativeInterruptIds ||
        (binding && binding.host.session !== session) || session.pendingMessageCount || session.agent.steeringQueue.messages.length ||
        session.agent.followUpQueue.messages.length || session._pendingNextTurnMessages.length ||
        session._pendingCustomMessages.length || session._pendingBashMessages.length ||
        !acSummaryCompatible(session, binding)) return false;
    const model = session.model;
    if (!model || model.provider !== request.selected.provider || model.id !== request.selected.modelId ||
        model.contextWindow !== request.selected.contextWindow) return false;
    const settings = session.settingsManager.getCompactionSettings();
    if (settings.reserveTokens !== request.settings.reserveTokens ||
        settings.keepRecentTokens !== request.settings.keepRecentTokens) return false;
    try {
        const witness = session.sessionManager.captureCompactionWitness(request.witness.firstKeptEntryId);
        if (Object.keys(request.witness).some(key => witness[key] !== request.witness[key])) return false;
        const preparation = prepareCompaction(session.sessionManager.entryStore, settings, model, session.sessionManager.getLeafId(), request.retainedText);
        return preparation &&
            preparation.firstKeptEntryId === request.witness.firstKeptEntryId;
    } catch { return false; }
}
function acSummaryValidUsage(usage) {
    const count = value => Number.isSafeInteger(value) && value >= 0;
    const cost = value => typeof value === "number" && Number.isFinite(value) && value >= 0 &&
        value <= Number.MAX_SAFE_INTEGER;
    const counters = ["input", "output", "cacheRead", "cacheWrite", "totalTokens"];
    const costs = ["input", "output", "cacheRead", "cacheWrite", "total"];
    return usage && typeof usage === "object" && !Array.isArray(usage) &&
        Object.keys(usage).every(key => [...counters, "reasoning", "cacheWrite1h", "cost"].includes(key)) &&
        counters.every(key => count(usage[key])) &&
        ["reasoning", "cacheWrite1h"].every(key => usage[key] === undefined || count(usage[key])) &&
        acExactObject(usage.cost, costs) && costs.every(key => cost(usage.cost[key]));
}
function acSummaryValidResult(result, request) {
    const files = paths => Array.isArray(paths) && paths.every(path =>
        typeof path === "string" && path.length > 0 && path.isWellFormed() &&
        Buffer.byteLength(path, "utf8") <= 4096 && !path.includes("\0"));
    return acExactObject(result, ["summary", "firstKeptEntryId", "tokensBefore", "usage", "details"]) &&
        typeof result.summary === "string" && result.summary.startsWith(request.retainedText + "\n\n") &&
        result.summary.isWellFormed() &&
        result.firstKeptEntryId === request.witness.firstKeptEntryId &&
        Number.isSafeInteger(result.tokensBefore) && result.tokensBefore >= 0 &&
        acExactObject(result.details, ["readFiles", "modifiedFiles"]) &&
        files(result.details.readFiles) && files(result.details.modifiedFiles) && acSummaryValidUsage(result.usage);
}
// Synchronous admission/preparation has no auth, hooks, provider, or native append.
function acAdmitSummary(request, session, conflict, spent, host) {
    if (spent.has(request.operationId)) return { denial: "duplicate_operation" };
    if (conflict) return { denial: "in_flight" };
    if (session.isCompacting) return { denial: "compacting" };
    if (!session.isIdle || session.isStreaming || session.isRetrying ||
        session._retryAttempt || session._nativeInterruptIds) return { denial: "busy" };
    if (session.pendingMessageCount || session.agent.steeringQueue.messages.length ||
        session.agent.followUpQueue.messages.length || session._pendingNextTurnMessages.length ||
        session._pendingCustomMessages.length || session._pendingBashMessages.length)
        return { denial: "queue_nonempty" };
    const manager = session.sessionManager;
    if (!manager || !session.sessionFile || !session.sessionId ||
        request.witness.sessionId !== session.sessionId ||
        request.witness.sessionFile !== session.sessionFile ||
        request.witness.leafId !== manager.getLeafId()) return { denial: "source_mismatch" };
    const model = session.model;
    if (!model || model.provider !== request.selected.provider || model.id !== request.selected.modelId ||
        model.contextWindow !== request.selected.contextWindow) return { denial: "model_mismatch" };
    const catalog = session.modelRuntime.getAvailableSnapshot();
    if (!Array.isArray(catalog) || !catalog.some(item => item.provider === model.provider &&
        item.id === model.id && item.contextWindow === model.contextWindow)) return { denial: "unsupported" };
    const settings = session.settingsManager.getCompactionSettings();
    if (!settings || settings.reserveTokens !== request.settings.reserveTokens ||
        settings.keepRecentTokens !== request.settings.keepRecentTokens) return { denial: "settings_mismatch" };
    let witness;
    try { witness = manager.captureCompactionWitness(request.witness.firstKeptEntryId); }
    catch { return { denial: "source_mismatch" }; }
    if (!acExactObject(witness, ["sessionId", "sessionFile", "leafId", "firstKeptEntryId", "revision"]) ||
        Object.keys(request.witness).some(key => witness[key] !== request.witness[key]))
        return { denial: "source_mismatch" };
    if (!acSummaryCompatible(session)) return { denial: "extension_unsupported" };
    let preparation;
    try { preparation = prepareCompaction(manager.entryStore, settings, model, manager.getLeafId(), request.retainedText); }
    catch { return { denial: "unsupported" }; }
    if (!preparation || preparation.firstKeptEntryId !== request.witness.firstKeptEntryId)
        return { denial: "source_mismatch" };
    // Native compact() owns model-sized map/reduction requests. Total retained
    // history is not a request-size limit, and raw JSON includes metadata that
    // never reaches the model. Do not reject a valid preparation before chunking.
    const binding = { host, runner: session.extensionRunner, streamFn: session.agent.streamFunction,
        model: session.model, modelRuntime: session.modelRuntime,
        settingsManager: session.settingsManager, manager: session.sessionManager,
        catalog: session.modelRuntime.getAvailableSnapshot() };
    if (typeof binding.streamFn !== "function" || !acSummaryCurrent(session, request, binding))
        return { denial: "source_mismatch" };
    return { preparation, binding };
}
async function acExecuteSummary(slot, session, request, preparation, binding, output) {
    // Native compact() owns the finite map/reduction plan and its concurrency.
    // Do not confuse its worker count with a total provider-call allowance.
    // Pi policy owns per-call output tokens. The owner enforces inactivity
    // across correlated real progress; total history does not get a second budget.
    const inFlight = new Set();
    let uncertainStream = false;
    let progressSequence = 0;
    const progress = (text = "", source = null) => output({ type: "agent_comms_compaction_progress",
        id: request.id, operationId: request.operationId, sequence: ++progressSequence,
        text, source });
    const selectedStream = (model, context, options) => {
        // No standalone getAuth: actual selected streamFn resolves auth on use.
        if (slot.controller.signal.aborted ||
            !acSummaryCurrent(session, request, binding) || model !== binding.model ||
            !acSummaryCompatible(session, binding)) throw new Error("Selected route changed before call");
        // Everything after this line, including auth/header hooks, is possibly
        // spent even if the fake transport observes zero network requests.
        slot.started = true;
        let stream;
        try {
            stream = binding.streamFn(model, context, { ...options, maxRetries: 0,
                signal: slot.controller.signal });
        } catch {
            // STARTED, but the stream function gave no receipt to join. It may
            // already have started provider work; hold until external retirement.
            slot.controller.abort();
            const unjoined = new Promise(() => {});
            inFlight.add(unjoined);
            return { result: () => unjoined };
        }
        const visible = new AssistantMessageEventStream();
        const response = (async () => {
            let source;
            try { source = await stream; }
            catch { slot.controller.abort(); await new Promise(() => {}); }
            let trusted = false;
            try {
                trusted = Object.getPrototypeOf(source) === AssistantMessageEventStream.prototype &&
                    source.result === acNativeSummaryResult;
            } catch { /* an untrusted getter is not a terminal receipt */ }
            if (!trusted) {
                slot.controller.abort();
                // No trusted terminal receipt. An instance with an overridden
                // result() may reject while its producer is still running.
                // Only externally proven child retirement can clear this slot.
                await new Promise(() => {});
            }
            const providerTerminal = Promise.resolve().then(() =>
                acNativeSummaryResult.call(source));
            void providerTerminal.catch(() => {}); // join below retains rejection
            let terminal;
            let terminalResult;
            let sawStart = false;
            let invalidEvent = false;
            // Consume the pinned AssistantMessageEventStream itself. Its
            // `result()` buffers everything before returning; deltas permit an
            // early abort request. We STILL drain/join through terminal, even
            // after a tool call or malformed event, before releasing the slot.
            try {
                for await (const event of source) {
                    const type = typeof event?.type === "string" ? event.type : null;
                    if (!type) invalidEvent = true;
                    if (type === "start") sawStart = true;
                    if (["text_delta", "thinking_delta", "toolcall_delta"].includes(type) &&
                        typeof event.delta !== "string") invalidEvent = true;
                    if (type?.startsWith("toolcall_")) invalidEvent = true;
                    if (invalidEvent && !slot.controller.signal.aborted) slot.controller.abort();
                    if (!invalidEvent && (type === "start" || type === "done" ||
                        type === "error"))
                        progress();
                    if (!invalidEvent && type !== "done" && type !== "error") visible.push(event);
                    if (type === "done" || type === "error") terminal = event;
                }
            } catch (error) {
                slot.controller.abort();
                throw error;
            } finally {
                // Consumer iterator closure is NOT provider completion. On
                // malformed events, parser errors or cancellation, join the
                // original pinned stream terminal before the slot can clear.
                try { terminalResult = await providerTerminal; }
                catch {
                    // Rejection is not completion: a forged or broken terminal
                    // can fail early while provider work continues.
                    slot.controller.abort();
                    await new Promise(() => {});
                }
            }
            if (invalidEvent)
                throw new Error("Selected summary stream contained unsupported events");
            if (terminal?.type === "error" && terminal.reason === "error" &&
                terminal.error === terminalResult && terminalResult?.role === "assistant" &&
                terminalResult.stopReason === "error")
                throw new AcSummaryProviderFailure(typeof terminalResult.errorMessage === "string" &&
                    terminalResult.errorMessage.trim() ? terminalResult.errorMessage : "Selected summary provider failed");
            if (terminal?.type === "error")
                throw new Error(typeof terminal.error?.errorMessage === "string" && terminal.error.errorMessage.trim()
                    ? terminal.error.errorMessage : `Selected summary provider stopped: ${terminal.reason}`);
            if (!sawStart || terminal?.type !== "done" || terminal.reason !== "stop")
                throw new Error("Selected summary stream ended without a complete response");
            const value = terminal.message;
            if (!value || value.stopReason !== "stop" || !acSummaryValidUsage(value.usage) ||
                !Array.isArray(value.content) || value.content.some(block => block.type === "toolCall"))
                throw new Error("Invalid native summary response");
            return value;
        })();
        inFlight.add(response);
        void response.then(() => inFlight.delete(response), error => {
            if (!(error instanceof AcSummaryProviderFailure)) uncertainStream = true;
            inFlight.delete(response);
        });
        void response.then(() => visible.end(), () => visible.end());
        return { result: () => response,
            [Symbol.asyncIterator]: () => visible[Symbol.asyncIterator]() };
    };
    try {
        // Native Pi generation only: never call AgentSession.compact(), which
        // aborts turns, emits hooks and appends session entries.
        const result = await compact(preparation, binding.model, undefined, undefined,
            request.customInstructions, slot.controller.signal, "low", selectedStream, undefined,
            { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } },
            { onSummaryText: progress,
              onSummaryProgress: source => progress("", source),
              onSummaryStart: source => progress("", source),
              onSummaryResponse: (_usage, source) => progress("", source) }, undefined);
        await Promise.allSettled([...inFlight]);
        if (slot.controller.signal.aborted ||
            !acSummaryCurrent(session, request, binding) || !acSummaryValidResult(result, request))
            throw new Error("Summary completion invalid or state changed");
        return { version: 1, status: "summarized", operationId: request.operationId,
            witness: request.witness, selected: request.selected, settings: request.settings, result };
    } catch (error) {
        // A receipt covers the entire summary-only operation, including sibling
        // map streams. No provider failure can settle an unjoined/uncertain stream.
        await Promise.allSettled([...inFlight]);
        if (error instanceof AcSummaryProviderFailure && !uncertainStream &&
            !slot.controller.signal.aborted && acSummaryCurrent(session, request, binding))
            return error.outcome(request);
        const reason = error instanceof Error && error.message ? error.message : "Selected summary failed without error detail";
        return slot.started ? acSummaryUnknown(request.operationId, reason) :
            acSummaryDecline(request.operationId, slot.controller.signal.aborted ? "cancelled" : "unsupported");
    } finally {
        slot.controller.abort();
        await Promise.allSettled([...inFlight]); // concurrent map chunks must join before releasing slot
    }
}

export async function runRpcMode(runtimeHost) {
    takeOverStdout();
    let session = runtimeHost.session;
    let unsubscribe;
    let unsubscribeBackpressure;
    const output = (obj) => {
        writeRawStdout(serializeJsonLine(obj));
    };
    const success = (id, command, data) => {
        if (data === undefined) {
            return { id, type: "response", command, success: true };
        }
        return { id, type: "response", command, success: true, data };
    };
    const error = (id, command, message) => {
        return { id, type: "response", command, success: false, error: message };
    };
    // Pending extension UI requests waiting for response
    const pendingExtensionRequests = new Map();
    // Shutdown request flag
    let shutdownRequested = false;
    let shuttingDown = false;
    const signalCleanupHandlers = [];
    /** Helper for dialog methods with signal/timeout support */
    function createDialogPromise(opts, defaultValue, request, parseResponse) {
        if (opts?.signal?.aborted)
            return Promise.resolve(defaultValue);
        const id = crypto.randomUUID();
        return new Promise((resolve, reject) => {
            let timeoutId;
            const cleanup = () => {
                if (timeoutId)
                    clearTimeout(timeoutId);
                opts?.signal?.removeEventListener("abort", onAbort);
                pendingExtensionRequests.delete(id);
            };
            const onAbort = () => {
                cleanup();
                resolve(defaultValue);
            };
            opts?.signal?.addEventListener("abort", onAbort, { once: true });
            if (opts?.timeout) {
                timeoutId = setTimeout(() => {
                    cleanup();
                    resolve(defaultValue);
                }, opts.timeout);
            }
            pendingExtensionRequests.set(id, {
                resolve: (response) => {
                    cleanup();
                    resolve(parseResponse(response));
                },
                reject,
            });
            output({ type: "extension_ui_request", id, ...request });
        });
    }
    /**
     * Create an extension UI context that uses the RPC protocol.
     */
    const createExtensionUIContext = () => ({
        select: (title, options, opts) => createDialogPromise(opts, undefined, { method: "select", title, options, timeout: opts?.timeout }, (r) => "cancelled" in r && r.cancelled ? undefined : "value" in r ? r.value : undefined),
        confirm: (title, message, opts) => createDialogPromise(opts, false, { method: "confirm", title, message, timeout: opts?.timeout }, (r) => "cancelled" in r && r.cancelled ? false : "confirmed" in r ? r.confirmed : false),
        input: (title, placeholder, opts) => createDialogPromise(opts, undefined, { method: "input", title, placeholder, timeout: opts?.timeout }, (r) => "cancelled" in r && r.cancelled ? undefined : "value" in r ? r.value : undefined),
        notify(message, type) {
            // Fire and forget - no response needed
            output({
                type: "extension_ui_request",
                id: crypto.randomUUID(),
                method: "notify",
                message,
                notifyType: type,
            });
        },
        onTerminalInput() {
            // Raw terminal input not supported in RPC mode
            return () => { };
        },
        setStatus(key, text) {
            // Fire and forget - no response needed
            output({
                type: "extension_ui_request",
                id: crypto.randomUUID(),
                method: "setStatus",
                statusKey: key,
                statusText: text,
            });
        },
        setWorkingMessage(_message) {
            // Working message not supported in RPC mode - requires TUI loader access
        },
        setWorkingVisible(_visible) {
            // Working visibility not supported in RPC mode - requires TUI loader access
        },
        setWorkingIndicator(_options) {
            // Working indicator customization not supported in RPC mode - requires TUI loader access
        },
        setHiddenThinkingLabel(_label) {
            // Hidden thinking label not supported in RPC mode - requires TUI message rendering access
        },
        setWidget(key, content, options) {
            // Only support string arrays in RPC mode - factory functions are ignored
            if (content === undefined || Array.isArray(content)) {
                output({
                    type: "extension_ui_request",
                    id: crypto.randomUUID(),
                    method: "setWidget",
                    widgetKey: key,
                    widgetLines: content,
                    widgetPlacement: options?.placement,
                });
            }
            // Component factories are not supported in RPC mode - would need TUI access
        },
        setFooter(_factory) {
            // Custom footer not supported in RPC mode - requires TUI access
        },
        setHeader(_factory) {
            // Custom header not supported in RPC mode - requires TUI access
        },
        setTitle(title) {
            // Fire and forget - host can implement terminal title control
            output({
                type: "extension_ui_request",
                id: crypto.randomUUID(),
                method: "setTitle",
                title,
            });
        },
        async custom() {
            // Custom UI not supported in RPC mode
            return undefined;
        },
        pasteToEditor(text) {
            // Paste handling not supported in RPC mode - falls back to setEditorText
            this.setEditorText(text);
        },
        setEditorText(text) {
            // Fire and forget - host can implement editor control
            output({
                type: "extension_ui_request",
                id: crypto.randomUUID(),
                method: "set_editor_text",
                text,
            });
        },
        getEditorText() {
            // Synchronous method can't wait for RPC response
            // Host should track editor state locally if needed
            return "";
        },
        async editor(title, prefill) {
            const id = crypto.randomUUID();
            return new Promise((resolve, reject) => {
                pendingExtensionRequests.set(id, {
                    resolve: (response) => {
                        if ("cancelled" in response && response.cancelled) {
                            resolve(undefined);
                        }
                        else if ("value" in response) {
                            resolve(response.value);
                        }
                        else {
                            resolve(undefined);
                        }
                    },
                    reject,
                });
                output({ type: "extension_ui_request", id, method: "editor", title, prefill });
            });
        },
        addAutocompleteProvider() {
            // Autocomplete provider composition is not supported in RPC mode
        },
        setEditorComponent() {
            // Custom editor components not supported in RPC mode
        },
        getEditorComponent() {
            // Custom editor components not supported in RPC mode
            return undefined;
        },
        get theme() {
            return theme;
        },
        getAllThemes() {
            return [];
        },
        getTheme(_name) {
            return undefined;
        },
        setTheme(_theme) {
            // Theme switching not supported in RPC mode
            return { success: false, error: "Theme switching not supported in RPC mode" };
        },
        getToolsExpanded() {
            // Tool expansion not supported in RPC mode - no TUI
            return false;
        },
        setToolsExpanded(_expanded) {
            // Tool expansion not supported in RPC mode - no TUI
        },
    });
    runtimeHost.setRebindSession(async () => {
        await rebindSession();
    });
    const rebindSession = async () => {
        session = runtimeHost.session;
        await session.bindExtensions({
            uiContext: createExtensionUIContext(),
            mode: "rpc",
            commandContextActions: {
                waitForIdle: () => session.waitForIdle(),
                newSession: async (options) => runtimeHost.newSession(options),
                fork: async (entryId, forkOptions) => {
                    const result = await runtimeHost.fork(entryId, forkOptions);
                    return { cancelled: result.cancelled };
                },
                navigateTree: async (targetId, options) => {
                    const result = await session.navigateTree(targetId, {
                        summarize: options?.summarize,
                        customInstructions: options?.customInstructions,
                        replaceInstructions: options?.replaceInstructions,
                        label: options?.label,
                    });
                    return { cancelled: result.cancelled };
                },
                switchSession: async (sessionPath, options) => {
                    return runtimeHost.switchSession(sessionPath, options);
                },
                reload: async () => {
                    await session.reload();
                },
            },
            shutdownHandler: () => {
                shutdownRequested = true;
            },
            onError: (err) => {
                output({ type: "extension_error", extensionPath: err.extensionPath, event: err.event, error: err.error });
            },
        });
        unsubscribe?.();
        unsubscribeBackpressure?.();
        unsubscribe = session.subscribe((event) => {
            output(toJsonEvent(event));
            if (event.type === "agent_settled") {
                void checkShutdownRequested();
            }
        });
        unsubscribeBackpressure = session.agent.subscribe(async () => {
            await waitForRawStdoutBackpressure();
        });
    };
    const registerSignalHandlers = () => {
        const signals = ["SIGTERM"];
        if (process.platform !== "win32") {
            signals.push("SIGHUP");
        }
        for (const signal of signals) {
            const handler = () => {
                killTrackedDetachedChildren();
                void shutdown(signal === "SIGHUP" ? 129 : 143, signal);
            };
            process.on(signal, handler);
            signalCleanupHandlers.push(() => process.off(signal, handler));
        }
    };
    await rebindSession();
    registerSignalHandlers();
    let acOtherCommandInFlight = 0;
    let acSummarySlot = null;
    const acSpentSummaryIds = new Set();
    // Handle a single command
    const handleCommand = async (command) => {
        const id = command.id;
        // Reserve synchronously before ANY await: RPC dispatches concurrent lines.
        if (acSummarySlot && !["agent_comms_cancel_summary", "agent_comms_summarize_compaction",
            "agent_comms_compaction_settings", "get_state"].includes(command.type))
            return error(id, command.type, "Selected summary in flight; mutation denied");
        switch (command.type) {
            // =================================================================
            // Prompting
            // =================================================================
            case "prompt": {
                // Start prompt handling immediately, but emit the authoritative response only after
                // prompt preflight succeeds. Queued and immediately handled prompts also count as success.
                let preflightSucceeded = false;
                // Prompt preflight may await auth/extensions before isStreaming.
                // Retain the mutation fence until the entire promise settles.
                acOtherCommandInFlight++;
                void session
                    .prompt(command.message, {
                    images: command.images,
                    streamingBehavior: command.streamingBehavior,
                    source: "rpc",
                    inputId: command.inputId,
                    preflightResult: (didSucceed) => {
                        if (didSucceed) {
                            preflightSucceeded = true;
                            output(success(id, "prompt"));
                        }
                    },
                })
                    .catch((e) => {
                    if (!preflightSucceeded) {
                        output(error(id, "prompt", e.message));
                    }
                })
                    .finally(() => { acOtherCommandInFlight--; });
                return undefined;
            }
            case "interrupt_steering": {
                return success(id, "interrupt_steering", { interrupted: session.interruptSteering(command.inputIds) });
            }
            case "steer": {
                await session.steer(command.message, command.images, command.inputId);
                return success(id, "steer");
            }
            case "follow_up": {
                await session.followUp(command.message, command.images, command.inputId);
                return success(id, "follow_up");
            }
            case "abort": {
                await session.abort();
                return success(id, "abort");
            }
            case "clear_queue": {
                return success(id, "clear_queue", session.clearQueue());
            }
            case "new_session": {
                const options = command.parentSession ? { parentSession: command.parentSession } : undefined;
                const result = await runtimeHost.newSession(options);
                if (!result.cancelled) {
                    await rebindSession();
                }
                return success(id, "new_session", result);
            }
            // =================================================================
            // State
            // =================================================================
            case "agent_comms_compaction_settings": {
                if (!acValidCompactionSettingsRequest(command))
                    return error(id, command.type, "Invalid selected compaction settings request");
                return success(id, command.type, acSelectedCompactionSettings(command, session,
                    acSummarySlot !== null || acOtherCommandInFlight !== 0));
            }
            case "agent_comms_summarize_compaction": {
                if (!acValidSummaryRequest(command))
                    return error(id, command.type, "Invalid v1 selected-summary request");
                const admission = acAdmitSummary(command, session,
                    acSummarySlot !== null || acOtherCommandInFlight !== 0,
                    acSpentSummaryIds, runtimeHost);
                if (admission.denial)
                    return success(id, command.type, acSummaryDecline(command.operationId, admission.denial));
                const selectedSession = session;
                const slot = { operationId: command.operationId, controller: new AbortController(),
                    started: false, done: null };
                acSummarySlot = slot;
                acSpentSummaryIds.add(command.operationId);
                slot.done = acExecuteSummary(slot, selectedSession, command,
                    admission.preparation, admission.binding, output).finally(() => {
                    if (acSummarySlot === slot) acSummarySlot = null;
                });
                return success(id, command.type, await slot.done);
            }
            case "agent_comms_cancel_summary": {
                if (!acValidSummaryCancel(command))
                    return error(id, command.type, "Invalid v1 selected-summary cancellation");
                const slot = acSummarySlot;
                if (!slot || slot.operationId !== command.operationId)
                    return success(id, command.type, acSpentSummaryIds.has(command.operationId)
                        ? acSummaryUnknown(command.operationId)
                        : acSummaryDecline(command.operationId, "unsupported"));
                slot.controller.abort();
                await slot.done; // NEVER free the slot before all concurrent chunks join.
                return success(id, command.type, slot.started
                    ? acSummaryUnknown(command.operationId)
                    : acSummaryDecline(command.operationId, "cancelled"));
            }
            case "get_state": {
                const state = {
                    model: session.model,
                    thinkingLevel: session.thinkingLevel,
                    isStreaming: session.isStreaming,
                    isCompacting: session.isCompacting || acSummarySlot !== null,
                    steeringMode: session.steeringMode,
                    followUpMode: session.followUpMode,
                    sessionFile: session.sessionFile,
                    sessionId: session.sessionId,
                    sessionName: session.sessionName,
                    autoCompactionEnabled: session.autoCompactionEnabled,
                    messageCount: session.messages.length,
                    pendingMessageCount: session.pendingMessageCount,
                    nativeInputProofCapability: session.sessionManager.nativeInputProofAvailable() ? "pi-native-input-v1-live-only" : null,
                };
                return success(id, "get_state", state);
            }
            // =================================================================
            // Model
            // =================================================================
            case "set_model": {
                const models = session.modelRuntime.getAvailableSnapshot();
                const model = models.find((m) => m.provider === command.provider && m.id === command.modelId);
                if (!model) {
                    return error(id, "set_model", `Model not found: ${command.provider}/${command.modelId}`);
                }
                await session.setModel(model);
                return success(id, "set_model", model);
            }
            case "cycle_model": {
                const result = await session.cycleModel();
                if (!result) {
                    return success(id, "cycle_model", null);
                }
                return success(id, "cycle_model", result);
            }
            case "get_available_models": {
                const models = session.modelRuntime.getAvailableSnapshot();
                return success(id, "get_available_models", { models });
            }
            // =================================================================
            // Thinking
            // =================================================================
            case "set_thinking_level": {
                session.setThinkingLevel(command.level);
                return success(id, "set_thinking_level");
            }
            case "cycle_thinking_level": {
                const level = session.cycleThinkingLevel();
                if (!level) {
                    return success(id, "cycle_thinking_level", null);
                }
                return success(id, "cycle_thinking_level", { level });
            }
            case "get_available_thinking_levels": {
                const levels = session.getAvailableThinkingLevels();
                return success(id, "get_available_thinking_levels", { levels });
            }
            // =================================================================
            // Queue Modes
            // =================================================================
            case "set_steering_mode": {
                session.setSteeringMode(command.mode);
                return success(id, "set_steering_mode");
            }
            case "set_follow_up_mode": {
                session.setFollowUpMode(command.mode);
                return success(id, "set_follow_up_mode");
            }
            // =================================================================
            // Compaction
            // =================================================================
            case "compact": {
                const result = await session.compact(command.customInstructions);
                return success(id, "compact", result);
            }
            case "set_auto_compaction": {
                session.setAutoCompactionEnabled(command.enabled);
                return success(id, "set_auto_compaction");
            }
            // =================================================================
            // Retry
            // =================================================================
            case "set_auto_retry": {
                session.setAutoRetryEnabled(command.enabled);
                return success(id, "set_auto_retry");
            }
            case "abort_retry": {
                session.abortRetry();
                return success(id, "abort_retry");
            }
            // =================================================================
            // Bash
            // =================================================================
            case "bash": {
                const eventResult = await session.extensionRunner.emitUserBash({
                    type: "user_bash",
                    command: command.command,
                    excludeFromContext: command.excludeFromContext ?? false,
                    cwd: session.sessionManager.getCwd(),
                });
                if (eventResult?.result) {
                    session.recordBashResult(command.command, eventResult.result, {
                        excludeFromContext: command.excludeFromContext,
                    });
                    return success(id, "bash", eventResult.result);
                }
                const result = await session.executeBash(command.command, undefined, {
                    excludeFromContext: command.excludeFromContext,
                    id,
                    operations: eventResult?.operations,
                });
                return success(id, "bash", result);
            }
            case "abort_bash": {
                session.abortBash();
                return success(id, "abort_bash");
            }
            // =================================================================
            // Session
            // =================================================================
            case "get_session_stats": {
                const stats = session.getSessionStats();
                return success(id, "get_session_stats", stats);
            }
            case "export_html": {
                const path = await session.exportToHtml(command.outputPath);
                return success(id, "export_html", { path });
            }
            case "switch_session": {
                const result = await runtimeHost.switchSession(command.sessionPath);
                if (!result.cancelled) {
                    await rebindSession();
                }
                return success(id, "switch_session", result);
            }
            case "fork": {
                const result = await runtimeHost.fork(command.entryId);
                if (!result.cancelled) {
                    await rebindSession();
                }
                return success(id, "fork", { text: result.selectedText, cancelled: result.cancelled });
            }
            case "clone": {
                const leafId = session.sessionManager.getLeafId();
                if (!leafId) {
                    return error(id, "clone", "Cannot clone session: no current entry selected");
                }
                const result = await runtimeHost.fork(leafId, { position: "at" });
                if (!result.cancelled) {
                    await rebindSession();
                }
                return success(id, "clone", { cancelled: result.cancelled });
            }
            case "get_fork_messages": {
                const messages = session.getUserMessagesForForking();
                return success(id, "get_fork_messages", { messages });
            }
            case "get_entries": {
                const sessionManager = session.sessionManager;
                let entries = sessionManager.getEntries();
                if (command.since !== undefined) {
                    const sinceIndex = entries.findIndex((e) => e.id === command.since);
                    if (sinceIndex === -1) {
                        return error(id, "get_entries", `Entry not found: ${command.since}`);
                    }
                    entries = entries.slice(sinceIndex + 1);
                }
                return success(id, "get_entries", { entries, leafId: sessionManager.getLeafId() });
            }
            case "get_tree": {
                const sessionManager = session.sessionManager;
                return success(id, "get_tree", { tree: sessionManager.getTree(), leafId: sessionManager.getLeafId() });
            }
            case "get_last_assistant_text": {
                const text = session.getLastAssistantText();
                return success(id, "get_last_assistant_text", { text });
            }
            case "set_session_name": {
                const name = command.name.trim();
                if (!name) {
                    return error(id, "set_session_name", "Session name cannot be empty");
                }
                session.setSessionName(name);
                return success(id, "set_session_name");
            }
            // =================================================================
            // Messages
            // =================================================================
            case "get_messages": {
                return success(id, "get_messages", { messages: session.messages });
            }
            // =================================================================
            // Commands (available for invocation via prompt)
            // =================================================================
            case "get_commands": {
                const commands = [];
                for (const command of session.extensionRunner.getRegisteredCommands()) {
                    commands.push({
                        name: command.invocationName,
                        description: command.description,
                        source: "extension",
                        sourceInfo: command.sourceInfo,
                    });
                }
                for (const template of session.promptTemplates) {
                    commands.push({
                        name: template.name,
                        description: template.description,
                        source: "prompt",
                        sourceInfo: template.sourceInfo,
                    });
                }
                for (const skill of session.resourceLoader.getSkills().skills) {
                    commands.push({
                        name: `skill:${skill.name}`,
                        description: skill.description,
                        source: "skill",
                        sourceInfo: skill.sourceInfo,
                    });
                }
                return success(id, "get_commands", { commands });
            }
            default: {
                const unknownCommand = command;
                return error(id, unknownCommand.type, `Unknown command: ${unknownCommand.type}`);
            }
        }
    };
    /**
     * Check if shutdown was requested and perform shutdown if so.
     * Called after handling each command when waiting for the next command.
     */
    let detachInput = () => { };
    async function shutdown(exitCode = 0, signal) {
        if (shuttingDown) {
            process.exit(exitCode);
        }
        shuttingDown = true;
        for (const cleanup of signalCleanupHandlers) {
            cleanup();
        }
        unsubscribe?.();
        unsubscribeBackpressure?.();
        await runtimeHost.dispose();
        detachInput();
        process.stdin.pause();
        if (signal !== "SIGTERM") {
            await flushRawStdout();
        }
        process.exit(exitCode);
    }
    async function checkShutdownRequested() {
        if (!shutdownRequested)
            return;
        await shutdown();
    }
    const handleInputLine = async (line) => {
        let parsed;
        try {
            parsed = JSON.parse(line);
        }
        catch (parseError) {
            output(error(undefined, "parse", `Failed to parse command: ${parseError instanceof Error ? parseError.message : String(parseError)}`));
            await waitForRawStdoutBackpressure();
            return;
        }
        // Handle extension UI responses
        if (typeof parsed === "object" &&
            parsed !== null &&
            "type" in parsed &&
            parsed.type === "extension_ui_response") {
            const response = parsed;
            const pending = pendingExtensionRequests.get(response.id);
            if (pending) {
                pendingExtensionRequests.delete(response.id);
                pending.resolve(response);
            }
            return;
        }
        const command = parsed;
        const acCountCommand = !["agent_comms_summarize_compaction", "agent_comms_cancel_summary",
            "agent_comms_compaction_settings", "get_state"].includes(command?.type);
        if (acCountCommand) acOtherCommandInFlight++;
        try {
            const response = await handleCommand(command);
            if (response) {
                output(response);
                await waitForRawStdoutBackpressure();
            }
            await checkShutdownRequested();
        }
        catch (commandError) {
            output(error(command?.id, command?.type ?? "parse", commandError instanceof Error ? commandError.message : String(commandError)));
            await waitForRawStdoutBackpressure();
        }
        finally {
            if (acCountCommand) acOtherCommandInFlight--;
        }
    };
    const onInputEnd = () => {
        void shutdown();
    };
    process.stdin.on("end", onInputEnd);
    detachInput = (() => {
        const detachJsonl = attachJsonlLineReader(process.stdin, (line) => {
            void handleInputLine(line);
        });
        return () => {
            detachJsonl();
            process.stdin.off("end", onInputEnd);
        };
    })();
    // Keep process alive forever
    return new Promise(() => { });
}
//# sourceMappingURL=rpc-mode.js.map