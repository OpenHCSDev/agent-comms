/** Session context admission: history stays in EntryStore until the native policy can load it. */
import { CompactionPolicy } from './compaction/agent-comms-policy.js';
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
        const policy=CompactionPolicy.fromEnvironment();
        if (!model || !policy.contextFits(
            manager.buildContextEntries().flatMap(sessionEntryToContextMessages), model, settings.reserveTokens)) {
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
    async beforeInput(session) {}
}
export class ReadyContext extends SessionContext {
    requireReady() {}
    messages(agent) { return agent.state.messages.values(); }
    install(agent) {
        agent.state.messages=this.manager.buildContextEntries().flatMap(sessionEntryToContextMessages).toArray();
    }
}
export class CompactionContext extends SessionContext {
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
