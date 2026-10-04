/** Private S4 SDK constructions. Installation alone never grants/submits input. */
import {createHash} from 'node:crypto';
import {isDeepStrictEqual} from 'node:util';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

export async function previewNativeCondition(session, packagePath, view) {
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
        context, identity:view.identity, manifest:view.manifest(),
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

export function armNativeCondition(agent,operation,output,inputId,stage) {
    // One private transform resource owns subscription, failure and restoration.
    // Operations retain their distinct source facts and original record spelling.
    const previous=agent.transformContext;
    const fs=process.getBuiltinModule('node:fs');
    const record=(event,value={})=>fs.appendFileSync(output,
        JSON.stringify({input_id:inputId,stage:stage+'-'+event,...value})+'\n',{mode:0o600});
    const restore=()=>{
        unsubscribe();
        if (agent.transformContext===transform) {
            agent.transformContext=previous;
            record('restored');
        }
    };
    const transform=async(messages,signal)=>{
        try {
            const result=await operation(messages,signal,previous);
            record('applied',{...result.observation,message_count:result.messages.length,
                agent_messages_sha256:process.getBuiltinModule('node:crypto').createHash('sha256')
                    .update(JSON.stringify(result.messages)).digest('hex')});
            return result.messages;
        } catch(error) {
            restore();
            record('refused',{reason:error.message});
            throw error;
        }
    };
    const unsubscribe=agent.subscribe(event=>{
        if (event.type==='agent_end') restore();
    });
    agent.transformContext=transform;
    record('armed');
    return restore;
}

export function armBoundedNativeCondition(session,packagePath,source,transform,output,inputId) {
    // Original bounded replacement: run the configured transform first and
    // replace only its corroborated summary. Original record format is retained.
    return armNativeCondition(session.agent,async(messages,signal,previous)=>{
        const transformed=previous ? await previous.call(session.agent,messages,signal) : messages;
        return {messages:await transform(session,packagePath,source,transformed),
            observation:{session:source.session,checkpoint_session:source.checkpoint_session,
                native_entry_id:source.native_entry_id,narrative_source:source.source}};
    },output,inputId,'bounded-transform');
}

export function armInstalledNativeCondition(session,construction,output,inputId) {
    session.storedContext.requireReady();
    if (!isDeepStrictEqual(Array.from(session.storedContext.messages(session.agent)),
            construction.agent_messages))
        throw new Error('Selected SDK construction is not installed');
    if (!isDeepStrictEqual(construction.source_witness,
            session.sessionManager.captureCompactionWitness(construction.source_witness.firstKeptEntryId)))
        throw new Error('Selected SDK installation source changed before observation');
    const count=construction.agent_messages.length;
    return armNativeCondition(session.agent,async(messages,signal,previous)=>{
        const prefix=messages.slice(0,count);
        if (!isDeepStrictEqual(prefix,construction.agent_messages))
            throw new Error('Installed SDK prefix changed before the original transform');
        const inputs=messages.slice(count).filter(message=>message.role==='user' && message.inputId===inputId);
        if (inputs.length!==1)
            throw new Error('Installed SDK observation does not contain one original new input');
        const sourcePrefix=process.getBuiltinModule('node:crypto').createHash('sha256')
            .update(JSON.stringify(prefix)).digest('hex');
        // Observe actual entry to the configured transform, then its real result.
        // No message is filtered, reconstructed or replaced by this observer.
        const transformed=previous ? await previous.call(session.agent,messages,signal) : messages;
        return {messages:transformed,observation:{condition:construction.condition,
            source_witness:construction.source_witness,
            entry_selection:construction.entry_selection,
            narrative_source:construction.narrative_source,
            construction_manifest:construction.manifest,
            construction_context_sha256:construction.context_sha256,
            source_prefix_count:count,source_prefix_sha256:sourcePrefix,
            source_message_count:messages.length}};
    },output,inputId,'installed-transform');
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

export async function constructNativeConditions(session, packagePath, boundedSource, selections) {
    const {TurnContext}=await import(pathToFileURL(join(packagePath,'dist/core/turn-context.js')));
    const {SessionContext}=await import(pathToFileURL(join(packagePath,'dist/core/session-context.js')));
    const manager=session.sessionManager,store=manager.entryStore;
    const witness=manager.captureCompactionWitness(manager.getLeafId());
    // These SDK metadata owners supply the actual selection. SDK entryMessages
    // supplies raw AgentMessages once; the same acquisition feeds preview and
    // installation. No provider-message-to-AgentMessage reconstruction.
    async function construction(messages,entries,details={}) {
        const context=await SessionContext.prefixContext(session,messages);
        const view=await TurnContext.capture(session,context,undefined,entries);
        return {evaluated:true,...await previewNativeCondition(session,packagePath,view),...details,
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
    async function entryConstruction(entries) {
        // This selection and raw SDK conversion share the acquired entry set.
        // A manifest's broad attribution or mutable ReadyContext messages alone
        // cannot reconstruct this observation later. Retain references, not a
        // second message payload or selection algorithm.
        return construction(Array.from(SessionContext.entryMessages(entries.values())),entries,{
            entry_selection:{kind:'journal',path:witness.sessionFile,
                entries:entries.map(entry=>entry.id)}});
    }
    const constructors={
        'full-context':async()=>{
            const entries=Array.from(store.uncompactedMetadata(manager.getLeafId()),meta=>store.get(meta.id));
            return entryConstruction(entries);
        },
        'recent-only':async()=>{
            const entries=Array.from(store.keptMetadata(manager.getLeafId()),meta=>store.get(meta.id));
            return entryConstruction(entries);
        },
        'task-memory':()=>construction(Array.from(session.storedContext.messages(session.agent)),
            Array.from(manager.buildContextEntries())),
        bounded,
    };
    const conditions={};
    for (const condition of selections ?? Object.keys(constructors)) {
        if (!Object.hasOwn(constructors,condition))
            throw new Error('Unknown SDK construction selection');
        const constructed=await constructors[condition]();
        if (constructed.evaluated)
            Object.defineProperty(constructed,'condition',{value:condition,enumerable:true});
        conditions[condition]=constructed;
    }
    if (!isDeepStrictEqual(witness,manager.captureCompactionWitness(witness.firstKeptEntryId)))
        throw new Error('Native selection changed during condition construction');
    return conditions;
}

export function armConfiguredNativeCondition(session,packagePath,condition,source,output,inputId) {
    // This is private fixture instrumentation of the existing prompt resource.
    // The original prompt alone still claims/admits/enqueues the input and owns
    // auth, extensions, callbacks, persistence and terminal disposition.
    const scope=new DisposableStack();
    const originalPrompt=session.prompt;
    const promptDescriptor=Object.getOwnPropertyDescriptor(session,'prompt');
    const record=stage=>process.getBuiltinModule('node:fs').appendFileSync(output,
        JSON.stringify({stage,input_id:inputId,condition})+'\n',{mode:0o600});
    const restorePrompt=()=>{
        if (session.prompt===prompt) {
            if (promptDescriptor) Object.defineProperty(session,'prompt',promptDescriptor);
            else delete session.prompt;
        }
    };
    async function prompt(text,options) {
        restorePrompt();
        try {
            if (options.inputId!==inputId)
                throw new Error('Configured SDK construction belongs to another original input');
            if (this.isStreaming || this.isCompacting)
                throw new Error('Configured SDK construction requires an idle native prompt');
            const context=this.storedContext;
            const originalBeforeInput=context.beforeInput;
            const beforeDescriptor=Object.getOwnPropertyDescriptor(context,'beforeInput');
            const restoreBeforeInput=()=>{
                if (context.beforeInput===beforeInput) {
                    if (beforeDescriptor) Object.defineProperty(context,'beforeInput',beforeDescriptor);
                    else delete context.beforeInput;
                }
            };
            async function beforeInput(selected) {
                restoreBeforeInput();
                await originalBeforeInput.call(this,selected);
                // Readiness/selection and current budget remain with the
                // original SDK owners. Build only this declared selection.
                const constructions=await constructNativeConditions(selected,packagePath,source,[condition]);
                const construction=await applyNativeCondition(selected,packagePath,constructions[condition]);
                scope.defer(armInstalledNativeCondition(selected,construction,output,inputId));
            }
            context.beforeInput=beforeInput;
            scope.defer(restoreBeforeInput);
            return await originalPrompt.call(this,text,options);
        } catch(error) {
            record('installed-input-refused');
            throw error;
        } finally {scope.dispose();}
    }
    scope.defer(()=>record('installed-input-retired'));
    scope.defer(restorePrompt);
    session.prompt=prompt;
    record('installed-input-armed');
    return ()=>scope.dispose();
}
