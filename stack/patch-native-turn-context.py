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


def extend_system_sources(core):
    """Capture original loader reads and the builder's writes, never search text."""
    loader = core/'resource-loader.js'
    loader.write_text('import { SystemLayerSegment } from "./turn-context.js";\n'+loader.read_text())
    replace_once(loader, '            return stripBom(readFileSync(input, "utf-8"));',
        '''            const raw=readFileSync(input);
            return SystemLayerSegment.fromFile(resolvePath(input),raw,stripBom(raw.toString('utf8')));''')
    # Literal arguments and read failures have no file attribution.
    source=loader.read_text()
    if source.count('return input;')!=2:
        raise ValueError('Original prompt literal/read-failure boundary changed')
    loader.write_text(source.replace('return input;', 'return SystemLayerSegment.unattributed(input);'))
    replace_once(loader, '''                return {
                    path: filePath,
                    content: stripBom(readFileSync(filePath, "utf-8")),
                };''', '''                const raw=readFileSync(filePath);
                const systemLayer=SystemLayerSegment.fromFile(filePath,raw,stripBom(raw.toString('utf8')));
                return {path:filePath,get content(){return systemLayer.value;},systemLayer};''')
    for obsolete in ('    systemPromptSourcePath;\n', '    appendSystemPromptSourcePaths;\n',
                     '        this.appendSystemPromptSourcePaths = [];\n'):
        replace_once(loader,obsolete,'')
    replace_once(loader, '        return this.systemPrompt;', '        return this.systemPrompt?.value;')
    replace_once(loader, '        return this.systemPromptSourcePath ? { path: this.systemPromptSourcePath } : undefined;',
                 '        return this.systemPrompt?.sourceFile();')
    replace_once(loader, '        return this.appendSystemPrompt;',
                 '        return this.appendSystemPrompt.map(layer=>layer.value);')
    replace_once(loader, '        return this.appendSystemPromptSourcePaths.map((path) => ({ path }));',
                 '        return this.appendSystemPrompt.flatMap(layer=>layer.sourceFile() ?? []);')
    replace_once(loader, '    getSystemPrompt() {', '''    systemPromptSources() {
        return {custom:this.systemPrompt,append:this.appendSystemPrompt};
    }
    getSystemPrompt() {''')
    replace_once(loader, '''        this.systemPrompt = this.systemPromptOverride ? this.systemPromptOverride(baseSystemPrompt) : baseSystemPrompt;
        this.systemPromptSourcePath =
            systemPromptSource && existsSync(systemPromptSource) ? resolvePath(systemPromptSource) : undefined;''',
        '''        const custom=this.systemPromptOverride
            ? this.systemPromptOverride(baseSystemPrompt?.value) : baseSystemPrompt?.value;
        this.systemPrompt=custom===undefined ? undefined
            : (baseSystemPrompt ?? SystemLayerSegment.unattributed('')).replaced(custom);''')
    replace_once(loader, '''        this.appendSystemPrompt = this.appendSystemPromptOverride
            ? this.appendSystemPromptOverride(baseAppend)
            : baseAppend;
        this.appendSystemPromptSourcePaths = appendSources
            .filter((source) => existsSync(source))
            .map((source) => resolvePath(source));''', '''        // Only the unchanged complete ordered value retains source attribution.
        const originalAppend=baseAppend.map(layer=>layer.value);
        const revisedAppend=this.appendSystemPromptOverride
            ? this.appendSystemPromptOverride(originalAppend) : originalAppend;
        this.appendSystemPrompt=JSON.stringify(revisedAppend)===JSON.stringify(baseAppend.map(layer=>layer.value))
            ? baseAppend : revisedAppend.map(value=>SystemLayerSegment.unattributed(value));''')

    builder=core/'system-prompt.js'
    builder.write_text('import { readFileSync } from "node:fs";\n'
        'import { SystemLayerSegment } from "./turn-context.js";\n'+builder.read_text())
    replace_once(builder, 'import { getDocsPath, getExamplesPath, getReadmePath }',
                 'import { VERSION, getDocsPath, getExamplesPath, getReadmePath }')
    replace_once(builder, 'export function buildSystemPrompt(options) {', '''export function buildSystemPrompt(options, layer) {
    const assembly=layer ?? SystemLayerSegment.fromResource(
        new URL('./system-prompt.js',import.meta.url).pathname,
        readFileSync(new URL('./system-prompt.js',import.meta.url)),`Pi ${VERSION} generated system prompt`);''')
    # Preserve the custom/default newline distinction while sharing the tail.
    source=builder.read_text()
    start=source.index('    if (customPrompt) {')
    end=source.index('    // Get absolute paths',start)
    source=source[:start]+'    if (customPrompt) {\n'+\
        '        assembly.extend((options.systemSources?.custom ?? SystemLayerSegment.unattributed(customPrompt)).replaced(customPrompt));\n'+\
        '    } else {\n'+source[end:]
    source=source.replace('    let prompt = `You are an expert coding assistant',
                          '    assembly.append(`You are an expert coding assistant',1)
    source=source.replace('cross-references before implementing\n- Always read pi .md files completely and follow links to related docs (e.g., tui.md for TUI API details)`;',
                          'cross-references before implementing\n- Always read pi .md files completely and follow links to related docs (e.g., tui.md for TUI API details)`);\n    }',1)
    source=source.replace('        prompt += appendSection;', '''        const append=options.systemSources?.append;
        if (append && append.map(value=>value.value).join('\\n\\n')===appendSystemPrompt) {
            assembly.append('\\n\\n');
            append.forEach((value,index)=>{if(index) assembly.append('\\n\\n'); assembly.extend(value);});
        } else { assembly.extend(SystemLayerSegment.unattributed(appendSection)); }''',1)
    source=source.replace('        prompt += "\\n\\n<project_context>\\n\\n";',
                          '        assembly.append("\\n\\n<project_context>\\n\\n");',1)
    source=source.replace('        prompt += "Project-specific instructions and guidelines:\\n\\n";',
                          '        assembly.append("Project-specific instructions and guidelines:\\n\\n");',1)
    source=source.replace('for (const { path: filePath, content } of contextFiles)',
                          'for (const { path: filePath, content, systemLayer } of contextFiles)',1)
    source=source.replace('            prompt += `<project_instructions path="${filePath}">\\n${content}\\n</project_instructions>\\n\\n`;',
        '''            assembly.append(`<project_instructions path="${filePath}">\\n`);
            assembly.extend((systemLayer ?? SystemLayerSegment.unattributed(content)).replaced(content));
            assembly.append('\\n</project_instructions>\\n\\n');''',1)
    source=source.replace('        prompt += "</project_context>\\n";',
                          '        assembly.append("</project_context>\\n");',1)
    source=source.replace('        prompt += formatSkillsForPrompt(skills, skillFileReadTool);',
                          '        formatSkillsForPrompt(skills,skillFileReadTool,assembly);',1)
    source=source.replace('    prompt += `\\nCurrent working directory: ${promptCwd}`;\n    return prompt;',
                          '    assembly.append(`\\nCurrent working directory: ${promptCwd}${customPrompt ? "\\n" : ""}`);\n    return assembly.value;',1)
    if 'prompt +=' in source or 'let prompt =' in source:
        raise ValueError('Original system builder consumer was not migrated')
    builder.write_text(source)

    skills=core/'skills.js'
    skills.write_text('import { SystemLayerSegment } from "./turn-context.js";\n'+skills.read_text())
    replace_once(skills, 'export function formatSkillsForPrompt(skills, fileReadTool = "read") {',
                 'export function formatSkillsForPrompt(skills, fileReadTool = "read", assembly) {')
    replace_once(skills, '    for (const skill of visibleSkills) {\n        lines.push("  <skill>");',
        '''    if (assembly) assembly.append(lines.join('\\n'));
    for (const skill of visibleSkills) {
        const start=lines.length;
        lines.push("  <skill>");''')
    replace_once(skills, '        lines.push("  </skill>");\n    }\n    lines.push("</available_skills>");',
        '''        lines.push("  </skill>");
        if (assembly) {
            const observed=SystemLayerSegment.fromResource(skill.filePath,
                JSON.stringify(skill),'SDK skill prompt metadata; not the skill file body');
            observed.append('\\n'+lines.slice(start).join('\\n'));
            assembly.extend(observed);
        }
    }
    lines.push("</available_skills>");
    if (assembly) assembly.append('\\n</available_skills>');''')

    session=core/'agent-session.js'
    replace_once(session, '    _baseSystemPrompt = "";',
                 '    _baseSystemPrompt = SystemLayerSegment.unattributed("");')
    source=session.read_text()
    source=source.replace('this._systemPromptOverride ?? this._baseSystemPrompt;',
                          'this._systemPromptOverride ?? this._baseSystemPrompt.value;')
    source=source.replace('this._systemPromptOverride ?? this._baseSystemPrompt,',
                          'this._systemPromptOverride ?? this._baseSystemPrompt.value,')
    source=source.replace('this.agent.state.systemPrompt = this._baseSystemPrompt;',
                          'this.agent.state.systemPrompt = this._baseSystemPrompt.value;')
    source=source.replace('currentImages, this._baseSystemPrompt, this._baseSystemPromptOptions)',
                          'currentImages, this._baseSystemPrompt.value, this._baseSystemPromptOptions)')
    session.write_text(source)
    replace_once(session, '            customPrompt: loaderSystemPrompt,',
                 '            customPrompt: loaderSystemPrompt,\n            systemSources: this._resourceLoader.systemPromptSources?.(),')
    replace_once(session, '        return buildSystemPrompt(this._baseSystemPromptOptions);',
        '''        const layer=SystemLayerSegment.fromResource(
            new URL('./system-prompt.js',import.meta.url).pathname,
            readFileSync(new URL('./system-prompt.js',import.meta.url)),'SDK system prompt assembly');
        buildSystemPrompt(this._baseSystemPromptOptions,layer);
        return layer;''')

    types=core/'resource-loader.d.ts'
    source=types.read_text()
    source=source.replace('    private systemPromptSourcePath?;\n','').replace('    private appendSystemPromptSourcePaths;\n','')
    source=source.replace('    getSystemPrompt(): string | undefined;',
        '    systemPromptSources?(): import("./system-prompt.js").SystemPromptSources;\n    getSystemPrompt(): string | undefined;',1)
    types.write_text(source)
    # Default declaration has a concrete capability; external loaders may omit it.
    replace_once(types,'    private loaded;',
        '    private loaded;\n    systemPromptSources(): import("./system-prompt.js").SystemPromptSources;')
    types=core/'system-prompt.d.ts'
    types.write_text('''export interface SystemPromptSources {
    custom?: import("./turn-context.js").SystemLayerSegment;
    append: readonly import("./turn-context.js").SystemLayerSegment[];
}
'''+types.read_text())
    replace_once(types, 'export interface BuildSystemPromptOptions {',
        'export interface BuildSystemPromptOptions {\n    systemSources?: SystemPromptSources;')
    replace_once(types, 'buildSystemPrompt(options: BuildSystemPromptOptions): string;',
        'buildSystemPrompt(options: BuildSystemPromptOptions, layer?: import("./turn-context.js").SystemLayerSegment): string;')
    source=types.read_text().replace('        content: string;',
        '        content: string;\n        systemLayer?: import("./turn-context.js").SystemLayerSegment;')
    types.write_text(source)
    types=core/'skills.d.ts'
    replace_once(types, 'formatSkillsForPrompt(skills: Skill[], fileReadTool?: "read" | "bash"): string;',
        'formatSkillsForPrompt(skills: Skill[], fileReadTool?: "read" | "bash", assembly?: import("./turn-context.js").SystemLayerSegment): string;')
    types=core/'agent-session.d.ts'
    replace_once(types, '    private _baseSystemPrompt;',
        '    private _baseSystemPrompt: import("./turn-context.js").SystemLayerSegment;')


def main(package):
    package=Path(package)
    core=package/'dist/core'
    (core/'turn-context.js').write_bytes(Path(__file__).with_name('native-turn-context.mjs').read_bytes())
    (core/'turn-context.d.ts').write_bytes(Path(__file__).with_name('native-turn-context.d.ts').read_bytes())
    session=core/'agent-session.js'
    replace_once(session, '        this.agent.onContextReady = async (context) => await this._commitNativeContext(context);',
        '''        this.agent.onContextReady = async (context, requestId) => {
            const source = await this._commitNativeContext(context);
            this._emit({type: "turn_context_observed", context:(await TurnContext.capture(this,context,source)).observation(requestId)});
        };''')
    source=session.read_text()
    session.write_text('import { TurnContext, NativeInputClaim, SystemLayerSegment } from "./turn-context.js";\n'+source)
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
            case "agent_comms_inspect_context_segment": {
                const context=(await TurnContext.recordedSegment(session,command.identity,
                    command.entries,command.expected,command.parts)).full();
                return outputArray(id, command.type, "segments", context.segments, {identity:context.identity,counter:context.counter});
            }
            case "get_state": {''')
    # Inspection neither consumes a mutation generation nor disturbs summary custody.
    source=rpc.read_text()
    before='"get_state"]'
    if source.count(before)!=2: raise ValueError('Original read-only RPC admission sets changed')
    rpc.write_text(source.replace(before,'"get_state", "agent_comms_inspect_context", "agent_comms_inspect_context_segment"]'))
    replace_once(core/'agent-session.d.ts', '    type: "context_committed";',
        '''    type: "turn_context_observed";
    context: import("./turn-context.js").NativeContextManifest;
} | {
    type: "context_committed";''')
    types=package/'dist/modes/rpc/rpc-types.d.ts'
    replace_once(types,'export type RpcCommand = {', '''export type RpcCommand = {
    id?: string;
    type: "agent_comms_inspect_context";
} | {
    id?: string;
    type: "agent_comms_inspect_context_segment";
    identity: {sessionId: string; sessionFile: string};
    entries: readonly string[];
    expected: import("../../core/turn-context.js").NativeContextSegmentManifest;
    parts: readonly import("../../core/turn-context.js").NativeContextSegmentManifest[];
} | {''')
    replace_once(types,'export type RpcResponse = {', '''export type RpcResponse = {
    id?: string;
    type: "response";
    command: "agent_comms_inspect_context" | "agent_comms_inspect_context_segment";
    success: true;
    data: import("../../core/turn-context.js").NativeContextData;
} | {''')
    extend_input_claim(session,rpc,core)
    extend_system_sources(core)


if __name__=='__main__': main(sys.argv[1])
