/** Private S4 constructions. These views neither install nor submit model input. */
import {createHash} from 'node:crypto';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

export async function constructNativeConditions(session, packagePath) {
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

    return {
        'full-context':observe(await TurnContext.fullSource(session)),
        'recent-only':observe(await TurnContext.recentSource(session)),
        'task-memory':observe(await TurnContext.next(session)),
        // Canonical summaries are packed. Without the original uncombined
        // narrative, stripping their stored facts would invent a control.
        bounded:{evaluated:false, reason:'Original uncombined narrative-only summary required'},
    };
}
