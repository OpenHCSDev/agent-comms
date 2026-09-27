// Injected only into an exact-byte disposable Pi RPC build. Readiness is
// observational; it is NOT route/auth parity or paid-summary authorization.
const acReadinessRouteStatus = "UNVERIFIED_NO_AUTH_RESOLUTION";
function acExactObject(value, keys) {
    return value !== null && typeof value === "object" && !Array.isArray(value) &&
        Object.keys(value).sort().join(",") === [...keys].sort().join(",");
}
function acValidReadiness(command) {
    const nonempty = value => typeof value === "string" && value.length > 0 && value.length <= 4096;
    const integer = value => Number.isSafeInteger(value) && value >= 0;
    return acExactObject(command, ["id", "type", "version", "dryRun", "witness", "selected", "settings"]) &&
        nonempty(command.id) && command.type === "agent_comms_prepare_compaction" &&
        command.version === 1 && command.dryRun === true &&
        acExactObject(command.witness, ["sessionId", "sessionFile", "leafId", "firstKeptEntryId", "revision"]) &&
        ["sessionId", "sessionFile", "leafId", "firstKeptEntryId", "revision"].every(key =>
            nonempty(command.witness[key])) &&
        acExactObject(command.selected, ["provider", "modelId", "contextWindow"]) &&
        nonempty(command.selected.provider) && nonempty(command.selected.modelId) &&
        integer(command.selected.contextWindow) && command.selected.contextWindow > 0 &&
        acExactObject(command.settings, ["reserveTokens", "keepRecentTokens"]) &&
        integer(command.settings.reserveTokens) && integer(command.settings.keepRecentTokens);
}
function acPrepareReadiness(command, session, conflictingCommand) {
    const decline = reason => ({ version: 1, status: "declined", reason });
    if (session.isCompacting) return decline("compacting");
    if (conflictingCommand || !session.isIdle || session.isStreaming || session.isRetrying ||
        session._retryAttempt || session._nativeInterruptIds) return decline("busy");
    if (session.pendingMessageCount || session.agent.steeringQueue.messages.length ||
        session.agent.followUpQueue.messages.length || session._pendingNextTurnMessages.length ||
        session._pendingCustomMessages.length || session._pendingBashMessages.length)
        return decline("queue_nonempty");
    const manager = session.sessionManager;
    if (!manager || !session.sessionFile || !session.sessionId ||
        command.witness.sessionId !== session.sessionId ||
        command.witness.sessionFile !== session.sessionFile ||
        command.witness.leafId !== manager.getLeafId()) return decline("source_mismatch");
    const model = session.model;
    if (!model || model.provider !== command.selected.provider || model.id !== command.selected.modelId ||
        model.contextWindow !== command.selected.contextWindow) return decline("model_mismatch");
    const catalog = session.modelRuntime.getAvailableSnapshot();
    if (!Array.isArray(catalog) || !catalog.some(item => item.provider === model.provider &&
        item.id === model.id && item.contextWindow === model.contextWindow)) return decline("unsupported");
    const settings = session.settingsManager.getCompactionSettings();
    if (!settings || settings.reserveTokens !== command.settings.reserveTokens ||
        settings.keepRecentTokens !== command.settings.keepRecentTokens) return decline("settings_mismatch");
    let witness;
    try { witness = manager.captureCompactionWitness(command.witness.firstKeptEntryId); }
    catch { return decline("source_mismatch"); }
    if (!acExactObject(witness, ["sessionId", "sessionFile", "leafId", "firstKeptEntryId", "revision"]) ||
        Object.keys(command.witness).some(key => witness[key] !== command.witness[key]))
        return decline("source_mismatch");
    let preparation;
    try { preparation = prepareCompaction(manager.getBranch(), settings); }
    catch { return decline("unsupported"); }
    if (!preparation || preparation.firstKeptEntryId !== command.witness.firstKeptEntryId)
        return decline("source_mismatch");
    if (preparation.isSplitTurn || preparation.turnPrefixMessages.length) return decline("split_turn");
    return { version: 1, status: "ready", routeStatus: acReadinessRouteStatus,
        witness: command.witness, selected: command.selected, settings: command.settings };
}
