/** Bound every tool result against the live model context before session persistence. */
import { createHash, randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { mkdir, rename, writeFile } from 'node:fs/promises';

const IMAGE_CONTEXT_TOKEN_BOUND = 1200;
const RESULT_WINDOW_FRACTION = 0.25;
const NOTICE_RESERVE_TOKENS = 2048;

function contentTokenUpperBound(content) {
    return content.reduce((total, block) => total +
        (block.type === 'text' ? Buffer.byteLength(block.text, 'utf8') : IMAGE_CONTEXT_TOKEN_BOUND), 0);
}

function piEstimatedContentTokens(content) {
    return Math.ceil(content.reduce((total, block) => total +
        (block.type === 'text' ? block.text.length : IMAGE_CONTEXT_TOKEN_BOUND * 4), 0) / 4);
}

function estimationCorrection(content) {
    return Math.max(0, contentTokenUpperBound(content) - piEstimatedContentTokens(content));
}

function prefixWithinBytes(text, maxBytes) {
    let low = 0;
    let high = text.length;
    while (low < high) {
        const middle = Math.ceil((low + high) / 2);
        if (Buffer.byteLength(text.slice(0, middle), 'utf8') <= maxBytes) low = middle;
        else high = middle - 1;
    }
    if (low > 0 && /[\uD800-\uDBFF]/.test(text[low - 1]) && /[\uDC00-\uDFFF]/.test(text[low])) low -= 1;
    return text.slice(0, low);
}

function suffixWithinBytes(text, maxBytes) {
    let low = 0;
    let high = text.length;
    while (low < high) {
        const count = Math.ceil((low + high) / 2);
        if (Buffer.byteLength(text.slice(text.length - count), 'utf8') <= maxBytes) low = count;
        else high = count - 1;
    }
    let start = text.length - low;
    if (start < text.length && /[\uDC00-\uDFFF]/.test(text[start]) && /[\uD800-\uDBFF]/.test(text[start - 1])) start += 1;
    return text.slice(start);
}

function safeSegment(value) {
    return value.replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 96) || 'tool';
}

function cacheRoot() {
    return process.env.XDG_CACHE_HOME || join(homedir(), '.cache');
}

function artifactEnvelope(toolCall, content, details, capturedAt) {
    const envelope = {
        schema: 'pi-tool-result-artifact-v1',
        capturedAt,
        tool: { name: toolCall.name, callId: toolCall.id },
        content,
    };
    if (details === undefined) return { envelope };
    try {
        JSON.stringify(details);
        return { envelope: { ...envelope, details } };
    } catch (error) {
        return {
            envelope: {
                ...envelope,
                detailsOmission: {
                    reason: 'tool result details were not JSON-serializable',
                    error: error instanceof Error ? error.message : String(error),
                },
            },
            detailsFallback: 'details omitted from artifact because they were not JSON-serializable',
        };
    }
}

export class ToolResultArtifactStore {
    constructor(sessionId, root = cacheRoot()) {
        this.directory = join(root, 'pi', 'tool-results', safeSegment(sessionId));
    }

    async persist(toolCall, content, details) {
        const capturedAt = new Date().toISOString();
        const { envelope, detailsFallback } = artifactEnvelope(toolCall, content, details, capturedAt);
        const bytes = Buffer.from(`${JSON.stringify(envelope, null, 2)}\n`, 'utf8');
        const digest = createHash('sha256').update(bytes).digest('hex');
        const stem = `${safeSegment(toolCall.name)}-${safeSegment(toolCall.id)}-${digest.slice(0, 16)}`;
        const path = join(this.directory, `${stem}.json`);
        const temporary = join(this.directory, `.${stem}.${randomUUID()}.tmp`);
        await mkdir(this.directory, { recursive: true, mode: 0o700 });
        await writeFile(temporary, bytes, { mode: 0o600, flag: 'wx' });
        await rename(temporary, path);
        return Object.freeze({ path, digest, byteCount: bytes.length, detailsFallback });
    }
}

export class ToolResultCompactionState {
    static shared = new ToolResultCompactionState();
    include(correctionTokens) {
        return correctionTokens === 0 ? this : new CorrectedToolResultCompaction(correctionTokens);
    }
    shouldCompact(contextTokens, contextWindow, settings, shouldCompact) {
        return shouldCompact(contextTokens, contextWindow, settings);
    }
}

export class CorrectedToolResultCompaction extends ToolResultCompactionState {
    constructor(correctionTokens) {
        super();
        this.correctionTokens = correctionTokens;
    }
    include(correctionTokens) {
        return correctionTokens === 0
            ? this
            : new CorrectedToolResultCompaction(this.correctionTokens + correctionTokens);
    }
    shouldCompact(contextTokens, contextWindow, settings, shouldCompact) {
        return shouldCompact(contextTokens + this.correctionTokens, contextWindow, settings);
    }
}

export class ToolResultBudgetOutcome {
    constructor(content) {
        this.content = content;
        this.correctionTokens = estimationCorrection(content);
    }
    nextCompactionState(current) {
        return current.include(this.correctionTokens);
    }
}

export class WithinBudgetToolResult extends ToolResultBudgetOutcome {}

export class ExternalizedToolResult extends ToolResultBudgetOutcome {
    constructor(content, artifact) {
        super(content);
        this.artifact = artifact;
    }
}

export class ContextExhaustingToolResult extends ExternalizedToolResult {}

export class ToolResultBudgetDecision {
    constructor(estimatedTokens) {
        this.estimatedTokens = estimatedTokens;
    }
    async materialize(content, _context) {
        return new WithinBudgetToolResult(content);
    }
}

export class ExternalizeToolResultDecision extends ToolResultBudgetDecision {
    outcome(content, artifact) {
        return new ExternalizedToolResult(content, artifact);
    }
    failed(content) {
        return new ContextExhaustingToolResult(content, undefined);
    }
    async materialize(content, { toolCall, details, artifactStore, budget }) {
        let artifact;
        try {
            artifact = await artifactStore.persist(toolCall, content, details);
        } catch (error) {
            const reason = error instanceof Error ? error.message : String(error);
            const excerpt = excerptText(content, Math.max(256, budget.resultTokens - NOTICE_RESERVE_TOKENS));
            return this.failed([{
                type: 'text',
                text: `${excerpt.text}\n\n[Complete tool result was rejected at the context boundary because it exceeded the live context budget and artifact persistence failed. Omitted range: ${excerpt.range}. Reason: ${reason}]`,
            }]);
        }
        const excerpt = excerptText(content, Math.max(256, budget.resultTokens - NOTICE_RESERVE_TOKENS));
        const notice = resultNotice({ artifact, budget, decision: this, range: excerpt.range, content });
        const boundedContent = [{ type: 'text', text: excerpt.text ? `${excerpt.text}\n\n${notice}` : notice }];
        return this.outcome(boundedContent, artifact);
    }
}

export class ContextExhaustingToolResultDecision extends ExternalizeToolResultDecision {
    outcome(content, artifact) {
        return new ContextExhaustingToolResult(content, artifact);
    }
}

export class ToolResultBudget {
    constructor({ contextWindow, reserveTokens, contextTokens, parallelCalls }) {
        this.contextWindow = Math.max(0, contextWindow);
        this.inputLimit = Math.max(0, this.contextWindow - Math.max(0, reserveTokens));
        this.remainingTokens = Math.max(0, this.inputLimit - Math.max(0, contextTokens));
        const calls = Math.max(1, parallelCalls);
        const resultTokens = Math.min(
            Math.floor(this.remainingTokens / calls),
            Math.floor((this.inputLimit * RESULT_WINDOW_FRACTION) / calls),
        );
        this.resultTokens = Math.max(0, resultTokens);
    }

    decide(content) {
        const estimatedTokens = contentTokenUpperBound(content);
        if (this.contextWindow <= 0 || estimatedTokens <= this.resultTokens) {
            return new ToolResultBudgetDecision(estimatedTokens);
        }
        return estimatedTokens > this.remainingTokens
            ? new ContextExhaustingToolResultDecision(estimatedTokens)
            : new ExternalizeToolResultDecision(estimatedTokens);
    }
}

function excerptText(content, availableBytes) {
    const textBlocks = content
        .map((block, index) => ({ block, index }))
        .filter(({ block }) => block.type === 'text' && block.text.length > 0);
    if (textBlocks.length === 0 || availableBytes <= 0) {
        return { text: '', range: 'all content blocks omitted' };
    }
    const first = textBlocks[0];
    const last = textBlocks[textBlocks.length - 1];
    if (first.index === last.index) {
        const length = first.block.text.length;
        const headText = prefixWithinBytes(first.block.text, Math.ceil(availableBytes / 2));
        const tailText = suffixWithinBytes(first.block.text, Math.floor(availableBytes / 2));
        if (headText.length + tailText.length >= length) {
            return { text: first.block.text, range: `text block ${first.index}: no characters omitted` };
        }
        const tailStart = length - tailText.length;
        return {
            text: `${headText}\n\n[… omitted by live context budget …]\n\n${tailText}`,
            range: `text block ${first.index}: omitted UTF-16 indices ${headText.length}-${tailStart - 1} of ${length}`,
        };
    }
    const firstText = prefixWithinBytes(first.block.text, Math.ceil(availableBytes / 2));
    const lastText = suffixWithinBytes(last.block.text, Math.floor(availableBytes / 2));
    const lastStart = last.block.text.length - lastText.length;
    return {
        text: `${firstText}\n\n[… omitted by live context budget …]\n\n${lastText}`,
        range: [
            firstText.length < first.block.text.length
                ? `text block ${first.index}: omitted UTF-16 indices ${firstText.length}-${first.block.text.length - 1} of ${first.block.text.length}`
                : undefined,
            first.index + 1 <= last.index - 1
                ? `content blocks ${first.index + 1}-${last.index - 1}: omitted in full`
                : undefined,
            lastStart > 0
                ? `text block ${last.index}: omitted UTF-16 indices 0-${lastStart - 1} of ${last.block.text.length}`
                : undefined,
        ].filter(Boolean).join('; ') || 'non-text content omitted',
    };
}

function resultNotice({ artifact, budget, decision, range, content }) {
    const omittedNonText = content.filter((block) => block.type !== 'text').length;
    const fallback = artifact.detailsFallback ? `\nArtifact fallback: ${artifact.detailsFallback}.` : '';
    return [
        `[Tool result externalized: ${decision.estimatedTokens} UTF-8-byte token upper bound exceeded its ${budget.resultTokens}-token live-context share.]`,
        `Complete result: ${artifact.path}`,
        `SHA-256: ${artifact.digest} (${artifact.byteCount} bytes)`,
        `Excerpt map: ${range}.`,
        omittedNonText > 0
            ? `Non-text blocks: ${omittedNonText} omitted from the excerpt because the live context budget was exceeded; they remain in the complete result.`
            : undefined,
        `Use the read tool with offset/limit for line-oriented JSON, or a bounded JSON parser, to inspect the complete result.${fallback}`,
    ].filter(Boolean).join('\n');
}

export async function budgetToolResult({ toolCall, content, details, contextTokens, contextWindow,
    reserveTokens, parallelCalls, artifactStore }) {
    const budget = new ToolResultBudget({ contextWindow, reserveTokens, contextTokens, parallelCalls });
    return budget.decide(content).materialize(content, { toolCall, details, artifactStore, budget });
}
