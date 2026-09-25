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
function summaryByteLimit(model, reserveTokens) {
    const window = model.contextWindow > 0 ? model.contextWindow : 128000;
    // Keep the serialized UTF-8 prompt below the model's token budget even
    // for poorly tokenizing text, with room for the system prompt and output.
    const byteLimit = Math.floor((window - reserveTokens) * 0.75);
    if (byteLimit < 4096) throw new Error('Compaction model context is too small');
    return byteLimit;
}
"""
TURN_PREFIX_BOUNDED = """    const byteLimit = summaryByteLimit(model, reserveTokens);
    if (Buffer.byteLength(conversationText, 'utf8') > byteLimit * 0.75) {
        return generateSummaryWithUsage(messages, model, reserveTokens, apiKey, headers,
            signal, TURN_PREFIX_SUMMARIZATION_PROMPT, undefined, thinkingLevel,
            streamFn, env, retry, callbacks, sessionId);
    }
    callbacks = sourceCallbacks(callbacks, Buffer.byteLength(conversationText, 'utf8'), Buffer.byteLength(conversationText, 'utf8'));
"""
BOUNDED = """    // Bound every summary request. A context-overflow recovery cannot summarize
    // the same oversized branch in one provider prompt. Chunk the serialized
    // history and carry a rolling summary; each chunk is a distinct attempt.
    const transcript = serializeConversation(convertToLlm(currentMessages));
    const byteLimit = summaryByteLimit(model, reserveTokens);
    const source = summarySource(currentMessages, previousSummary);
    const sourceBytes = Buffer.byteLength(source, 'utf8');
    if (!boundedChunk && Buffer.byteLength(transcript, 'utf8') + Buffer.byteLength(previousSummary ?? '', 'utf8') > byteLimit * 0.75) {
        const chunkBytes = Math.floor(byteLimit * 0.7);
        const priorLimit = Math.floor(byteLimit * 0.2);
        let rolling;
        let combinedUsage;
        let processedBytes = 0;
        for (let start = 0; start < source.length;) {
            const end = summaryChunkEnd(source, start, chunkBytes);
            const part = source.slice(start, end);
            start = end;
            if (rolling && Buffer.byteLength(rolling, 'utf8') > priorLimit) {
                const compressed = await generateSummaryWithUsage(
                    [{ role: 'user', content: [{ type: 'text', text: rolling }], timestamp: Date.now() }],
                    model, reserveTokens, apiKey, headers, signal, customInstructions,
                    undefined, thinkingLevel, streamFn, env, retry, sourceCallbacks(callbacks, processedBytes, sourceBytes, "shrink"), sessionId, true);
                rolling = compressed.text;
                combinedUsage = combinedUsage ? combineUsage(combinedUsage, compressed.usage) : compressed.usage;
                if (Buffer.byteLength(rolling, 'utf8') > priorLimit) throw new Error('Compaction summary exceeds its context budget');
            }
            processedBytes += Buffer.byteLength(part, 'utf8');
            const step = await generateSummaryWithUsage(
                [{ role: 'user', content: [{ type: 'text', text: part }], timestamp: Date.now() }],
                model, reserveTokens, apiKey, headers, signal, customInstructions,
                rolling, thinkingLevel, streamFn, env, retry, sourceCallbacks(callbacks, processedBytes, sourceBytes), sessionId, true);
            rolling = step.text;
            combinedUsage = combinedUsage ? combineUsage(combinedUsage, step.usage) : step.usage;
        }
        return { text: rolling, usage: combinedUsage };
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
        "? Math.min(4096, Math.max(256, Math.floor(byteLimit / 12)), "
        "model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY) "
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
        + TURN_PREFIX_PROMPT
        + "    if (Buffer.byteLength(promptText, 'utf8') > byteLimit) "
        "throw new Error('Turn prefix prompt exceeds its context budget');\n",
    )
    source = source.replace(CUT_SEARCH, CUT_FALLBACK + CUT_SEARCH, 1)
    source = source.replace(COMPACT_PREPARATION, COMPACT_PREPARATION + SOURCE_TOTAL, 1)
    source = source.replace(PREFIX_CALL, PREFIX_OFFSET + PREFIX_CALL, 1)
    path.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
