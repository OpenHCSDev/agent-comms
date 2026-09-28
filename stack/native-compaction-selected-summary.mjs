// Injected into the exact disposable Pi RPC build, never the installed pin.
// One operation is NOT a replay grant. Python must journal its ID before send.
const acSummaryId = value => typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
const acNativeSummaryResult = AssistantMessageEventStream.prototype.result;
// Read the actual selected SettingsManager: it already owns project trust,
// migrations and in-memory overrides. No detached reader guesses that state.
function acValidCompactionSettingsRequest(command) {
    const text = value => typeof value === "string" && value.length > 0 && value.length <= 4096;
    return acExactObject(command, ["id", "type", "version", "sessionId", "sessionFile", "selected", "contextTokens"]) &&
        command.type === "agent_comms_compaction_settings" && command.version === 1 && text(command.id) &&
        text(command.sessionId) && text(command.sessionFile) &&
        acExactObject(command.selected, ["provider", "modelId", "contextWindow"]) &&
        text(command.selected.provider) && text(command.selected.modelId) &&
        Number.isSafeInteger(command.selected.contextWindow) && command.selected.contextWindow > 0 &&
        Number.isSafeInteger(command.contextTokens) && command.contextTokens >= 0;
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
    return {version: 1, sessionId: session.sessionId, sessionFile: session.sessionFile,
        selected: command.selected, contextTokens: command.contextTokens,
        decision: {enabled: settings.enabled, reserveTokens: settings.reserveTokens,
            keepRecentTokens: settings.keepRecentTokens,
            trigger: shouldCompact(command.contextTokens, model.contextWindow, settings)}};
}
function acValidSummaryRequest(value) {
    const fields = ["id", "type", "version", "operationId", "witness", "selected", "settings"];
    if (value && Object.hasOwn(value, "customInstructions")) {
        if (typeof value.customInstructions !== "string" || !value.customInstructions.isWellFormed()) return false;
        fields.push("customInstructions");
    }
    if (!acExactObject(value, fields) ||
        value.type !== "agent_comms_summarize_compaction" || !acSummaryId(value.operationId)) return false;
    const readiness = { id: value.id, type: "agent_comms_prepare_compaction", version: value.version,
        dryRun: true, witness: value.witness, selected: value.selected, settings: value.settings };
    return acValidReadiness(readiness);
}
function acValidSummaryCancel(value) {
    return acExactObject(value, ["id", "type", "version", "operationId"]) &&
        typeof value.id === "string" && value.id.length > 0 && value.id.length <= 4096 &&
        value.type === "agent_comms_cancel_summary" && value.version === 1 && acSummaryId(value.operationId);
}
const acSummaryDecline = (operationId, reason) => ({ version: 1, status: "declined", operationId, reason });
// Failure detail is diagnostic only: it never changes UNKNOWN or grants replay.
const acSummaryUnknown = (operationId, reason) => ({ version: 1, status: "unknown", operationId,
    ...(reason === undefined ? {} : { reason: reason.replace(/[\u0000-\u001f\u007f]/g, " ")
        .trim().slice(0, 1024).toWellFormed() }) });
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
        const preparation = prepareCompaction(session.sessionManager.getBranch(), settings);
        return preparation && !preparation.isSplitTurn && preparation.turnPrefixMessages.length === 0 &&
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
        typeof result.summary === "string" && result.summary.trim().length > 0 &&
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
    const readiness = acPrepareReadiness({ ...request, type: "agent_comms_prepare_compaction", dryRun: true },
        session, false);
    if (readiness.status !== "ready") return { denial: readiness.reason };
    if (!acSummaryCompatible(session)) return { denial: "extension_unsupported" };
    let preparation;
    try { preparation = prepareCompaction(session.sessionManager.getBranch(),
        session.settingsManager.getCompactionSettings()); }
    catch { return { denial: "unsupported" }; }
    if (!preparation || preparation.isSplitTurn || preparation.turnPrefixMessages.length ||
        preparation.firstKeptEntryId !== request.witness.firstKeptEntryId) return { denial: "source_mismatch" };
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
    let progressSequence = 0;
    const progress = () => output({ type: "agent_comms_compaction_progress",
        id: request.id, operationId: request.operationId, sequence: ++progressSequence });
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
                        type === "error" || ((type === "text_delta" || type === "thinking_delta") && event.delta.length)))
                        progress();
                    if (type === "done" || type === "error") terminal = event;
                }
            } catch (error) {
                slot.controller.abort();
                throw error;
            } finally {
                // Consumer iterator closure is NOT provider completion. On
                // malformed events, parser errors or cancellation, join the
                // original pinned stream terminal before the slot can clear.
                try { await providerTerminal; }
                catch {
                    // Rejection is not completion: a forged or broken terminal
                    // can fail early while provider work continues.
                    slot.controller.abort();
                    await new Promise(() => {});
                }
            }
            if (invalidEvent)
                throw new Error("Selected summary stream contained unsupported events");
            if (terminal?.type === "error")
                throw new Error(typeof terminal.error?.errorMessage === "string" && terminal.error.errorMessage.trim()
                    ? terminal.error.errorMessage : `Selected summary provider stopped: ${terminal.reason}`);
            if (!sawStart || terminal?.type !== "done" || terminal.reason !== "stop")
                throw new Error("Selected summary stream ended without a complete response");
            const value = terminal.message;
            if (!value || value.stopReason !== "stop" || !acSummaryValidUsage(value.usage) ||
                !Array.isArray(value.content) || value.content.some(block => block.type === "toolCall"))
                throw new Error("Invalid native summary response");
            if (!Number.isSafeInteger(options.maxTokens) || options.maxTokens < 1 ||
                value.usage.output > options.maxTokens)
                throw new Error("Summary provider exceeded the native plan output token budget");
            return value;
        })();
        inFlight.add(response);
        void response.then(() => inFlight.delete(response), () => inFlight.delete(response));
        return { result: () => response };
    };
    try {
        // Native Pi generation only: never call AgentSession.compact(), which
        // aborts turns, emits hooks and appends session entries.
        const result = await compact(preparation, binding.model, undefined, undefined,
            request.customInstructions, slot.controller.signal, "low", selectedStream, undefined,
            { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } }, undefined, undefined);
        await Promise.allSettled([...inFlight]);
        if (slot.controller.signal.aborted ||
            !acSummaryCurrent(session, request, binding) || !acSummaryValidResult(result, request))
            throw new Error("Summary completion invalid or state changed");
        return { version: 1, status: "summarized", operationId: request.operationId,
            witness: request.witness, selected: request.selected, settings: request.settings, result };
    } catch (error) {
        const reason = error instanceof Error && error.message ? error.message : "Selected summary failed without error detail";
        return slot.started ? acSummaryUnknown(request.operationId, reason) :
            acSummaryDecline(request.operationId, slot.controller.signal.aborted ? "cancelled" : "unsupported");
    } finally {
        slot.controller.abort();
        await Promise.allSettled([...inFlight]); // concurrent map chunks must join before releasing slot
    }
}
