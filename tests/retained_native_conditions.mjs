/** Private S4 SDK constructions. Installation alone never grants/submits input. */
import {createHash} from 'node:crypto';
import {isDeepStrictEqual} from 'node:util';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

async function observe(session, packagePath, view) {
    const {ContextBudget, BudgetAdmissionError} = await import(pathToFileURL(join(
        packagePath, 'node_modules/@earendil-works/pi-ai/dist/api/agent-comms-context-budget.js')));
    const context = view.render();
    let admission = {evaluated:false, reason:'SDK session has no selected model'};
    if (session.model) {
        const budget = new ContextBudget(session.model, context);
        let outcome;
        try {
            budget.allowance(undefined);
            outcome = {admitted:true};
        } catch (error) {
            if (!(error instanceof BudgetAdmissionError)) throw error;
            outcome = {admitted:false, reason:error.message};
        }
        admission = {evaluated:true, ...outcome, model:budget.model,
            estimated_input_tokens:budget.input,
            ...(Number.isFinite(budget.available) ? {available_tokens:budget.available} : {})};
    }
    return {
        context, manifest:view.full(),
        context_sha256:createHash('sha256').update(JSON.stringify(context)).digest('hex'),
        budget:admission,
        scope:'SDK context preview; not submitted input, serialized HTTP input, or provider usage',
    };
}

export async function boundedMessages(session, packagePath, source) {
    if (!source?.evaluated)
        throw new Error('Original eligible narrative source required for bounded application');
    const manager=session.sessionManager, store=manager.entryStore;
    store.assertCurrent();
    const identity={sessionId:manager.getSessionId(),sessionFile:manager.getSessionFile()};
    if (!isDeepStrictEqual(identity,source.session))
        throw new Error('Bounded source belongs to another original native session');
    const latest=store.latestMetadata(manager.getLeafId(),'compaction');
    if (latest?.id !== source.native_entry_id)
        throw new Error('Bounded source is not the selected native compaction');
    const witness=manager.captureCompactionWitness(latest.firstKeptEntryId);
    const {sessionEntryToContextMessages} = await import(pathToFileURL(join(
        packagePath,'dist/core/session-manager.js')));
    const {SessionContext} = await import(pathToFileURL(join(
        packagePath,'dist/core/session-context.js')));
    const [originalSummary]=sessionEntryToContextMessages(store.get(latest.id));
    // This detached SDK message retains the original wrapper, timestamp and
    // tokensBefore. History/packed text is never changed or stripped.
    const summary={...originalSummary,summary:source.summary};
    const kept=Array.from(store.keptMetadata(manager.getLeafId()),meta=>store.get(meta.id));
    const messages=[summary,...SessionContext.entryMessages(kept.values())];
    store.assertCurrent();
    if (!isDeepStrictEqual(witness,manager.captureCompactionWitness(latest.firstKeptEntryId)))
        throw new Error('Native selection changed during bounded construction');
    return {messages,originalSummary,witness};
}

export async function transformBoundedNativeCondition(session,packagePath,source,messages) {
    // Agent's original transform has already run. Preserve its entire raw
    // result and the fresh input; replace exactly the corroborated SDK message.
    session.storedContext.requireReady();
    const {messages:[summary],originalSummary}=await boundedMessages(session,packagePath,source);
    const matches=messages.filter(message=>isDeepStrictEqual(message,originalSummary));
    if (matches.length !== 1)
        throw new Error('Original SDK compaction message is not unique in transformed input');
    return messages.map(message=>message===matches[0] ? summary : message);
}

export function armBoundedNativeCondition(session,packagePath,source,transform,output,inputId) {
    // This function also crosses the private inspector's SDK boundary as source.
    // Keep its dependencies explicit; no product globals, command or overlay.
    const agent=session.agent, previous=agent.transformContext;
    const fs=process.getBuiltinModule('node:fs');
    const record=value=>fs.appendFileSync(output,JSON.stringify({input_id:inputId,...value})+'\n',{mode:0o600});
    const restore=()=>{
        agent.transformContext=previous;
        unsubscribe();
        record({stage:'bounded-transform-restored'});
    };
    const unsubscribe=agent.subscribe(event=>{
        if (event.type==='agent_end') restore();
    });
    agent.transformContext=async(messages,signal)=>{
        try {
            const transformed=previous ? await previous.call(agent,messages,signal) : messages;
            const result=await transform(session,packagePath,source,transformed);
            record({stage:'bounded-transform-applied',session:source.session,
                checkpoint_session:source.checkpoint_session,native_entry_id:source.native_entry_id,
                narrative_source:source.source,message_count:result.length,
                agent_messages_sha256:process.getBuiltinModule('node:crypto').createHash('sha256')
                    .update(JSON.stringify(result)).digest('hex')});
            return result;
        } catch(error) {
            restore();
            record({stage:'bounded-transform-refused',reason:error.message});
            throw error;
        }
    };
    record({stage:'bounded-transform-armed'});
    return restore;
}

export async function applyNativeCondition(session, packagePath, construction) {
    // This original SessionContext grants installation. A measured preview
    // cannot manufacture readiness or stand in for final input admission.
    session.storedContext.requireReady();
    if (!construction.evaluated)
        throw new Error('Original evaluated SDK construction required');
    const manager=session.sessionManager;
    if (!isDeepStrictEqual(construction.source_witness,
            manager.captureCompactionWitness(construction.source_witness.firstKeptEntryId)))
        throw new Error('Native selection changed since condition construction');
    const {SessionContext}=await import(pathToFileURL(join(packagePath,'dist/core/session-context.js')));
    const {ContextBudget}=await import(pathToFileURL(join(packagePath,
        'node_modules/@earendil-works/pi-ai/dist/api/agent-comms-context-budget.js')));
    const context=await SessionContext.prefixContext(session,construction.agent_messages);
    if (createHash('sha256').update(JSON.stringify(context)).digest('hex')!==construction.context_sha256)
        throw new Error('SDK construction changed before installation');
    // Settings/model can change while a preview is borrowed. Re-admit with the
    // original budget owner now; never reuse a captured allowance or truncate.
    new ContextBudget(session.model,context).allowance(undefined);
    if (!isDeepStrictEqual(construction.source_witness,
            manager.captureCompactionWitness(construction.source_witness.firstKeptEntryId)))
        throw new Error('Native selection changed during condition installation');
    session.storedContext.requireReady();
    session.storedContext.install(session.agent,construction.agent_messages);
    return {...construction,scope:'Original AgentMessages installed through selected SessionContext; not input admission/submission, final request capacity or provider proof'};
}

export async function constructNativeConditions(session, packagePath, boundedSource) {
    const {TurnContext}=await import(pathToFileURL(join(packagePath,'dist/core/turn-context.js')));
    const {SessionContext}=await import(pathToFileURL(join(packagePath,'dist/core/session-context.js')));
    const manager=session.sessionManager,store=manager.entryStore;
    const witness=manager.captureCompactionWitness(manager.getLeafId());
    // These SDK metadata owners supply the actual selection. SDK entryMessages
    // supplies raw AgentMessages once; the same acquisition feeds preview and
    // installation. No provider-message-to-AgentMessage reconstruction.
    const full=Array.from(store.uncompactedMetadata(manager.getLeafId()),meta=>store.get(meta.id));
    const recent=Array.from(store.keptMetadata(manager.getLeafId()),meta=>store.get(meta.id));
    const task=Array.from(session.storedContext.messages(session.agent));
    async function construction(messages,entries,details={}) {
        const context=await SessionContext.prefixContext(session,messages);
        const view=await TurnContext.capture(session,context,undefined,entries);
        return {evaluated:true,...await observe(session,packagePath,view),...details,
            agent_messages:messages,source_witness:witness,
            scope:'Original acquired SDK construction; not installed/submitted input or final request capacity'};
    }
    async function bounded() {
        if (!boundedSource)
            return {evaluated:false,reason:'Original uncombined narrative-only summary required'};
        if (!boundedSource.evaluated) return boundedSource;
        const {messages}=await boundedMessages(session,packagePath,boundedSource);
        return construction(messages,Array.from(manager.buildContextEntries()),{
            narrative_source:boundedSource.source,
            checkpoint_session:boundedSource.checkpoint_session,
            native_entry_id:boundedSource.native_entry_id});
    }
    const conditions={
        'full-context':await construction(Array.from(SessionContext.entryMessages(full.values())),full),
        'recent-only':await construction(Array.from(SessionContext.entryMessages(recent.values())),recent),
        'task-memory':await construction(task,Array.from(manager.buildContextEntries())),
        bounded:await bounded(),
    };
    if (!isDeepStrictEqual(witness,manager.captureCompactionWitness(witness.firstKeptEntryId)))
        throw new Error('Native selection changed during condition construction');
    return conditions;
}
