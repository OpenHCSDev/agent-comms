/** Session context admission: history stays in EntryStore until the native policy can load it. */
import { ContextBudget } from '../../node_modules/@earendil-works/pi-ai/dist/api/agent-comms-context-budget.js';
import { sessionEntryToContextMessages } from './session-manager.js';
import { convertToLlm } from './messages.js';

export class SessionContext {
    constructor(manager) {
        if(new.target === SessionContext)throw new TypeError('Concrete session context required');
        this.manager=manager;
    }
    static restore(session) {
        const manager=session.sessionManager;
        const model=session.model;
        const settings=session.settingsManager.getCompactionSettings();
        const messages=model ? this.sourceMessages(manager).toArray() : [];
        session.storedContext=(!model || this.sourceBudget(session, messages).compactionRequired(settings))
            ? new CompactionContext(manager)
            : new ReadyContext(manager);
        session.storedContext.install(session.agent, messages);
        return session.storedContext;
    }
    static sourceMessages(manager) {
        return manager.buildContextEntries().flatMap(sessionEntryToContextMessages);
    }
    static sourceBudget(session, messages) {
        return new ContextBudget(session.model, this.sourceContext(session, convertToLlm(messages)));
    }
    static sourceContext(session, messages) {
        return {
            systemPrompt: session.systemPrompt,
            messages,
            tools: session.agent.state.tools,
        };
    }
    static async prefixContext(session, messages) {
        // Use the original SDK converter (including configured image exclusion),
        // not a Python narrative or an independently captured prompt body.
        return this.sourceContext(session, await session.agent.convertToLlm(Array.from(messages)));
    }
    compactionRequired(session, settings) {
        return SessionContext.sourceBudget(session, SessionContext.sourceMessages(this.manager).toArray())
            .compactionRequired(settings);
    }
    install(agent, messages) { throw new Error('Concrete session context required'); }
    requireReady() { throw new Error('Native compaction did not admit the retained context'); }
    messages(agent) { throw new Error("Concrete context messages required"); }
    messageCount(agent) { throw new Error("Concrete context message count required"); }
    async beforeInput(session) {}
}
export class ReadyContext extends SessionContext {
    requireReady() {}
    messages(agent) { return agent.state.messages.values(); }
    messageCount(agent) { return agent.state.messages.length; }
    install(agent, messages) {
        agent.state.messages=messages;
    }
}
export class CompactionContext extends SessionContext {
    messages() { return SessionContext.sourceMessages(this.manager); }
    messageCount() { return this.manager.entryStore.contextMessageCount(this.manager.getLeafId()); }
    install(agent) { agent.state.messages=[]; }
    async beforeInput(session) {
        // The canonical Python owner must reserve/commit before any original input.
        if(process.env.AGENT_COMMS_NATIVE_CONFIG_DIR)
            throw new Error('Stored native context requires journaled owner compaction before input');
        await session.compact();
        session.storedContext.requireReady();
    }
}
