#!/usr/bin/env python3
"""Bound Pi's compaction summary input in the pinned native copy only."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

STOCK_SHA = "3d5f1f2a3e801c965214717b6abad1839239b4a030517bffdf0c8eff25df5c2a"
HEADER = (
    "export async function generateSummaryWithUsage(currentMessages, model, reserveTokens, "
    "apiKey, headers, signal, customInstructions, previousSummary, thinkingLevel, "
    "streamFn, env, retry, callbacks, sessionId) {\n"
)
TOKEN_LIMIT = (
    "const maxTokens = Math.min(Math.floor(0.8 * reserveTokens), "
    "model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY);"
)
PROMPT_END = "    promptText += basePrompt;\n"
SUMMARY_RETURN = (
    "    return retryAssistantCall(produce, retry, requestOptions.signal, callbacks);\n"
)
REPORT_SUMMARY = (
    "    const response = await retryAssistantCall(produce, retry, requestOptions.signal, callbacks);\n"
    "    callbacks?.onSummaryResponse?.(response.usage, response.stopReason === 'stop' && contentText(response.content).trim() ? callbacks?.sourceProgress : undefined);\n"
    "    if (response.stopReason === 'stop' && !contentText(response.content).trim()) "
    "throw new Error('Compaction returned an empty summary');\n"
    "    return response;\n"
)
TURN_PREFIX_PROMPT = (
    "    const promptText = `<conversation>\\n${conversationText}\\n</conversation>\\n\\n"
    "${TURN_PREFIX_SUMMARIZATION_PROMPT}`;\n"
)
LIMIT_HELPER = """function summarySource(messages, previousSummary) {
    const transcript = serializeConversation(convertToLlm(messages));
    return previousSummary
        ? `<previous-summary>\\n${previousSummary}\\n</previous-summary>\\n\\n${transcript}`
        : transcript;
}
function sourceCallbacks(callbacks, done, total, phase) {
    return { ...callbacks, sourceProgress: {
        sourceBytesDone: (callbacks?.sourceOffset ?? 0) + done,
        sourceBytesTotal: callbacks?.sourceTotal ?? total,
        summaryPhase: phase ?? callbacks?.sourcePhase ?? "history",
    } };
}
function summaryChunkEnd(source, start, byteLimit) {
    let low = start + 1;
    let high = Math.min(source.length, start + byteLimit);
    while (low < high) {
        const middle = Math.ceil((low + high) / 2);
        if (Buffer.byteLength(source.slice(start, middle), 'utf8') <= byteLimit) low = middle;
        else high = middle - 1;
    }
    let end = low;
    if (end < source.length && /[\\uD800-\\uDBFF]/.test(source[end - 1])) end--;
    // Preserve the final remainder; a trailing newline must not manufacture
    // a separate provider request for a few closing characters.
    if (end < source.length) {
        const newline = source.lastIndexOf('\\n', end - 1);
        if (newline > start + (end - start) / 2) end = newline + 1;
    }
    return end;
}

"""
TURN_PREFIX_BOUNDED = """    const policy = CompactionPolicy.fromEnvironment(env);
    const byteLimit = policy.inputBytes(model, reserveTokens);
    if (Buffer.byteLength(conversationText, 'utf8') > byteLimit * policy.sourceBudgetRatio) {
        return generateSummaryWithUsage(messages, model, reserveTokens, apiKey, headers,
            signal, [TURN_PREFIX_SUMMARIZATION_PROMPT, customInstructions].filter(Boolean).join('\\n\\n'), undefined, thinkingLevel,
            streamFn, env, retry, callbacks, sessionId);
    }
    callbacks = sourceCallbacks(callbacks, Buffer.byteLength(conversationText, 'utf8'), Buffer.byteLength(conversationText, 'utf8'));
"""
BOUNDED = """    // Keep provider prompts byte-bounded without pretending the chars/4
    // estimator is a tokenizer. Independent source segments run at most four
    // at a time; ordered synthesis replaces the serial rolling-summary chain.
    const transcript = serializeConversation(convertToLlm(currentMessages));
    const policy = CompactionPolicy.fromEnvironment(env);
    const byteLimit = policy.inputBytes(model, reserveTokens);
    const source = summarySource(currentMessages, previousSummary);
    const sourceBytes = Buffer.byteLength(source, 'utf8');
    if (!boundedChunk && Buffer.byteLength(transcript, 'utf8') + Buffer.byteLength(previousSummary ?? '', 'utf8') > byteLimit * policy.sourceBudgetRatio) {
        const chunkBytes = Math.floor(byteLimit * policy.sourceBudgetRatio);
        const controller = new AbortController();
        const abort = () => controller.abort(signal?.reason);
        if (signal?.aborted) abort();
        else signal?.addEventListener("abort", abort, { once: true });
        let combinedUsage;
        let processedBytes = 0;
        let failure;
        const partsOf = (text) => {
            const parts = [];
            for (let start = 0; start < text.length;) {
                const end = summaryChunkEnd(text, start, chunkBytes);
                parts.push(text.slice(start, end));
                start = end;
            }
            return parts;
        };
        const summarizeParts = async (parts, phase) => {
            if (phase === "synthesis") callbacks?.onSummaryStart?.(
                sourceCallbacks(callbacks, processedBytes, sourceBytes, "synthesis").sourceProgress);
            const plan = policy.plan(parts);
            parts = plan.segments;
            const results = new Array(parts.length);
            let next = 0;
            const workers = Array.from({ length: Math.min(plan.workers, parts.length) }, async () => {
                while (next < parts.length && !failure && !controller.signal.aborted) {
                    const index = next++;
                    const part = parts[index];
                    const focus = phase === "synthesis"
                        ? "Combine these chronological segment summaries into one continuation summary. Preserve unresolved requests, constraints, exact paths, tool evidence and decisions. Later segments supersede earlier ones only when they explicitly change them."
                        : `Independently summarize chronological segment ${index + 1} of ${parts.length}. Preserve unresolved requests, constraints, exact paths, tool evidence and decisions; later synthesis will combine every segment.`;
                    const partCallbacks = { ...callbacks, sourceProgress: {},
                        onSummaryResponse: (usage, completed) => {
                            if (completed && phase !== "synthesis") processedBytes += Buffer.byteLength(part, 'utf8');
                            const progress = sourceCallbacks(callbacks, processedBytes, sourceBytes,
                                phase === "synthesis" ? "synthesis" : undefined).sourceProgress;
                            callbacks?.onSummaryResponse?.(usage, completed ? progress : undefined);
                        },
                    };
                    try {
                        results[index] = await generateSummaryWithUsage(
                            [{ role: 'user', content: [{ type: 'text', text: part }], timestamp: Date.now() }],
                            model, reserveTokens, apiKey, headers, controller.signal,
                            [customInstructions, focus].filter(Boolean).join('\\n\\n'),
                            undefined, thinkingLevel, streamFn, env,
                            { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } }, partCallbacks, sessionId, true);
                    } catch (error) {
                        failure ??= error;
                        controller.abort(error);
                    }
                }
            });
            await Promise.all(workers);
            if (failure) throw failure;
            if (controller.signal.aborted) throw controller.signal.reason ?? new Error('Compaction aborted');
            for (const result of results) combinedUsage = combinedUsage ? combineUsage(combinedUsage, result.usage) : result.usage;
            return results.map(result => result.text);
        };
        try {
            let summaries = await summarizeParts(partsOf(source), "map");
            for (;;) {
                const ordered = summaries.map((text, index) =>
                    `<segment-summary index="${index + 1}">\\n${text}\\n</segment-summary>`).join('\\n\\n');
                const pieces = partsOf(ordered);
                const next = await summarizeParts(pieces, "synthesis");
                if (pieces.length === 1) return { text: next[0], usage: combinedUsage };
                if (next.reduce((sum, text) => sum + Buffer.byteLength(text, 'utf8'), 0) >= Buffer.byteLength(ordered, 'utf8'))
                    throw new Error('Compaction summaries did not shrink within the context budget');
                summaries = next;
            }
        } finally {
            controller.abort();
            signal?.removeEventListener("abort", abort);
        }
    }
    if (!boundedChunk) callbacks = sourceCallbacks(callbacks, sourceBytes, sourceBytes);
"""


# A tool-result tail has no following valid cut yet. Keep its entire owning
# assistant/tool-result group, rather than defaulting back to the oldest entry
# and falsely concluding that nothing can be summarized.
CUT_SEARCH = """            for (let c = 0; c < cutPoints.length; c++) {
"""
CUT_FALLBACK = """            cutIndex = cutPoints[cutPoints.length - 1];
"""


COMPACT_PREPARATION = "    const { firstKeptEntryId, messagesToSummarize, turnPrefixMessages, isSplitTurn, tokensBefore, previousSummary, fileOps, settings, } = preparation;\n"
SOURCE_TOTAL = """    const historyBytes = messagesToSummarize.length || !isSplitTurn
        ? Buffer.byteLength(summarySource(messagesToSummarize, previousSummary), 'utf8') : 0;
    const prefixBytes = isSplitTurn ? Buffer.byteLength(summarySource(turnPrefixMessages), 'utf8') : 0;
    callbacks = { ...callbacks, sourceTotal: historyBytes + prefixBytes, sourceOffset: 0, sourcePhase: "history" };
    callbacks.onSummaryStart?.({ sourceBytesDone: 0, sourceBytesTotal: callbacks.sourceTotal, summaryPhase: "history" });
"""
PREFIX_CALL = "        const turnPrefixResult = await generateTurnPrefixSummary("
PREFIX_OFFSET = '        callbacks = { ...callbacks, sourceOffset: historyBytes, sourcePhase: "current-turn" };\n'
PREFIX_SIGNATURE = "async function generateTurnPrefixSummary(messages, model, reserveTokens, apiKey, headers, env, signal, thinkingLevel, streamFn, retry, callbacks, sessionId) {"
PREFIX_INVOCATION = "generateTurnPrefixSummary(turnPrefixMessages, model, settings.reserveTokens, apiKey, headers, env, signal, thinkingLevel, streamFn, retry, callbacks, sessionId)"
EMPTY_HISTORY = '        let historyText = "No prior history.";'



def main(path: Path) -> None:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != STOCK_SHA:
        raise SystemExit("Native compaction source does not match the pinned Pi release")
    source = raw.decode()
    if (
        source.count(HEADER) != 1
        or source.count(TOKEN_LIMIT) != 1
        or source.count(PROMPT_END) != 1
        or source.count(SUMMARY_RETURN) != 1
        or source.count(TURN_PREFIX_PROMPT) != 1
        or source.count(CUT_SEARCH) != 1
        or source.count(COMPACT_PREPARATION) != 1
        or source.count(PREFIX_CALL) != 1
        or source.count(PREFIX_SIGNATURE) != 1
        or source.count(PREFIX_INVOCATION) != 1
        or source.count(EMPTY_HISTORY) != 1
    ):
        raise SystemExit("Native compaction anchors changed")
    source = source.replace(
        HEADER,
        LIMIT_HELPER
        + HEADER.replace("sessionId) {", "sessionId, boundedChunk = false) {")
        + BOUNDED,
    )
    source = source.replace(SUMMARY_RETURN, REPORT_SUMMARY, 1)
    source = source.replace(
        TOKEN_LIMIT,
        "const maxTokens = boundedChunk "
        "? policy.summaryTokens(model, byteLimit) "
        ": Math.min(Math.floor(0.8 * reserveTokens), model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY);",
    )
    source = source.replace(
        PROMPT_END,
        PROMPT_END + "    if (Buffer.byteLength(promptText, 'utf8') > byteLimit) "
        "throw new Error('Compaction prompt exceeds its context budget');\n",
    )
    source = source.replace(
        TURN_PREFIX_PROMPT,
        TURN_PREFIX_BOUNDED
        + TURN_PREFIX_PROMPT.replace(
            "${TURN_PREFIX_SUMMARIZATION_PROMPT}",
            "${[TURN_PREFIX_SUMMARIZATION_PROMPT, customInstructions].filter(Boolean).join('\\n\\n')}",
        )
        + "    if (Buffer.byteLength(promptText, 'utf8') > byteLimit) "
        "throw new Error('Turn prefix prompt exceeds its context budget');\n",
    )
    source = source.replace(PREFIX_SIGNATURE, PREFIX_SIGNATURE.replace("sessionId)", "sessionId, customInstructions)"), 1)
    source = source.replace(PREFIX_INVOCATION, PREFIX_INVOCATION[:-1] + ", customInstructions)", 1)
    # A repeated split can have no new history messages. Its old summary is
    # still authoritative context and must survive rather than become empty.
    source = source.replace(EMPTY_HISTORY, '        let historyText = previousSummary || "No prior history.";', 1)
    source = source.replace(CUT_SEARCH, CUT_FALLBACK + CUT_SEARCH, 1)
    source = source.replace(COMPACT_PREPARATION, COMPACT_PREPARATION + SOURCE_TOTAL, 1)
    source = source.replace(PREFIX_CALL, PREFIX_OFFSET + PREFIX_CALL, 1)
    path.write_text('import { CompactionPolicy } from "./agent-comms-policy.js";\n' + source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
