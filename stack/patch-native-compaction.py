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
BOUNDED = """    // Bound every summary request. A context-overflow recovery cannot summarize
    // the same oversized branch in one provider prompt. Chunk the serialized
    // history and carry a rolling summary; each chunk is a distinct attempt.
    const transcript = serializeConversation(convertToLlm(currentMessages));
    const window = model.contextWindow > 0 ? model.contextWindow : 128000;
    const charLimit = Math.max(4096, Math.min(64000, Math.floor((window - reserveTokens) * 0.5)));
    if (!boundedChunk && transcript.length + (previousSummary?.length ?? 0) > charLimit) {
        const source = previousSummary
            ? `<previous-summary>\\n${previousSummary}\\n</previous-summary>\\n\\n${transcript}`
            : transcript;
        const chunkLength = Math.max(1024, Math.floor(charLimit * 0.55));
        const priorLimit = Math.floor(charLimit * 0.35);
        let rolling;
        let combinedUsage;
        for (let start = 0; start < source.length;) {
            let end = Math.min(source.length, start + chunkLength);
            if (end < source.length && /[\\uD800-\\uDBFF]/.test(source[end - 1])) end--;
            const newline = source.lastIndexOf('\\n', end);
            if (newline > start + chunkLength / 2) end = newline + 1;
            const part = source.slice(start, end);
            start = end;
            if (rolling && rolling.length > priorLimit) {
                const compressed = await generateSummaryWithUsage(
                    [{ role: 'user', content: [{ type: 'text', text: rolling }], timestamp: Date.now() }],
                    model, reserveTokens, apiKey, headers, signal, customInstructions,
                    undefined, thinkingLevel, streamFn, env, retry, callbacks, sessionId, true);
                rolling = compressed.text;
                combinedUsage = combinedUsage ? combineUsage(combinedUsage, compressed.usage) : compressed.usage;
                if (rolling.length > priorLimit) throw new Error('Compaction summary exceeds its context budget');
            }
            const step = await generateSummaryWithUsage(
                [{ role: 'user', content: [{ type: 'text', text: part }], timestamp: Date.now() }],
                model, reserveTokens, apiKey, headers, signal, customInstructions,
                rolling, thinkingLevel, streamFn, env, retry, callbacks, sessionId, true);
            rolling = step.text;
            combinedUsage = combinedUsage ? combineUsage(combinedUsage, step.usage) : step.usage;
        }
        return { text: rolling, usage: combinedUsage };
    }
"""


def main(path: Path) -> None:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != STOCK_SHA:
        raise SystemExit("Native compaction source does not match the pinned Pi release")
    source = raw.decode()
    if source.count(HEADER) != 1 or source.count(TOKEN_LIMIT) != 1:
        raise SystemExit("Native compaction anchors changed")
    source = source.replace(
        HEADER, HEADER.replace("sessionId) {", "sessionId, boundedChunk = false) {") + BOUNDED
    )
    source = source.replace(
        TOKEN_LIMIT,
        "const maxTokens = boundedChunk "
        "? Math.min(4096, Math.max(256, Math.floor(charLimit / 12)), "
        "model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY) "
        ": Math.min(Math.floor(0.8 * reserveTokens), model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY);",
    )
    path.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
