// Injected into the exact disposable Pi RPC build, never the installed pin.
// One operation is NOT a replay grant. Python must journal its ID before send.
const acSummaryLimits = Object.freeze({ calls: 4, deadlineMs: 90000,
    sourceBytes: 1048576, outputBytes: 262144 });
const acSummaryId = value => typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
const acNativeSummaryResult = AssistantMessageEventStream.prototype.result;
function acValidSummaryRequest(value) {
    if (!acExactObject(value, ["id", "type", "version", "operationId", "witness", "selected", "settings"]) ||
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
const acSummaryUnknown = operationId => ({ version: 1, status: "unknown", operationId });
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
function acSummarySourceBytes(preparation) {
    // Reject giant inputs before Pi serialization. Then measure Pi's actual
    // summary source, not an unrelated JSON/characters-per-token estimate.
    const raw = JSON.stringify([preparation.messagesToSummarize, preparation.previousSummary]);
    if (!raw || Buffer.byteLength(raw, "utf8") > acSummaryLimits.sourceBytes) return Infinity;
    const transcript = serializeConversation(convertToLlm(preparation.messagesToSummarize));
    return Buffer.byteLength(transcript, "utf8") +
        (preparation.previousSummary === undefined ? 0 :
            Buffer.byteLength(`<previous-summary>\n${preparation.previousSummary}\n</previous-summary>\n\n`, "utf8"));
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
    const files = paths => Array.isArray(paths) && paths.length <= 256 && paths.every(path =>
        typeof path === "string" && path.length > 0 && path.isWellFormed() &&
        Buffer.byteLength(path, "utf8") <= 4096 && !path.includes("\0"));
    return acExactObject(result, ["summary", "firstKeptEntryId", "tokensBefore", "usage", "details"]) &&
        typeof result.summary === "string" && result.summary.trim().length > 0 &&
        result.summary.isWellFormed() && Buffer.byteLength(result.summary, "utf8") <= acSummaryLimits.outputBytes &&
        result.firstKeptEntryId === request.witness.firstKeptEntryId &&
        Number.isSafeInteger(result.tokensBefore) && result.tokensBefore >= 0 &&
        acExactObject(result.details, ["readFiles", "modifiedFiles"]) &&
        files(result.details.readFiles) && files(result.details.modifiedFiles) && acSummaryValidUsage(result.usage);
}
// Synchronous admission/preparation has no auth, hooks, provider, or native append.
function acAdmitSummary(request, session, conflict, spent, host) {
    if (spent.has(request.operationId)) return { denial: "duplicate_operation" };
    if (spent.size >= 1024) return { denial: "limit_exceeded" };
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
    let bytes;
    try { bytes = acSummarySourceBytes(preparation); }
    catch { return { denial: "unsupported" }; }
    if (bytes > acSummaryLimits.sourceBytes) return { denial: "limit_exceeded" };
    const binding = { host, runner: session.extensionRunner, streamFn: session.agent.streamFunction,
        model: session.model, modelRuntime: session.modelRuntime,
        settingsManager: session.settingsManager, manager: session.sessionManager,
        catalog: session.modelRuntime.getAvailableSnapshot() };
    if (typeof binding.streamFn !== "function" || !acSummaryCurrent(session, request, binding))
        return { denial: "source_mismatch" };
    return { preparation, binding };
}
async function acExecuteSummary(slot, session, request, preparation, binding) {
    const inFlight = new Set();
    let calls = 0;
    let responseBytes = 0;
    const deadline = Date.now() + acSummaryLimits.deadlineMs;
    const timer = setTimeout(() => { slot.timedOut = true; slot.controller.abort(); }, acSummaryLimits.deadlineMs);
    const selectedStream = (model, context, options) => {
        // No standalone getAuth: actual selected streamFn resolves auth on use.
        if (slot.controller.signal.aborted || Date.now() >= deadline ||
            !acSummaryCurrent(session, request, binding) || model !== binding.model ||
            !acSummaryCompatible(session, binding)) throw new Error("Selected route changed before call");
        if (calls >= acSummaryLimits.calls) { slot.controller.abort(); throw new Error("Summary call limit"); }
        calls++;
        // Everything after this line, including auth/header hooks, is possibly
        // spent even if the fake transport observes zero network requests.
        slot.started = true;
        let stream;
        try {
            stream = binding.streamFn(model, context, { ...options, maxRetries: 0,
                signal: slot.controller.signal, timeoutMs: Math.max(1, deadline - Date.now()) });
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
            let overBudget = false;
            let callDeltaBytes = 0;
            // Consume the pinned AssistantMessageEventStream itself. Its
            // `result()` buffers everything before returning; deltas permit an
            // early abort request. We STILL drain/join through terminal, even
            // after an oversized chunk or tool call, before releasing the slot.
            try {
                for await (const event of source) {
                    const type = typeof event?.type === "string" ? event.type : null;
                    if (!type) overBudget = true;
                    if (type === "start") sawStart = true;
                    if (["text_delta", "thinking_delta", "toolcall_delta"].includes(type)) {
                        if (typeof event.delta !== "string") overBudget = true;
                        else {
                            const bytes = Buffer.byteLength(event.delta, "utf8");
                            callDeltaBytes += bytes;
                            responseBytes += bytes;
                        }
                        if (responseBytes > acSummaryLimits.outputBytes) overBudget = true;
                    }
                    if (type?.startsWith("toolcall_")) overBudget = true;
                    if (overBudget && !slot.controller.signal.aborted) slot.controller.abort();
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
            if (overBudget || !sawStart || terminal?.type !== "done" || terminal.reason !== "stop")
                throw new Error("Incomplete or oversized selected stream");
            const value = terminal.message;
            if (!value || value.stopReason !== "stop" || !acSummaryValidUsage(value.usage) ||
                !Array.isArray(value.content) || value.content.some(block => block.type === "toolCall"))
                throw new Error("Invalid native summary response");
            // A provider can emit a large single chunk, or omit deltas. The
            // final-result check is a separate conservative ceiling, NOT a
            // network/heap cap on untrusted provider internals.
            const finalBytes = Buffer.byteLength(JSON.stringify(value.content), "utf8");
            responseBytes += Math.max(0, finalBytes - callDeltaBytes);
            if (responseBytes > acSummaryLimits.outputBytes)
                throw new Error("Native summary result too large");
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
            undefined, slot.controller.signal, "low", selectedStream, undefined,
            { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } }, undefined, undefined);
        await Promise.allSettled([...inFlight]);
        if (slot.controller.signal.aborted || slot.timedOut || Date.now() >= deadline ||
            !acSummaryCurrent(session, request, binding) || !acSummaryValidResult(result, request))
            throw new Error("Summary completion invalid or state changed");
        return { version: 1, status: "summarized", operationId: request.operationId,
            witness: request.witness, selected: request.selected, settings: request.settings, result };
    } catch {
        return slot.started ? acSummaryUnknown(request.operationId) :
            acSummaryDecline(request.operationId, slot.controller.signal.aborted ? "cancelled" : "unsupported");
    } finally {
        slot.controller.abort();
        await Promise.allSettled([...inFlight]); // concurrent map chunks must join before releasing slot
        clearTimeout(timer);
    }
}
