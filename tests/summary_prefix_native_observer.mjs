/** Private installed journey only: original call frames, hashes/counts, no bodies.
 * Inspector observes the actual route formation and selected request; it does
 * not substitute provider, transport, response, session or product source.
 */
import { appendFileSync } from 'node:fs';

const [, , port, packageRoot, output, contexts, summaries] = process.argv;
if (output && packageRoot) {
    // External inspection preserves the native import fence. Read only original
    // frames from this owned private child's loopback debugger; no code overlay.
    let endpoint;
    const deadline = Date.now() + 30000;
    while (!endpoint && Date.now() < deadline) {
        try {
            const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
            endpoint = targets[0].webSocketDebuggerUrl;
        } catch {
            await new Promise(resolve => setTimeout(resolve, 50));
        }
    }
    if (!endpoint) throw new Error('Owned debugger did not open');
    const observer = new WebSocket(endpoint);
    await new Promise((resolve, reject) => {
        observer.addEventListener('open', resolve, { once: true });
        observer.addEventListener('error', reject, { once: true });
    });
    let sequence = 0;
    const pending = new Map();
    const post = (method, params = {}) => new Promise((resolve, reject) => {
        const id = ++sequence;
        pending.set(id, { resolve, reject });
        observer.send(JSON.stringify({ id, method, params }));
    });
    observer.addEventListener('message', ({ data }) => {
        const message = JSON.parse(data);
        if (message.id) {
            const request = pending.get(message.id);
            pending.delete(message.id);
            if (message.error) request.reject(new Error(message.error.message));
            else request.resolve(message.result);
        } else if (message.method === 'Debugger.paused') {
            void paused(message.params);
        }
    });
    await post('Debugger.enable');
    const rpc = `${packageRoot}/dist/modes/rpc/rpc-mode.js`;
    const api = `${packageRoot}/node_modules/@earendil-works/pi-ai/dist/api/openai-codex-responses.js`;
    const source = await import('node:fs');
    const rpcLines = source.readFileSync(rpc, 'utf8').split('\n');
    const apiLines = source.readFileSync(api, 'utf8').split('\n');
    const line = (lines, text) => {
        const indices = lines.flatMap((value, index) => value.includes(text) ? [index] : []);
        if (indices.length !== 1) throw new Error('Original observation anchor changed');
        return indices[0];
    };
    const { pathToFileURL } = await import('node:url');
    const routePoint = (!summaries || contexts) && await post('Debugger.setBreakpointByUrl', {
        url: pathToFileURL(api).href, lineNumber: line(apiLines, 'if (model.baseUrl !== DEFAULT_CODEX_BASE_URL)'),
    });
    const streamPoint = (!summaries || contexts) && await post('Debugger.setBreakpointByUrl', {
        url: pathToFileURL(rpc).href,
        lineNumber: line(rpcLines, 'const selectedStream = (model, context, options) => {') + 2,
    });
    const summaryRequestPoint = summaries && await post('Debugger.setBreakpointByUrl', {
        url:pathToFileURL(rpc).href,
        lineNumber:line(rpcLines, 'const result = await compact(preparation, binding.model'),
    });
    const assemblyPoint = summaries && await post('Debugger.setBreakpointByUrl', {
        url:pathToFileURL(`${packageRoot}/dist/core/compaction/compaction.js`).href,
        lineNumber:line(source.readFileSync(`${packageRoot}/dist/core/compaction/compaction.js`, 'utf8').split('\n'),
            'summary = contextPolicy.packSummary(retainedText, summary, annotations,'),
    });
    // Inspector correlation within this one private selected operation. It does
    // not select a native source or grant a request, commit or retry.
    let originalSummaryRequest;
    const contextPoint = contexts && await post('Debugger.setBreakpointByUrl', {
        url: pathToFileURL(`${packageRoot}/dist/core/turn-context.js`).href,
        lineNumber: line(source.readFileSync(`${packageRoot}/dist/core/turn-context.js`, 'utf8').split('\n'),
            "    manifest(requestId) {"),
    });
    appendFileSync(output, JSON.stringify({ stage: 'observer-ready' }) + '\n', { mode: 0o600 });
    async function paused(params) {
        try {
            if (!params.hitBreakpoints.length) return;
            const frame = params.callFrames[0];
            if (summaryRequestPoint && params.hitBreakpoints.includes(summaryRequestPoint.breakpointId)) {
                const observed = await post('Debugger.evaluateOnCallFrame', {
                    callFrameId:frame.callFrameId,
                    expression:'({request, tokensBefore:preparation.tokensBefore})', returnByValue:true,
                });
                if (observed.exceptionDetails) throw new Error('Original selected summary request unavailable');
                originalSummaryRequest = observed.result.value;
                return;
            }
            if (assemblyPoint && params.hitBreakpoints.includes(assemblyPoint.breakpointId)) {
                const observed = await post('Debugger.evaluateOnCallFrame', {
                    callFrameId:frame.callFrameId,
                    expression:`({summary, firstKeptEntryId, tokensBefore,
                        generated_parts:[historyResult?.text,prefixResult?.text].filter(value=>value!==undefined),
                        inherited_summary:historyResult ? null : previousSummary ?? null})`,
                    returnByValue:true,
                });
                if (observed.exceptionDetails) throw new Error('Original pre-pack summary assembly unavailable');
                const assembly = observed.result.value;
                if (!originalSummaryRequest ||
                    assembly.firstKeptEntryId !== originalSummaryRequest.request.witness.firstKeptEntryId ||
                    assembly.tokensBefore !== originalSummaryRequest.tokensBefore)
                    throw new Error('Original summary assembly does not match its inspected selected request');
                const {request} = originalSummaryRequest;
                const path = `${summaries}/summary-${request.operationId}.json`;
                source.writeFileSync(path, JSON.stringify({request, summary:assembly.summary,
                    generated_parts:assembly.generated_parts, inherited_summary:assembly.inherited_summary}),
                    {mode:0o600,flag:'wx'});
                appendFileSync(output, JSON.stringify({stage:'summary-assembly', path,
                    operation_id:request.operationId})+'\n', {mode:0o600});
                originalSummaryRequest = undefined;
                return;
            }
            if (contextPoint && params.hitBreakpoints.includes(contextPoint.breakpointId)) {
                // Read the original TurnContext object at its manifest publication.
                // Do not ask for a later preview or reconstruct provider context.
                const original = await post('Debugger.evaluateOnCallFrame', {
                    callFrameId: frame.callFrameId,
                    expression: '({context:this.full(), serialized:this.segments.map(segment=>JSON.stringify(segment.value))})',
                    returnByValue: true,
                });
                if (original.exceptionDetails) throw new Error('Original SDK capture unavailable');
                const { context:data, serialized } = original.result.value;
                const [provenance] = data.segments[0].provenance.filter(value => value.kind === 'native');
                if (!provenance) throw new Error('SDK capture is a preview, not a committed request');
                const path = `${contexts}/context-${provenance.context_digest}.json`;
                source.writeFileSync(path, JSON.stringify(data), { mode: 0o600, flag: 'wx' });
                source.writeFileSync(`${contexts}/segments-${provenance.context_digest}.json`,
                    JSON.stringify(serialized), { mode:0o600, flag:'wx' });
                appendFileSync(output, JSON.stringify({stage:'source-context', path,
                    request_generation:provenance.request_generation,
                    context_digest:provenance.context_digest}) + '\n', {mode:0o600});
                return;
            }
            const stage = params.hitBreakpoints.includes(routePoint?.breakpointId) ? 'route' : 'selected-request';
            const expression = `(() => {
                const hash = value => process.getBuiltinModule('node:crypto').createHash('sha256')
                    .update(JSON.stringify(value ?? null)).digest('hex');
                const prefix = ${stage === 'route' ? 'context.messages' : 'context.messages.slice(0, -1)'};
                return { stage: ${JSON.stringify(stage)}, provider: model.provider, api: model.api,
                    canonicalEndpoint: model.baseUrl === 'https://chatgpt.com/backend-api',
                    systemHash: hash(context.systemPrompt), toolsHash: hash(context.tools),
                    orderedPrefixHash: hash(prefix), messageCount: prefix.length,
                    toolCount: context.tools?.length ?? 0,
                    ${stage === 'route' ? 'prefixCapabilityEntered: true' : `
                    toolChoiceNone: options.toolChoice === 'none',
                    originalInstructions: context.systemPrompt === session.systemPrompt,
                    originalTools: context.tools === session.agent.state.tools,
                    boundProvider: session.modelRuntime.getProvider(model.provider) === binding.provider`}
                };
            })()`;
            const result = await post('Debugger.evaluateOnCallFrame', {
                callFrameId: frame.callFrameId, expression, returnByValue: true,
            });
                if (result.exceptionDetails) throw new Error('Original frame observation unavailable');
                const value = result.result.value;
                appendFileSync(output, JSON.stringify(value) + '\n', { mode: 0o600 });
                if (stage === 'selected-request' && (!value.canonicalEndpoint || !value.toolChoiceNone ||
                        !value.originalInstructions || !value.originalTools || !value.boundProvider)) {
                    // Cancel only this owned private gate BEFORE STARTED/auth.
                    await post('Debugger.evaluateOnCallFrame', {
                        callFrameId: frame.callFrameId, expression: 'slot.controller.abort()',
                    });
                    appendFileSync(output, JSON.stringify({ unqualifiedRouteCancelledBeforeRequest: true }) + '\n');
                }
        } catch (error) {
            appendFileSync(output, JSON.stringify({ observerFailed: error.message }) + '\n', { mode: 0o600 });
        } finally {
            await post('Debugger.resume');
        }
    }
    await post('Runtime.runIfWaitingForDebugger');
    await new Promise(resolve => observer.addEventListener('close', resolve, { once: true }));
}
