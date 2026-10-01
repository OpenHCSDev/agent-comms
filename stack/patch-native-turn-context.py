#!/usr/bin/env python3
"""Observe the original committed provider context and expose its read-only base."""
from pathlib import Path
import sys


def replace_once(path, before, after):
    source=path.read_text()
    if source.count(before)!=1:
        raise ValueError(f'Original native context seam changed: {path}')
    path.write_text(source.replace(before,after,1))


def main(package):
    package=Path(package)
    core=package/'dist/core'
    (core/'turn-context.js').write_bytes(Path(__file__).with_name('native-turn-context.mjs').read_bytes())
    (core/'turn-context.d.ts').write_bytes(Path(__file__).with_name('native-turn-context.d.ts').read_bytes())
    session=core/'agent-session.js'
    replace_once(session, '        this.agent.onContextReady = async (context) => await this._commitNativeContext(context);',
        '''        this.agent.onContextReady = async (context) => {
            const source = await this._commitNativeContext(context);
            this._emit({type: "turn_context_observed", context:(await TurnContext.capture(this,context,source)).manifest()});
        };''')
    source=session.read_text()
    session.write_text('import { TurnContext } from "./turn-context.js";\n'+source)
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
    before='"agent_comms_compaction_settings", "get_state"'
    if source.count(before)!=2: raise ValueError('Original read-only RPC admission sets changed')
    rpc.write_text(source.replace(before,'"agent_comms_compaction_settings", "get_state", "agent_comms_inspect_context"'))
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


if __name__=='__main__': main(sys.argv[1])
