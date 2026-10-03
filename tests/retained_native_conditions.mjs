/** Private S4 constructions. These views neither install nor submit model input. */
import {createHash} from 'node:crypto';
import {isDeepStrictEqual} from 'node:util';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

export async function constructNativeConditions(session, packagePath, boundedSource) {
    const {TurnContext} = await import(pathToFileURL(join(packagePath, 'dist/core/turn-context.js')));
    const {ContextBudget, BudgetAdmissionError} = await import(pathToFileURL(join(
        packagePath, 'node_modules/@earendil-works/pi-ai/dist/api/agent-comms-context-budget.js')));

    function observe(view) {
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

    async function bounded() {
        if (!boundedSource)
            return {evaluated:false, reason:'Original uncombined narrative-only summary required'};
        if (!boundedSource.evaluated) return boundedSource;

        const manager=session.sessionManager, store=manager.entryStore;
        store.assertCurrent();
        const identity={sessionId:manager.getSessionId(),sessionFile:manager.getSessionFile()};
        if (!isDeepStrictEqual(identity,boundedSource.session))
            throw new Error('Bounded source belongs to another original native session');
        const latest=store.latestMetadata(manager.getLeafId(),'compaction');
        if (latest?.id !== boundedSource.native_entry_id)
            throw new Error('Bounded source is not the selected native compaction');

        const {sessionEntryToContextMessages} = await import(pathToFileURL(join(
            packagePath,'dist/core/session-manager.js')));
        const {SessionContext} = await import(pathToFileURL(join(
            packagePath,'dist/core/session-context.js')));
        const entries=Array.from(manager.buildContextEntries());
        const [summary]=sessionEntryToContextMessages(store.get(latest.id));
        // Change the detached SDK message, never the stored entry or packed text.
        // Native conversion owns the wrapper, timestamp and original tokensBefore.
        summary.summary=boundedSource.summary;
        const kept=Array.from(store.keptMetadata(manager.getLeafId()),meta=>store.get(meta.id));
        const context=await SessionContext.prefixContext(session,
            [summary,...SessionContext.entryMessages(kept.values())]);
        const view=await TurnContext.capture(session,context,undefined,entries);
        store.assertCurrent();
        return {evaluated:true,...observe(view),narrative_source:boundedSource.source,
            native_entry_id:latest.id,
            scope:'SDK bounded preview from the supplied narrative source and native kept cut; not installed/submitted input'};
    }

    return {
        'full-context':observe(await TurnContext.fullSource(session)),
        'recent-only':observe(await TurnContext.recentSource(session)),
        'task-memory':observe(await TurnContext.next(session)),
        bounded:await bounded(),
    };
}
