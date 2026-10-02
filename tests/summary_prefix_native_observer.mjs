/** Private installed journey only: original call frames, hashes/counts, no bodies.
 * Inspector observes the actual route formation and selected request; it does
 * not substitute provider, transport, response, session or product source.
 */
import inspector from 'node:inspector';
import { appendFileSync } from 'node:fs';

const output = process.env.AC_PREFIX_OBSERVATION;
const packageRoot = process.env.AC_PREFIX_PACKAGE;
if (output && packageRoot) {
    const observer = new inspector.Session();
    observer.connect();
    const post = (method, params = {}) => new Promise((resolve, reject) =>
        observer.post(method, params, (error, result) => error ? reject(error) : resolve(result)));
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
    const routePoint = await post('Debugger.setBreakpointByUrl', {
        url: pathToFileURL(api).href, lineNumber: line(apiLines, 'if (model.baseUrl !== DEFAULT_CODEX_BASE_URL)'),
    });
    const streamPoint = await post('Debugger.setBreakpointByUrl', {
        url: pathToFileURL(rpc).href,
        lineNumber: line(rpcLines, 'const selectedStream = (model, context, options) => {') + 2,
    });
    observer.on('Debugger.paused', async ({ params }) => {
        try {
            const frame = params.callFrames[0];
            const stage = params.hitBreakpoints.includes(routePoint.breakpointId) ? 'route' : 'selected-request';
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
            appendFileSync(output, JSON.stringify(result.result.value) + '\n', { mode: 0o600 });
            const value = result.result.value;
            if (stage === 'selected-request' && (!value.canonicalEndpoint || !value.toolChoiceNone ||
                    !value.originalInstructions || !value.originalTools || !value.boundProvider)) {
                // Cancel only this owned private gate BEFORE the original
                // selectedStream marks STARTED or invokes auth/provider work.
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
    });
}
