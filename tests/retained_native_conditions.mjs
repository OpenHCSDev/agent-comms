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

async function boundedConstruction(session, packagePath, source) {
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
    const {TurnContext} = await import(pathToFileURL(join(packagePath,'dist/core/turn-context.js')));
    const entries=Array.from(manager.buildContextEntries());
    const [summary]=sessionEntryToContextMessages(store.get(latest.id));
    // This detached SDK message retains the original wrapper, timestamp and
    // tokensBefore. History/packed text is never changed or stripped.
    summary.summary=source.summary;
    const kept=Array.from(store.keptMetadata(manager.getLeafId()),meta=>store.get(meta.id));
    const messages=[summary,...SessionContext.entryMessages(kept.values())];
    const context=await SessionContext.prefixContext(session,messages);
    const view=await TurnContext.capture(session,context,undefined,entries);
    const observed=await observe(session,packagePath,view);
    store.assertCurrent();
    if (!isDeepStrictEqual(witness,manager.captureCompactionWitness(latest.firstKeptEntryId)))
        throw new Error('Native selection changed during bounded construction');
    return {messages, observed:{evaluated:true,...observed,narrative_source:source.source,
        checkpoint_session:source.checkpoint_session,native_entry_id:latest.id,
        scope:'SDK bounded preview from the supplied narrative source and native kept cut; not installed/submitted input'}};
}

export async function applyBoundedNativeCondition(session, packagePath, source) {
    // The selected native context owns readiness. CompactionContext refuses;
    // no replacement context, cached permission or direct agent-state write.
    session.storedContext.requireReady();
    const {messages,observed}=await boundedConstruction(session,packagePath,source);
    session.storedContext.requireReady();
    session.storedContext.install(session.agent,messages);
    return {...observed,scope:'Bounded raw SDK context installed through the selected SessionContext; not input admission/submission or provider proof'};
}

export async function constructNativeConditions(session, packagePath, boundedSource) {
    const {TurnContext} = await import(pathToFileURL(join(packagePath,'dist/core/turn-context.js')));
    async function bounded() {
        if (!boundedSource)
            return {evaluated:false, reason:'Original uncombined narrative-only summary required'};
        if (!boundedSource.evaluated) return boundedSource;
        return (await boundedConstruction(session,packagePath,boundedSource)).observed;
    }
    return {
        'full-context':await observe(session,packagePath,await TurnContext.fullSource(session)),
        'recent-only':await observe(session,packagePath,await TurnContext.recentSource(session)),
        'task-memory':await observe(session,packagePath,await TurnContext.next(session)),
        bounded:await bounded(),
    };
}
