/** Observe SDK Context values; never edit, load, queue, or replay model input. */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { estimateTokens } from './compaction/compaction.js';
import { sessionEntryToContextMessages } from './session-manager.js';

const hash = value => createHash('sha256').update(value).digest('hex');
const kind = declaration => declaration.name.replace(/Segment$/, '').replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase();
const file = (path, content) => ({kind:'file', path, sha256:hash(content)});

class ContextSegment {
    constructor(provenance, value) {
        if (!provenance.length) throw new TypeError('Context requires its original source');
        this.provenance = provenance;
        this.value = value;
    }
    manifest() {
        const raw = JSON.stringify(this.value);
        return {kind:kind(this.constructor), provenance:this.provenance,
            sha256:hash(raw), utf8_bytes:Buffer.byteLength(raw), tokens:this.tokens()};
    }
    full() { return {...this.manifest(), ...this.payload()}; }
}
class SystemLayerSegment extends ContextSegment {
    tokens() { return estimateTokens({role:'user', content:[{type:'text', text:this.value}], timestamp:0}); }
    payload() { return {content:this.value}; }
    render(provider) { provider.systemPrompt = this.value; }
}
class NativeMessages extends ContextSegment {
    tokens() { return this.value.reduce((count,message) => count + estimateTokens(message),0); }
    payload() { return {messages:this.value}; }
    render(provider) { provider.messages.push(...this.value); }
}
class TranscriptSegment extends NativeMessages {}
class CompactionSummarySegment extends NativeMessages {}
class InjectionMessageSegment extends NativeMessages {}
class ToolCatalogSegment extends ContextSegment {
    tokens() { return estimateTokens({role:'user',content:[{type:'text',text:JSON.stringify(this.value)}],timestamp:0}); }
    payload() { return {tools:this.value}; }
    render(provider) { provider.tools = this.value; }
}

export class TurnContext {
    constructor(identity, segments) { this.identity=identity; this.segments = segments; }
    static async capture(session, context, source = {request_generation:0, context_digest:hash(`pi-assembled-context-v1\n${JSON.stringify(context)}`)}) {
        const loader = session.resourceLoader;
        const identity = {sessionId:session.sessionId,sessionFile:session.sessionFile};
        const provenance = [{kind:'native', identity, ...source}];
        const systemSources = [...provenance,
            file(new URL('./system-prompt.js',import.meta.url).pathname,readFileSync(new URL('./system-prompt.js',import.meta.url)))];
        const custom = loader.getSystemPromptSource();
        if (custom) systemSources.push(file(custom.path,loader.getSystemPrompt()));
        loader.getAppendSystemPromptSources().forEach((source,index) =>
            systemSources.push(file(source.path,loader.getAppendSystemPrompt()[index])));
        for (const source of loader.getAgentsFiles().agentsFiles)
            systemSources.push(file(source.path,source.content));
        for (const skill of loader.getSkills().skills)
            systemSources.push({kind:'resource', path:skill.filePath,
                representation:'SDK skill prompt metadata',sha256:hash(JSON.stringify(skill))});
        const entries = Array.from(session.sessionManager.buildContextEntries());
        const journal = {kind:'journal', path:session.sessionFile ?? '', entries:entries.map(entry=>entry.id)};
        // SDK owns entry-to-context interpretation. Exact source equality permits
        // narrower coordinates; transformed messages retain the whole selected cut.
        const identified = new Map();
        for (const entry of entries) {
            const declaration = entry.type === 'compaction' ? CompactionSummarySegment
                : entry.type === 'custom_message' ? InjectionMessageSegment : TranscriptSegment;
            for (const message of await session.agent.convertToLlm(sessionEntryToContextMessages(entry))) {
                const encoded=JSON.stringify(message), matches=identified.get(encoded) ?? [];
                matches.push({declaration,id:entry.id}); identified.set(encoded,matches);
            }
        }
        const segments=[new SystemLayerSegment(systemSources,context.systemPrompt)];
        let group;
        for (const message of context.messages) {
            const original=identified.get(JSON.stringify(message))?.shift();
            const declaration=original?.declaration ?? TranscriptSegment;
            const source=original ? {kind:'journal',path:journal.path,entries:[original.id]} : journal;
            if (!group || group.constructor !== declaration) {
                group=new declaration([...provenance],[]); segments.push(group);
            }
            group.value.push(message);
            // Provenance is an observation, never an alternate entry/message owner.
            if (!group.provenance.some(value=>JSON.stringify(value)===JSON.stringify(source)))
                group.provenance.push(source);
        }
        segments.push(new ToolCatalogSegment(provenance,context.tools));
        return new TurnContext(identity,segments);
    }
    static async next(session) {
        return this.capture(session,{systemPrompt:session.systemPrompt,
            messages:await session.agent.convertToLlm(Array.from(session.storedContext.messages(session.agent))),
            tools:session.agent.state.tools});
    }
    render() {
        const provider={messages:[]};
        for(const segment of this.segments) segment.render(provider);
        // Match SDK Context member order for recorded-input byte comparisons.
        return {systemPrompt:provider.systemPrompt,messages:provider.messages,tools:provider.tools};
    }
    manifest() { return {counter:'pi.estimateTokens',segments:this.segments.map(segment=>segment.manifest())}; }
    full() { return {identity:this.identity,counter:'pi.estimateTokens',segments:this.segments.map(segment=>segment.full())}; }
}
