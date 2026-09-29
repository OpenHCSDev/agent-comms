/** Session context admission: history stays in EntryStore until the native policy can load it. */
import { estimateTokens } from './compaction/compaction.js';
import { sessionEntryToContextMessages } from './session-manager.js';

export class SessionContext {
    constructor(manager) {
        if(new.target === SessionContext)throw new TypeError('Concrete session context required');
        this.manager=manager;
    }
    static restore(session) {
        const manager=session.sessionManager;
        const model=session.model;
        const settings=session.settingsManager.getCompactionSettings();
        // AgentSession owns measured selected-branch usage. After compaction it
        // deliberately reports unknown until a new assistant response: estimate
        // that current context without reusing stale pre-compaction usage.
        const tokens=session.getContextUsage()?.tokens ?? manager.buildContextEntries()
            .flatMap(sessionEntryToContextMessages).reduce((total, message)=>total+estimateTokens(message), 0);
        if (!model || tokens > model.contextWindow - settings.reserveTokens) {
                session.storedContext=new CompactionContext(manager);
                session.storedContext.install(session.agent);
                return session.storedContext;
        }
        session.storedContext=new ReadyContext(manager);
        session.storedContext.install(session.agent);
        return session.storedContext;
    }
    install(agent) { throw new Error('Concrete session context required'); }
    requireReady() { throw new Error('Native compaction did not admit the retained context'); }
    messages(agent) { throw new Error("Concrete context messages required"); }
    requiresCompaction() { return false; }
    summaryDeclineReason(reason) { throw new Error("Concrete context admission required"); }
    async beforeInput(session) {}
}
export class ReadyContext extends SessionContext {
    summaryDeclineReason(reason) { return reason; }
    requireReady() {}
    messages(agent) { return agent.state.messages.values(); }
    install(agent) {
        agent.state.messages=this.manager.buildContextEntries().flatMap(sessionEntryToContextMessages).toArray();
    }
}
export class CompactionContext extends SessionContext {
    summaryDeclineReason(reason) { return "context_requires_compaction"; }
    requiresCompaction() { return true; }
    messages() { return this.manager.buildContextEntries().flatMap(sessionEntryToContextMessages); }
    install(agent) { agent.state.messages=[]; }
    async beforeInput(session) {
        // The canonical Python owner must reserve/commit before any original input.
        if(process.env.AGENT_COMMS_NATIVE_CONFIG_DIR)
            throw new Error('Stored native context requires journaled owner compaction before input');
        await session.compact();
        session.storedContext.requireReady();
    }
}
