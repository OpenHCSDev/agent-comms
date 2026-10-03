#!/usr/bin/env python3
"""Observe the original committed provider context and expose its read-only base."""
from pathlib import Path
import re
import sys


def replace_once(path, before, after):
    source=path.read_text()
    if source.count(before)!=1:
        raise ValueError(f'Original native context seam changed: {path}')
    path.write_text(source.replace(before,after,1))


def extend_input_claim(session, rpc, core):
    # Change the original claim value in place. Every digest consumer keeps the
    # original proof; descriptors never enter provider input or saved messages.
    source=session.read_text()
    source,count=re.subn(r'this\._nativeInputClaims\.get\(([^()]+)\)',
                         r'this._nativeInputClaims.get(\1)?.digest',source)
    if count!=6: raise ValueError('Original native input claim consumer closure changed')
    session.write_text(source)
    replace_once(session,'    _claimNativeInput(inputId, request) {',
                 '    _claimNativeInput(inputId, request, contributions=[]) {')
    replace_once(session,'        this._nativeInputClaims.set(inputId, digest);',
                 '        this._nativeInputClaims.set(inputId, NativeInputClaim.capture(digest,request,contributions));')
    replace_once(session,'                expandPromptTemplates, source: options?.source ?? "interactive",\n            });',
                 '                expandPromptTemplates, source: options?.source ?? "interactive",\n            },options?.contextContributions);')
    for method,kind in (('steer','steer'),('followUp','follow_up')):
        replace_once(session,f'    async {method}(text, images, inputId) {{',
                     f'    async {method}(text, images, inputId, contextContributions) {{')
        before=f'this._claimNativeInput(inputId, {{ kind: "{kind}", text, images: images ?? null }})'
        replace_once(session,before,before[:-1]+',contextContributions)')
        replace_once(rpc,f'await session.{method}(command.message, command.images, command.inputId);',
                     f'await session.{method}(command.message, command.images, command.inputId, command.contextContributions);')
    replace_once(rpc,'                    inputId: command.inputId,',
                 '                    inputId: command.inputId,\n                    contextContributions: command.contextContributions,')
    replace_once(session,'        for (const { inputId, sessionEntryId } of tracked) {\n',
        '''        const contributors=[];
        for (const { inputId, sessionEntryId } of tracked) {
            const claim=this._nativeInputClaims.get(inputId);
            if (claim) contributors.push({input_id:inputId,contributors:claim.observe(
                llmContext.messages.find(message=>message.inputId===inputId),
                {kind:'native',identity:{sessionId:this.sessionId,sessionFile:this.sessionFile},
                    request_generation:generation,context_digest:digest},
                {kind:'journal',path:this.sessionFile,entries:[sessionEntryId]})});
''')
    replace_once(session,'return {request_generation:generation, context_digest:digest};',
                 'return {request_generation:generation, context_digest:digest, contributors};')
    types=core/'agent-session.d.ts'
    replace_once(types,'export interface PromptOptions {',
                 'export interface PromptOptions {\n    contextContributions?: readonly import("./turn-context.js").InputContributionCoordinates[];')
    for method in ('steer','followUp'):
        replace_once(types,f'{method}(text: string, images?: ImageContent[], inputId?: string): Promise<void>;',
            f'{method}(text: string, images?: ImageContent[], inputId?: string, contextContributions?: readonly import("./turn-context.js").InputContributionCoordinates[]): Promise<void>;')
    types=core.parent/'modes/rpc/rpc-types.d.ts'
    source=types.read_text()
    before='    inputId?: string;'
    if source.count(before)!=3: raise ValueError('Original prompt/steer/follow-up declaration closure changed')
    types.write_text(source.replace(before,before+'\n    contextContributions?: readonly import("../../core/turn-context.js").InputContributionCoordinates[];'))


def main(package):
    package=Path(package)
    core=package/'dist/core'
    (core/'turn-context.js').write_bytes(Path(__file__).with_name('native-turn-context.mjs').read_bytes())
    (core/'turn-context.d.ts').write_bytes(Path(__file__).with_name('native-turn-context.d.ts').read_bytes())
    session=core/'agent-session.js'
    replace_once(session, '        this.agent.onContextReady = async (context) => await this._commitNativeContext(context);',
        '''        this.agent.onContextReady = async (context, requestId) => {
            const source = await this._commitNativeContext(context);
            this._emit({type: "turn_context_observed", context:(await TurnContext.capture(this,context,source)).manifest(requestId)});
        };''')
    source=session.read_text()
    session.write_text('import { TurnContext, NativeInputClaim } from "./turn-context.js";\n'+source)
    replace_once(session, '''                llmContextDigest: digest });
        }
    }
    get modelRuntime()''','''                llmContextDigest: digest });
        }
        return {request_generation:generation, context_digest:digest};
    }
    get modelRuntime()''')
    rpc=package/'dist/modes/rpc/rpc-mode.js'
    source=rpc.read_text()
    rpc.write_text('import { TurnContext } from "../../core/turn-context.js";\n'+source)
    replace_once(rpc, '            case "get_state": {', '''            case "agent_comms_inspect_context": {
                const context=(await TurnContext.next(session)).full();
                return outputArray(id, command.type, "segments", context.segments, {identity:context.identity,counter:context.counter});
            }
            case "get_state": {''')
    # Inspection neither consumes a mutation generation nor disturbs summary custody.
    source=rpc.read_text()
    before='"get_state"]'
    if source.count(before)!=2: raise ValueError('Original read-only RPC admission sets changed')
    rpc.write_text(source.replace(before,'"get_state", "agent_comms_inspect_context"]'))
    replace_once(core/'agent-session.d.ts', '    type: "context_committed";',
        '''    type: "turn_context_observed";
    context: import("./turn-context.js").NativeContextManifest;
} | {
    type: "context_committed";''')
    types=package/'dist/modes/rpc/rpc-types.d.ts'
    replace_once(types,'export type RpcCommand = {', '''export type RpcCommand = {
    id?: string;
    type: "agent_comms_inspect_context";
} | {''')
    replace_once(types,'export type RpcResponse = {', '''export type RpcResponse = {
    id?: string;
    type: "response";
    command: "agent_comms_inspect_context";
    success: true;
    data: import("../../core/turn-context.js").NativeContextData;
} | {''')
    extend_input_claim(session,rpc,core)


if __name__=='__main__': main(sys.argv[1])
