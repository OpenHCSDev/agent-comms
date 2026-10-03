/** Observe SDK Context values; never edit, load, queue, or replay model input. */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { estimateTokens } from './compaction/compaction.js';
import { sessionEntryToContextMessages } from './session-manager.js';
import { SessionContext } from './session-context.js';

const hash = value => createHash('sha256').update(value).digest('hex');
const kind = declaration => declaration.name.replace(/Segment$/, '').replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase();
const file = (path, content) => ({kind:'file', path, sha256:hash(content)});

class InputByteRange {
    constructor(offset,length) { this.offset=offset; this.length=length; }
    static requireCoordinate(value) {
        if (!Number.isSafeInteger(value) || value<0)
            throw new TypeError('Context input byte coordinate must be a nonnegative integer');
    }
    static capture(offset,length,bytes) {
        this.requireCoordinate(offset); this.requireCoordinate(length);
        if (length>bytes.length-offset)
            throw new TypeError('Context contribution extends beyond its original input');
        return new this(offset,length);
    }
    bytes(input) { return input.subarray(this.offset,this.offset+this.length); }
    matches(input,digest) { return hash(this.bytes(input))===digest; }
    requireDigest(input,digest) {
        if (!this.matches(input,digest))
            throw new TypeError('Context contribution digest differs from its original byte range');
    }
}
class InputImages {
    constructor(indices,images) {
        this.indices=Object.freeze([...indices]);
        this.fingerprint=hash(JSON.stringify(this.content(images)));
    }
    static capture(indices,images=[]) {
        for (const index of indices) {
            if (!Number.isSafeInteger(index) || index<0 || index>=images.length)
                throw new TypeError('Context contribution names no original input image');
        }
        return new this(indices,images);
    }
    preserved(images) {
        return this.indices.every(index=>images[index]!==undefined)
            && hash(JSON.stringify(this.content(images)))===this.fingerprint;
    }
    content(images) { return this.indices.map(index=>images[index]); }
}
class InputContribution {
    constructor(kind,provenance,range,digest,images) {
        if (!provenance.length) throw new TypeError('Context contribution requires its original source');
        this.kind=kind; this.provenance=provenance; this.range=range;
        this.digest=digest; this.images=images;
    }
    static capture(source,request,bytes) {
        const range=InputByteRange.capture(source.offset,source.length,bytes);
        range.requireDigest(bytes,source.sha256);
        return new this(source.kind,source.provenance,range,source.sha256,
            InputImages.capture(source.images,request.images ?? []));
    }
    preserved(bytes,images) {
        return this.range.matches(bytes,this.digest) && this.images.preserved(images);
    }
    observe(bytes,images,native,journal) {
        const content=[{type:'text',text:this.range.bytes(bytes).toString()},...this.images.content(images)];
        const encoded=JSON.stringify(content);
        return {kind:this.kind,provenance:[...this.provenance,native,journal],
            sha256:hash(encoded),utf8_bytes:Buffer.byteLength(encoded),
            tokens:estimateTokens({role:'user',content,timestamp:0})};
    }
}

/** The existing one-use input claim also owns its read-only source coordinates. */
export class NativeInputClaim {
    constructor(digest, contributions) { this.digest=digest; this.contributions=contributions; }
    static capture(digest, request, contributions=[]) {
        const bytes=Buffer.from(request.text);
        return new this(digest,contributions.map(source=>InputContribution.capture(source,request,bytes)));
    }
    observe(message, native, journal) {
        const blocks=typeof message.content==='string'
            ? [{type:'text',text:message.content}] : message.content;
        const bytes=Buffer.from(blocks.filter(block=>block.type==='text').map(block=>block.text).join(''));
        const images=blocks.filter(block=>block.type==='image');
        if (!this.contributions.every(source=>source.preserved(bytes,images))) {
            // Extension transformations own their resulting value. A changed
            // range cannot claim byte-identical logical attribution or reject
            // an otherwise valid original native input.
            return [new TransformedInputSegment(
                [...this.contributions.flatMap(source=>source.provenance),native,journal],
                [message]).manifest()];
        }
        return this.contributions.map(source=>source.observe(bytes,images,native,journal));
    }
}

class ContextSegment {
    constructor(provenance, value) {
        if (!provenance.length) throw new TypeError('Context requires its original source');
        this.provenance = provenance;
        this.value = value;
        this.contributors = [];
    }
    manifest() {
        const raw = JSON.stringify(this.value);
        return {kind:kind(this.constructor), provenance:this.provenance,
            sha256:hash(raw), utf8_bytes:Buffer.byteLength(raw), tokens:this.tokens(),
            contributors:this.contributors};
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
class TransformedInputSegment extends NativeMessages {}
class ToolCatalogSegment extends ContextSegment {
    tokens() { return estimateTokens({role:'user',content:[{type:'text',text:JSON.stringify(this.value)}],timestamp:0}); }
    payload() { return {tools:this.value}; }
    render(provider) { provider.tools = this.value; }
}

export class TurnContext {
    constructor(identity, segments) { this.identity=identity; this.segments = segments; }
    static async capture(session, context, source,
                         sourceEntries=session.sessionManager.buildContextEntries()) {
        const loader = session.resourceLoader;
        const identity = {sessionId:session.sessionId,sessionFile:session.sessionFile};
        const provenance = [source ? {kind:'native',identity,
            request_generation:source.request_generation,context_digest:source.context_digest}
            : {kind:'preview',identity,context_digest:hash(`pi-assembled-context-v1\n${JSON.stringify(context)}`)}];
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
        const entries = Array.from(sourceEntries);
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
            const entrySource=original ? {kind:'journal',path:journal.path,entries:[original.id]} : journal;
            if (!group || group.constructor !== declaration) {
                group=new declaration([...provenance],[]); segments.push(group);
            }
            group.value.push(message);
            group.contributors.push(...(source?.contributors ?? [])
                .filter(item=>item.input_id===message.inputId).flatMap(item=>item.contributors));
            // Provenance is an observation, never an alternate entry/message owner.
            if (!group.provenance.some(value=>JSON.stringify(value)===JSON.stringify(entrySource)))
                group.provenance.push(entrySource);
        }
        segments.push(new ToolCatalogSegment(provenance,context.tools));
        return new TurnContext(identity,segments);
    }
    static async next(session) {
        return this.capture(session,{systemPrompt:session.systemPrompt,
            messages:await session.agent.convertToLlm(Array.from(session.storedContext.messages(session.agent))),
            tools:session.agent.state.tools});
    }
    static async project(session, entries) {
        // One captured entry set supplies both SDK conversion and attribution.
        // This is a preview; no native input/context proof is minted.
        const selected = Array.from(entries);
        const context = await SessionContext.entryContext(session, selected.values());
        return this.capture(session, context, undefined, selected);
    }
    static async fullSource(session) {
        const manager=session.sessionManager;
        return this.project(session, manager.entryStore.uncompactedEntries(manager.getLeafId()));
    }
    static async recentSource(session) {
        const manager=session.sessionManager;
        return this.project(session, manager.entryStore.keptEntries(manager.getLeafId()));
    }
    render() {
        const provider={messages:[]};
        for(const segment of this.segments) segment.render(provider);
        // Match SDK Context member order for recorded-input byte comparisons.
        return {systemPrompt:provider.systemPrompt,messages:provider.messages,tools:provider.tools};
    }
    manifest(requestId) {
        return {counter:'pi.estimateTokens',segments:this.segments.map(segment=>segment.manifest()),
            ...(requestId === undefined ? {} : {requestId})};
    }
    full() { return {identity:this.identity,counter:'pi.estimateTokens',segments:this.segments.map(segment=>segment.full())}; }
}
